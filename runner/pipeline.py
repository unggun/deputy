"""The state machine. One tick advances each task by at most one step."""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any

from . import graph, vcs
from .agents.base import CodingAgent
from .config import Config, Source
from .models import AIState, Task, TriageResult
from .providers.base import TaskProvider
from .state import Store
from .triage import triage

log = logging.getLogger(__name__)


def _in_window(now: dt.datetime, windows: list[str]) -> bool:
    for w in windows or []:
        start, end = w.split("-")
        s = dt.time(*map(int, start.split(":")))
        e = dt.time(*map(int, end.split(":")))
        if s <= now.time() <= e:
            return True
    return False


def _midnight_epoch(now: dt.datetime) -> int:
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class Pipeline:
    def __init__(self, cfg: Config, source: Source, provider: TaskProvider,
                 agent: CodingAgent, store: Store, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.source = source
        self.provider = provider
        self.agent = agent
        self.store = store
        self.dry_run = dry_run
        self.runner_id = str(cfg.get("runner.id", "runner"))
        self.mode = str(cfg.get("runner.mode", "shadow"))  # shadow | execute
        self.work_root = cfg.path("runner.work_dir", "~/.ai-runner/work")
        self.log_root = cfg.path("runner.log_dir", "~/.ai-runner/logs")
        self.limits = cfg.get("runner.limits", {}) or {}
        self.policy = cfg.get("policy", {}) or {}
        self.pcfg = source.provider

    # --- source-scoped helpers ----------------------------------------
    def _row_id(self, provider_task_id: str) -> str:
        """Namespace task ids so two workspaces can't collide in one DB."""
        return f"{self.source.key}:{provider_task_id}"

    def _env_for(self, repo: dict[str, Any]) -> dict[str, str]:
        return vcs.profile_env(self.cfg.profile_for_repo(repo))

    def _status(self, name: str, default: str) -> str:
        return str(self.pcfg.get(name, default))

    # ==================================================================
    def tick(self, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now()
        windows = self.cfg.get("runner.windows", {}) or {}
        days = [d.lower()[:3] for d in self.cfg.get("runner.days",
                ["mon", "tue", "wed", "thu", "fri"])]
        if now.strftime("%a").lower() not in days:
            log.info("outside working days, nothing to do")
            return
        if not _in_window(now, windows.get("advance", ["00:00-23:59"])):
            log.info("outside advance window, nothing to do")
            return

        budget = float(self.limits.get("daily_budget_usd", 20))
        spent = self.store.spend_since(_midnight_epoch(now))
        if spent >= budget:
            log.warning("daily budget exhausted (%.2f / %.2f USD)", spent, budget)
            return

        # 1. Advance work already in flight (cheap first).
        for row in self.store.in_flight(source=self.source.key):
            try:
                self._advance(row, now, windows)
            except Exception as exc:  # noqa: BLE001
                log.exception("advance failed for %s", row["task_id"])
                self.store.upsert(row["task_id"], last_error=str(exc)[:1000])

        # 2. Triage new tickets.
        if _in_window(now, windows.get("triage", [])):
            self._triage_new()

    # ==================================================================
    def _triage_new(self) -> None:
        cap = int(self.limits.get("max_new_triage_per_run", 10))
        n = 0
        for task in self.provider.fetch_candidates():
            if n >= cap:
                break
            row_id = self._row_id(task.id)
            local = self.store.get(row_id)
            if local and local["state"] != AIState.NEW.value:
                continue
            if task.ai_state != AIState.NEW:
                # The remote says work is under way but this machine has no record
                # of it - it was started on another laptop, or reassigned to us.
                if local is None and not task.ai_state.is_terminal:
                    self._orphan(task)
                continue
            n += 1
            self.store.upsert(row_id, source=self.source.key,
                              provider_task_id=task.id,
                              task_key=task.key, title=task.title,
                              provider_updated_at=task.updated_at)
            try:
                self._do_triage(task)
            except Exception as exc:  # noqa: BLE001
                log.exception("triage failed for %s", task.key)
                self.store.upsert(task.id, last_error=str(exc)[:1000])

    def _orphan(self, task: Task) -> None:
        """A ticket mid-flight on a machine that has no local state for it.

        This happens on reassignment or when a dev switches laptops. We never
        silently adopt: the branch, worktree and PR all live somewhere else.
        Ask a human, once.
        """
        rid = self._row_id(task.id)
        self.store.upsert(rid, source=self.source.key, provider_task_id=task.id,
                          task_key=task.key, title=task.title,
                          state=AIState.BLOCKED.value,
                          last_error=f"orphaned in state {task.ai_state.value}")
        self._comment(
            rid, f"orphan-{task.ai_state.value}",
            f"This ticket is marked `{task.ai_state.value}` but I have no local record "
            f"of it — it was most likely started on a different machine or reassigned.\n\n"
            f"To hand it over: clear the AI State field and I'll re-triage from scratch on "
            f"the next run. Any existing branch or PR will need to be closed manually first.")
        self.provider.set_ai_state(task.id, AIState.BLOCKED)

    def _do_triage(self, task: Task) -> None:
        rid = self._row_id(task.id)
        comments = self.provider.list_comments(task.id)
        repo = self.source.repo_for_container(task.list_id, task.list_name)
        result, cost = triage(self.agent, task, comments, repo, self.policy)
        self.store.record("triage", rid, f"{result.decision} ({result.scope})", cost)
        log.info("[%s] triage -> %s (conf %.2f)", task.key, result.decision, result.confidence)

        if result.decision == "human_only":
            self._comment(rid, "human_only", _fmt_human_only(result))
            self._set(rid, AIState.HUMAN_ONLY)
            return

        if result.decision == "clarify":
            self._comment(rid, f"clarify-{len(comments)}", _fmt_questions(result))
            last = comments[-1].id if comments else ""
            self.store.upsert(rid, last_seen_comment_id=last)
            self._set(rid, AIState.NEEDS_INPUT)
            return

        # execute
        self.store.upsert(rid, repo_key=result.repo_key, meta={"plan": result.plan})
        gate = str(self.policy.get("require_plan_approval_from_scope", "m"))
        order = {"xs": 0, "s": 1, "m": 2, "l": 3}
        if self.mode == "shadow" or order.get(result.scope, 2) >= order.get(gate, 2):
            self._comment(rid, "plan", _fmt_plan(result, shadow=self.mode == "shadow"))
            last = comments[-1].id if comments else ""
            self.store.upsert(rid, last_seen_comment_id=last)
            self._set(rid, AIState.AWAITING_PLAN_OK)
        else:
            self._comment(rid, "plan", _fmt_plan(result, shadow=False))
            self._set(rid, AIState.QUEUED)

    # ==================================================================
    def _advance(self, row: dict[str, Any], now: dt.datetime,
                 windows: dict[str, Any]) -> None:
        state = AIState(row["state"])
        task_id = row["task_id"]   # namespaced row id

        if state in (AIState.NEEDS_INPUT, AIState.AWAITING_PLAN_OK):
            self._check_for_human_reply(row, state)
            return

        if state == AIState.QUEUED:
            if not _in_window(now, windows.get("execute", [])):
                return
            if self.store.active_count() >= int(self.limits.get("max_active_tasks", 3)):
                return
            if self.mode != "execute":
                return

            # Two agents branching from the same base in one repo will produce
            # conflicting PRs. One at a time per repo.
            free = graph.repo_is_free(row["repo_key"] or "",
                                      self.store.in_flight(source=self.source.key),
                                      exclude_row_id=task_id)
            if not free.ok:
                log.debug("[%s] deferred: %s", row["task_key"], free.reason)
                return

            # The provider already knows the dependency graph; read it.
            try:
                clear = graph.blockers_clear(
                    self.provider.blocked_by(row["provider_task_id"]))
            except Exception:  # noqa: BLE001
                clear = graph.Gate(True)   # never let a dependency read stop work
            if not clear.ok:
                self._comment(task_id, f"waiting-{row['provider_task_id']}",
                              f"Holding off — {clear.reason}. "
                              f"I'll pick this up once they're done.")
                log.info("[%s] deferred: %s", row["task_key"], clear.reason)
                return
            if not self.store.acquire_lease(task_id, self.runner_id,
                                            int(self.limits.get("agent_timeout_minutes", 45)) * 60 + 300):
                return
            try:
                self._execute(row)
            finally:
                self.store.release_lease(task_id)
            return

        if state == AIState.IN_PROGRESS:
            # A previous run died mid-flight; the lease has expired.
            if row["lease_until"] and row["lease_until"] > time.time():
                return
            attempts = int(row["attempts"]) + 1
            if attempts > int(self.limits.get("max_fix_attempts", 3)):
                self._escalate(task_id, "Execution crashed repeatedly; needs a human.")
            else:
                self.store.set_state(task_id, AIState.QUEUED, attempts=attempts)
            return

        if state == AIState.PR_OPEN:
            self._poll_pr(row, now, windows)

    # --- execution ----------------------------------------------------
    def _execute(self, row: dict[str, Any]) -> None:
        rid = row["task_id"]
        task = self.provider.get_task(row["provider_task_id"])
        repo = self.source.repo_by_key(row["repo_key"] or "")
        if not repo:
            self._escalate(rid, "No repository mapped; cannot execute.")
            return
        env = self._env_for(repo)

        branch = row["branch"] or f"ai/{vcs.slug(task.key, 20)}-{vcs.slug(task.title, 40)}"
        self.store.set_state(rid, AIState.IN_PROGRESS, branch=branch)
        self.provider.set_ai_state(task.id, AIState.IN_PROGRESS)
        self.provider.set_status(task.id, self._status("in_progress_status", "in progress"))
        self._comment(rid, f"start-{branch}", f"Starting work on branch `{branch}`.")

        wt = vcs.ensure_worktree(repo, branch, self.work_root, env=env)
        if repo.get("setup"):
            vcs.run(["bash", "-lc", repo["setup"]], cwd=wt, check=False, env=env)

        plan = (self.store.get(rid) or {}).get("meta") or "{}"
        prompt = _work_prompt(task, repo, plan)
        res = self.agent.work(prompt, wt,
                              log_path=self.log_root / f"{task.key}-implement.log")
        self.store.record("implement", rid, res.error or "ok", res.cost_usd)

        if not res.ok:
            self._escalate(rid, f"Agent run failed: {res.error}")
            return
        if not vcs.commit_all(wt, f"{task.key}: {task.title}\n\nRefs: {task.url}", env=env):
            self._escalate(rid, "Agent produced no code changes. Ticket may be unclear.")
            return

        base = repo.get("base_branch", "main")
        vcs.push(wt, branch, env=env)
        number, url = vcs.create_pr(
            wt, title=f"{task.key}: {task.title}",
            body=_pr_body(task, res.text, vcs.diff_stat(wt, base)),
            base=base, draft=bool(self.cfg.get("vcs.draft_prs", True)), env=env,
        )
        self.store.set_state(rid, AIState.PR_OPEN, pr_number=number, pr_url=url, attempts=0)
        self.provider.set_ai_state(task.id, AIState.PR_OPEN)
        self._comment(rid, f"pr-{number}",
                      f"Opened draft PR: {url}\n\n**What changed**\n{res.text[:1500]}")

    # --- PR polling ---------------------------------------------------
    def _poll_pr(self, row: dict[str, Any], now: dt.datetime,
                 windows: dict[str, Any]) -> None:
        rid, number = row["task_id"], int(row["pr_number"])
        pid = row["provider_task_id"]
        repo = self.source.repo_by_key(row["repo_key"] or "")
        env = self._env_for(repo)
        wt = self.work_root / repo["key"] / vcs.slug(row["branch"], 60)
        if not wt.exists():
            wt = vcs.ensure_worktree(repo, row["branch"], self.work_root, env=env)

        st = vcs.pr_status(wt, number, env=env)
        if st["merged"]:
            self._set(rid, AIState.DONE)
            self.provider.set_status(pid, self._status("done_status", "done"))
            self._comment(rid, f"merged-{number}", f"PR merged: {st['url']}")
            vcs.remove_worktree(repo, wt, env=env)
            return
        if st["state"] == "CLOSED":
            self._escalate(rid, f"PR {st['url']} was closed without merging.")
            return
        if st["ci"] == "pending":
            return

        profile = self.cfg.profile_for_repo(repo)
        allowlist = (repo.get("reviewer_allowlist")
                     or profile.get("reviewer_allowlist")
                     or self.cfg.get("vcs.reviewer_allowlist", []) or [])
        feedback = vcs.human_feedback(wt, number, allowlist, env=env)
        needs_fix = st["ci"] == "failing" or bool(feedback)

        if not needs_fix:
            if st["draft"] and st["ci"] in ("passing", "none"):
                vcs.mark_ready(wt, number, env=env)
                self.provider.set_status(pid, self._status("review_status", "code review"))
                self._comment(rid, f"ready-{number}",
                              f"Checks are green. PR is ready for review: {st['url']}")
            return

        attempts = int(row["attempts"]) + 1
        if attempts > int(self.limits.get("max_fix_attempts", 3)):
            self._escalate(rid, f"Gave up after {attempts - 1} fix attempts on {st['url']}.")
            return
        if not _in_window(now, windows.get("execute", [])):
            return

        logs = vcs.failing_check_logs(wt, number, env=env) if st["ci"] == "failing" else ""
        prompt = _fix_prompt(row["task_key"], st, feedback, logs)
        res = self.agent.work(prompt, wt,
                              log_path=self.log_root / f"{row['task_key']}-fix-{attempts}.log")
        self.store.record("fix", rid, res.error or "ok", res.cost_usd)
        if res.ok and vcs.commit_all(wt, f"{row['task_key']}: address review/CI feedback", env=env):
            vcs.push(wt, row["branch"], env=env)
            self._comment(rid, f"fix-{number}-{attempts}",
                          f"Pushed fixes (attempt {attempts}).\n\n{res.text[:1200]}")
            self.store.upsert(rid, attempts=attempts)
        else:
            self._escalate(rid, f"Could not resolve feedback automatically: {res.error or 'no changes produced'}")

    # --- human reply detection -----------------------------------------
    def _check_for_human_reply(self, row: dict[str, Any], state: AIState) -> None:
        rid, pid = row["task_id"], row["provider_task_id"]
        comments = self.provider.list_comments(pid)
        seen = row["last_seen_comment_id"] or ""
        idx = next((i for i, c in enumerate(comments) if c.id == seen), -1)
        fresh = [c for c in comments[idx + 1:] if not c.is_bot]
        if not fresh:
            return
        self.store.upsert(rid, last_seen_comment_id=comments[-1].id)

        if state == AIState.AWAITING_PLAN_OK:
            body = " ".join(c.text.lower() for c in fresh)
            approve = self.policy.get("approval_keywords", ["approved", "go ahead", "lgtm", "proceed"])
            reject = self.policy.get("rejection_keywords", ["no", "stop", "hold", "don't"])
            if any(k in body for k in approve) and not any(k in body for k in reject):
                self._set(rid, AIState.QUEUED)
                self._comment(rid, f"approved-{fresh[-1].id}", "Plan approved — queued for execution.")
            else:
                self._set(rid, AIState.NEW)  # re-triage with the new context
            return

        self._set(rid, AIState.NEW)  # re-triage now that questions were answered

    # --- helpers -------------------------------------------------------
    def _pid(self, rid: str) -> str:
        row = self.store.get(rid) or {}
        return str(row.get("provider_task_id") or rid.split(":", 1)[-1])

    def _set(self, rid: str, state: AIState) -> None:
        row = self.store.get(rid)
        if row:
            graph.check(AIState(row["state"]), state)
        self.store.set_state(rid, state)
        self.provider.set_ai_state(self._pid(rid), state)

    def _comment(self, rid: str, key: str, markdown: str) -> None:
        if not self.store.claim_comment(f"{rid}:{key}", rid):
            return
        self.provider.add_comment(self._pid(rid), markdown)

    def _escalate(self, rid: str, reason: str) -> None:
        self.store.set_state(rid, AIState.BLOCKED, last_error=reason[:1000])
        pid = self._pid(rid)
        self.provider.set_ai_state(pid, AIState.BLOCKED)
        self.provider.add_comment(pid, f"**Escalated to a human.**\n\n{reason}")


# --- message formatting -------------------------------------------------
def _fmt_questions(r: TriageResult) -> str:
    qs = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(r.questions))
    return (f"**I need a bit more before I can pick this up.**\n\n{r.reasoning}\n\n{qs}\n\n"
            f"_Reply in this comment thread and I'll re-read the ticket on the next run._")


def _fmt_human_only(r: TriageResult) -> str:
    hints = "\n".join(f"- {h}" for h in r.hints) or "- (no hints)"
    flags = ", ".join(r.risk_flags) or "none"
    return (f"**This one needs a human.**\n\n{r.reasoning}\n\n"
            f"Risk flags: `{flags}`\n\nWhere I'd start:\n{hints}")


def _fmt_plan(r: TriageResult, shadow: bool) -> str:
    steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(r.plan))
    ac = "\n".join(f"- {a}" for a in r.acceptance_criteria) or "- (none inferred)"
    tail = ("\n\n_Shadow mode: no code will be written._" if shadow else
            "\n\n_Reply `approved` to let me start, or correct the plan and I'll re-read it._")
    return (f"**Plan** (scope `{r.scope}`, confidence {r.confidence:.0%})\n\n{steps}\n\n"
            f"**Acceptance criteria I inferred**\n{ac}{tail}")


def _work_prompt(task: Task, repo: dict[str, Any], plan_json: str) -> str:
    return f"""You are implementing a single ticket in an existing repository.
You are on a fresh branch in an isolated worktree. Work only within this directory.

{task.as_prompt_block()}
Agreed plan (JSON): {plan_json}

Rules:
- Follow the conventions already present in this codebase; read before you write.
- Make the smallest change that satisfies the ticket. No refactors, no drive-by fixes.
- Add or update tests. Run the test command: {repo.get('test', '(none configured)')}
- Do NOT modify CI config, dependencies lockfiles, secrets, or .env files unless the ticket explicitly asks.
- Do NOT commit, push, or open a PR — the runner handles that.
- If the ticket turns out to be ambiguous or unsafe, make no changes and explain why.

Finish by printing a short summary: what you changed, which files, how you verified it,
and anything the reviewer should look at closely."""


def _fix_prompt(task_key: str, st: dict[str, Any], feedback: list[str], logs: str) -> str:
    fb = "\n".join(f"- {f}" for f in feedback) or "(none)"
    return f"""You opened PR {st['url']} for {task_key}. It needs another pass.

Failing checks: {', '.join(st['failed_checks']) or 'none'}
Reviewer feedback:
{fb}

CI output (tail):
{logs}

Address the failures and the reviewer feedback. Same rules as before: smallest
possible change, no unrelated edits, do not commit or push. If a piece of feedback
requires a product decision you cannot make, leave it and say so explicitly."""


def _pr_body(task: Task, summary: str, diffstat: str) -> str:
    return (f"Implements [{task.key}]({task.url}) — {task.title}\n\n"
            f"## What changed\n{summary[:4000]}\n\n"
            f"## Diffstat\n```\n{diffstat}\n```\n\n"
            f"---\nGenerated by ai-task-runner. Review carefully before merging.")
