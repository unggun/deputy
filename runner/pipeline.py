"""The state machine. One tick advances each task by at most one step."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import analyst, brain, graph, review, vcs
from .agents.base import CodingAgent
from .config import Config, Source
from .jsonio import StationError
from .models import AIState, AnalysisResult, ReviewResult, Task, TriageResult, agent_log_name
from .providers.base import TaskProvider
from .redact import redact
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
        self.acfg = cfg.get("agent", {}) or {}
        self.brain_root = cfg.path("runner.brain_dir", "~/.ai-runner/brain")
        self.pcfg = source.provider
        # Lazily gathered on first use: building it touches every vcs
        # profile, and a shadow-mode tick may never need one.
        self._secrets: list[str] | None = None

    # --- source-scoped helpers ----------------------------------------
    def _row_id(self, provider_task_id: str, repo_key: str | None = None) -> str:
        """Namespace task ids so two workspaces can't collide in one DB.

        Two shapes:
          "{source}:{ticket}"            before triage has routed the ticket
          "{source}:{ticket}#{repo}"     once it has

        A ticket that needs a change in two repos gets two rows, each with its
        own branch, worktree, PR, review cycle and attempt counter. `#` and not
        `:` because comment idempotency keys are already "{rid}:{key}", and
        `_pid` splits on `:`.

        Rows written before per-repo fan-out existed carry the two-segment id
        with a repo_key column and no `#`. They keep advancing unchanged --
        `_advance` only ever uses `row["task_id"]`, and `rows_for_task` finds
        them by (source, provider_task_id) regardless of id shape.
        """
        base = f"{self.source.key}:{provider_task_id}"
        return f"{base}#{repo_key}" if repo_key else base

    def _env_for(self, repo: dict[str, Any]) -> dict[str, str]:
        return vcs.profile_env(self.cfg.profile_for_repo(repo))

    def _brain_file(self, repo_key: str) -> Path:
        return brain.brain_path(self.brain_root, self.source.key, repo_key)

    def _brain_block(self, repo_key: str) -> str:
        return brain.as_prompt_block(self._brain_file(repo_key))

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

        reason = self._limits_exceeded(now)
        if reason:
            log.warning("%s; nothing to do", reason)
            return

        # 1. Advance work already in flight (cheap first).
        for row in self.store.in_flight(source=self.source.key):
            try:
                self._advance(row, now, windows)
            except Exception as exc:  # noqa: BLE001
                log.exception("advance failed for %s", row["task_id"])
                self.store.upsert(row["task_id"],
                                  last_error=self._clean(str(exc))[:1000])

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
            # One ticket can own several rows (one per repo), and their ids
            # depend on a routing decision triage has not made yet -- so this
            # has to be a lookup by ticket, not by row id.
            rows = self.store.rows_for_task(self.source.key, task.id)
            if any(r["state"] != AIState.NEW.value for r in rows):
                continue
            if task.ai_state != AIState.NEW:
                # The remote says work is under way but this machine has no record
                # of it - it was started on another laptop, or reassigned to us.
                if not rows and not task.ai_state.is_terminal:
                    self._orphan(task)
                continue
            n += 1
            # Placeholder row, keyed without a repo segment: it records that we
            # have seen this ticket (and any triage crash) before triage has
            # decided which repos it belongs to. `_route` retires it.
            row_id = self._row_id(task.id)
            self.store.upsert(row_id, source=self.source.key,
                              provider_task_id=task.id,
                              task_key=task.key, title=task.title,
                              provider_updated_at=task.updated_at)
            try:
                self._do_triage(task)
            except Exception as exc:  # noqa: BLE001
                log.exception("triage failed for %s", task.key)
                self.store.upsert(row_id, last_error=self._clean(str(exc))[:1000])

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

    def _candidate_repos(self, task: Task) -> list[dict[str, Any]]:
        """Repos triage is allowed to choose between for this ticket.

        On a re-triage (a human answered a question, or rejected a plan) the
        ticket already has rows, and those rows may already own branches and
        worktrees. Re-running the routing decision could strand them, so an
        already-routed ticket keeps its repos and triage only re-judges the
        work itself.
        """
        assigned = [r["repo_key"] for r in self.store.rows_for_task(self.source.key, task.id)
                    if r.get("repo_key")]
        if assigned:
            keep = [self.source.repo_by_key(k) for k in dict.fromkeys(assigned)]
            hits = [r for r in keep if r]
            if hits:
                return hits
        return self.source.repos_for_container(task.list_id, task.list_name)

    def _route(self, task: Task, repo_keys: list[str], meta: dict[str, Any],
               state: AIState) -> None:
        """Give the ticket one row per repo and move them all to `state`.

        Rows for repos this ticket no longer needs are dropped, and the
        pre-triage placeholder is retired, so `rows_for_task` afterwards
        returns exactly the repos triage chose.
        """
        existing = {r["task_id"]: r for r in self.store.rows_for_task(self.source.key, task.id)}
        # The placeholder may have been sitting in needs-input tracking where
        # the human's reply landed; carry that forward so the routed rows do
        # not re-read the whole thread as fresh.
        seen_comment = next((r.get("last_seen_comment_id") for r in existing.values()
                             if r.get("last_seen_comment_id")), None)
        wanted: list[str] = []
        for rk in repo_keys:
            rid = self._row_id(task.id, rk)
            # An older two-segment row already carrying this repo is reused as
            # it stands: it may own a branch or a PR, and re-keying it would
            # orphan both.
            legacy = next((tid for tid, r in existing.items()
                           if r.get("repo_key") == rk and "#" not in tid), None)
            wanted.append(legacy or rid)
            self.store.upsert(legacy or rid, source=self.source.key,
                              provider_task_id=task.id, task_key=task.key,
                              title=task.title, repo_key=rk,
                              provider_updated_at=task.updated_at, meta=dict(meta),
                              **({"last_seen_comment_id": seen_comment} if seen_comment else {}))
        for tid in existing:
            if tid not in wanted:
                self.store.delete(tid)
        for tid in wanted:
            self._set(tid, state)

    def _do_triage(self, task: Task) -> None:
        rid = self._row_id(task.id)
        comments = self.provider.list_comments(task.id)
        repos = self._candidate_repos(task)
        try:
            result, spend = triage(self.agent, task, comments, repos, self.policy)
        except StationError as exc:
            # The agent run itself failed (not just an unparseable reply).
            # Record what it cost before this propagates to the generic
            # per-row handler in tick() — otherwise a failing triage run
            # spends money `daily_budget_usd` never finds out about.
            self.store.record("triage", rid, f"failed: {exc}", exc.cost_usd, exc.tokens)
            raise
        self.store.record("triage", rid, f"{result.decision} ({result.scope})",
                          spend.cost_usd, spend.tokens)
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
        meta = {"plan": result.plan, "acceptance_criteria": result.acceptance_criteria}
        gate = str(self.policy.get("require_plan_approval_from_scope", "m"))
        order = {"xs": 0, "s": 1, "m": 2, "l": 3}
        if len(result.repo_keys) > 1:
            log.info("[%s] spans %d repos: %s", task.key,
                     len(result.repo_keys), ", ".join(result.repo_keys))
        if self.mode == "shadow" or order.get(result.scope, 2) >= order.get(gate, 2):
            # Mark this as a fresh entry into PLANNING so the arm below does
            # not mistake a healthy first run for a crash recovery. Each repo
            # gets its own analyst pass: the plan for the backend is not the
            # plan for the frontend.
            meta["plan_pending"] = True
            # A human is going to read this plan, so ground it in the code
            # first. The plan comment is posted by _plan, not here.
            self._route(task, result.repo_keys, meta, AIState.PLANNING)
        else:
            self._route(task, result.repo_keys, meta, AIState.QUEUED)
            for rk in result.repo_keys:
                self._comment(self._row_id(task.id, rk), "plan",
                              _fmt_plan(result, shadow=False))

    # ==================================================================
    def _advance(self, row: dict[str, Any], now: dt.datetime,
                 windows: dict[str, Any]) -> None:
        state = AIState(row["state"])
        task_id = row["task_id"]   # namespaced row id

        if state in (AIState.NEEDS_INPUT, AIState.AWAITING_PLAN_OK):
            self._check_for_human_reply(row, state)
            return

        if state == AIState.PLANNING:
            if not _in_window(now, windows.get("execute", [])):
                return
            if row["lease_until"] and row["lease_until"] > time.time():
                return
            if not self._budget_available(now):
                return
            # Acquire the lease BEFORE consuming the `plan_pending` marker or
            # counting an attempt. If another runner wins the lease between
            # our stale `in_flight()` snapshot and this call, we must not
            # have already written off this round as a crash — that would
            # misclassify a healthy run happening elsewhere as our failure.
            if not self.store.acquire_lease(
                    task_id, self.runner_id,
                    int(self.limits.get("agent_timeout_minutes", 45)) * 60 + 300):
                return
            try:
                meta = json.loads(row["meta"] or "{}")
                if meta.pop("plan_pending", False):
                    # A fresh route into planning: this is the first analyst
                    # run for this plan, not a retry. `_plan` is invoked from
                    # this arm, so unlike the IN_PROGRESS arm, reaching here
                    # does not by itself imply the previous attempt failed.
                    self.store.upsert(task_id, meta=meta)
                else:
                    # No pending marker means the previous `_plan` died
                    # before it could transition the row. Only that counts
                    # against attempts.
                    attempts = int(row["attempts"]) + 1
                    if attempts > int(self.limits.get("max_fix_attempts", 3)):
                        self._escalate(task_id, "Analyst crashed repeatedly; needs a human.")
                        return
                    self.store.upsert(task_id, attempts=attempts)
                self._plan(row)
            finally:
                self.store.release_lease(task_id)
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
            if not self._budget_available(now):
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
                self._record_transition(task_id, AIState.QUEUED)
                self.store.set_state(task_id, AIState.QUEUED, attempts=attempts)
            return

        if state == AIState.REVIEW:
            if not _in_window(now, windows.get("execute", [])):
                return
            if row["lease_until"] and row["lease_until"] > time.time():
                return
            if not self._budget_available(now):
                return
            # Same ordering fix as the PLANNING arm above: acquire the lease
            # before touching `review_pending` or `attempts`, so a lost race
            # for the lease leaves both untouched for whoever holds it.
            if not self.store.acquire_lease(
                    task_id, self.runner_id,
                    int(self.limits.get("agent_timeout_minutes", 45)) * 60 + 300):
                return
            try:
                meta = json.loads(row["meta"] or "{}")
                if meta.pop("review_pending", False):
                    # A fresh push: this is the first review of this branch
                    # state, not a retry. `_review` is invoked from this arm,
                    # so unlike the IN_PROGRESS arm, reaching here does not
                    # by itself imply the previous attempt failed.
                    self.store.upsert(task_id, meta=meta)
                else:
                    # No pending marker means the previous `_review` died
                    # before it could transition the row. Only that counts
                    # against attempts.
                    attempts = int(row["attempts"]) + 1
                    if attempts > int(self.limits.get("max_fix_attempts", 3)):
                        self._escalate(task_id, "Reviewer crashed repeatedly; needs a human.")
                        return
                    self.store.upsert(task_id, attempts=attempts)
                self._review(row)
            finally:
                self.store.release_lease(task_id)
            return

        if state == AIState.PR_OPEN:
            self._poll_pr(row, now, windows)

    def _plan(self, row: dict[str, Any]) -> None:
        """The analyst station: turn a repo-blind triage plan into a real one."""
        rid = row["task_id"]
        task = self.provider.get_task(row["provider_task_id"])
        repo = self.source.repo_by_key(row["repo_key"] or "")
        if not repo:
            self._escalate(rid, "No repository mapped; cannot plan.")
            return

        env = self._env_for(repo)
        branch = row["branch"] or vcs.branch_name(self.provider.branch_key(task), task.title)
        wt = vcs.ensure_worktree(repo, branch, self.work_root, env=env)
        self.store.upsert(rid, branch=branch)

        meta = json.loads((self.store.get(rid) or {}).get("meta") or "{}")
        comments = self.provider.list_comments(task.id)
        # Cycle-scope the plan comment key on the comment count observed
        # *before* this round posts anything. On a plan rejection
        # (plan-review -> new -> planning -> plan-review) the human's
        # rejection reply is itself a new comment, so this count always
        # differs between rounds — the same trick `_do_triage` uses for
        # `clarify-{len(comments)}`. A rerun of the exact same tick
        # recomputes the same count before anything new is posted, so the
        # idempotency claim in `_comment` still suppresses the duplicate.
        round_key = len(comments)
        try:
            result, spend = analyst.analyse(
                self.agent, task, comments, repo,
                self._brain_block(repo["key"]), meta.get("acceptance_criteria", []),
                cwd=wt,
                model=self.acfg.get("model"),
                tools=self.acfg.get("planner_tools") or analyst.PLANNER_TOOLS,
                log_path=self.log_root / agent_log_name(task.key, repo["key"], "plan"),
            )
        except StationError as exc:
            self.store.record("plan", rid, f"failed: {exc}", exc.cost_usd, exc.tokens)
            raise
        self.store.record("plan", rid, result.verdict, spend.cost_usd, spend.tokens)
        # The analyst produced output without crashing: this station's own
        # crash budget resets so the next station starts with a clean slate
        # instead of inheriting attempts spent recovering earlier crashes.
        self.store.upsert(rid, attempts=0)

        if result.verdict == "needs_input":
            self._comment(rid, f"plan-questions-{round_key}", _fmt_analyst_questions(result))
            comments = self.provider.list_comments(task.id)
            self.store.upsert(rid, last_seen_comment_id=comments[-1].id if comments else "")
            self._set(rid, AIState.NEEDS_INPUT)
            return

        meta.update(plan=result.plan,
                    acceptance_criteria=result.acceptance_criteria,
                    problem=result.problem, approach=result.approach,
                    files_of_interest=result.files_of_interest)
        self.store.upsert(rid, meta=meta)
        self._comment(rid, f"plan-{round_key}", _fmt_analysis(result, shadow=self.mode == "shadow"))
        comments = self.provider.list_comments(task.id)
        self.store.upsert(rid, last_seen_comment_id=comments[-1].id if comments else "")
        self._set(rid, AIState.AWAITING_PLAN_OK)

    # --- execution ----------------------------------------------------
    def _execute(self, row: dict[str, Any]) -> None:
        rid = row["task_id"]
        task = self.provider.get_task(row["provider_task_id"])
        repo = self.source.repo_by_key(row["repo_key"] or "")
        if not repo:
            self._escalate(rid, "No repository mapped; cannot execute.")
            return
        env = self._env_for(repo)

        branch = row["branch"] or vcs.branch_name(self.provider.branch_key(task), task.title)
        meta = json.loads((self.store.get(rid) or {}).get("meta") or "{}")
        revision = meta.get("revision")

        self._record_transition(rid, AIState.IN_PROGRESS)
        self.store.set_state(rid, AIState.IN_PROGRESS, branch=branch)
        self._push_remote_state(rid)
        # Only claim "in progress" on the board once no repo on this ticket is
        # further back than that -- a second repo still queued means the ticket
        # as a whole has not started.
        self._set_status_when(rid, AIState.IN_PROGRESS,
                              "in_progress_status", "in progress")
        if not revision:
            self._comment(rid, f"start-{branch}", f"Starting work on branch `{branch}`.")

        wt = vcs.ensure_worktree(repo, branch, self.work_root, env=env)
        if repo.get("setup"):
            vcs.run(["bash", "-lc", repo["setup"]], cwd=wt, check=False, env=env)

        brain_block = self._brain_block(repo["key"])
        if revision:
            prompt = _revision_prompt(task, repo, meta, revision, brain_block)
        else:
            prompt = _work_prompt(task, repo, meta, brain_block)
        res = self.agent.work(prompt, wt,
                              log_path=self.log_root / agent_log_name(task.key, repo["key"], "implement"))
        self.store.record("implement", rid, res.error or "ok", res.cost_usd,
                          res.usage.total)

        if not res.ok:
            self._escalate(rid, f"Agent run failed: {res.error}")
            return
        if not vcs.commit_all(wt, f"{task.key}: {task.title}\n\nRefs: {task.url}", env=env):
            self._escalate(rid, "Agent produced no code changes. Ticket may be unclear.")
            return

        vcs.push(wt, branch, env=env)
        meta = json.loads((self.store.get(rid) or {}).get("meta") or "{}")
        cycle = int((meta.get("revision") or {}).get("cycle", 0))
        meta.pop("revision", None)          # the reviewer judges a clean row
        meta["impl_summary"] = res.text[:4000]
        meta["review_cycle"] = cycle
        meta["review_pending"] = True       # fresh entry into REVIEW, not a retry
        # The implementer station just completed successfully and is handing
        # off to REVIEW — reset the shared `attempts` counter so the reviewer
        # gets its own full crash budget instead of inheriting whatever the
        # implementer spent recovering earlier crashes.
        self.store.upsert(rid, meta=meta, attempts=0)
        self._set(rid, AIState.REVIEW)
        self._comment(rid, f"pushed-{branch}-{cycle}",
                      f"Pushed `{branch}`. Running review before opening a PR.")

    def _review(self, row: dict[str, Any]) -> None:
        """The reviewer station: judge the pushed branch before a human sees it."""
        rid = row["task_id"]
        task = self.provider.get_task(row["provider_task_id"])
        repo = self.source.repo_by_key(row["repo_key"] or "")
        if not repo:
            self._escalate(rid, "No repository mapped; cannot review.")
            return

        env = self._env_for(repo)
        base = repo.get("base_branch", "main")
        branch = row["branch"]
        meta = json.loads((self.store.get(rid) or {}).get("meta") or "{}")

        wt = vcs.ensure_review_worktree(repo, branch, self.work_root, env=env)
        try:
            result, spend = review.review(
                self.agent, task.key, meta.get("acceptance_criteria", []),
                base, branch, meta.get("impl_summary", ""),
                self._brain_block(repo["key"]),
                cwd=wt,
                model=self.acfg.get("reviewer_model") or self.acfg.get("model"),
                tools=review.tools_for(repo, self.acfg.get("reviewer_tools")),
                log_path=self.log_root / agent_log_name(task.key, repo["key"], "review"),
            )
        except StationError as exc:
            self.store.record("review", rid, f"failed: {exc}", exc.cost_usd, exc.tokens)
            raise
        self.store.record("review", rid, result.verdict, spend.cost_usd, spend.tokens)
        cycle = int(meta.get("review_cycle", 0))

        if result.verdict == "reject":
            self._comment(rid, f"review-reject-{cycle}", _fmt_review(result))
            self._remove_review_worktree(repo, wt, env, task.key)
            self._escalate(rid, f"Reviewer rejected the approach: {result.summary}")
            return

        if result.verdict == "request_changes":
            if cycle + 1 > int(self.limits.get("max_review_cycles", 2)):
                self._comment(rid, f"review-giveup-{cycle}", _fmt_review(result))
                self._remove_review_worktree(repo, wt, env, task.key)
                self._escalate(
                    rid, f"Still failing review after {cycle} revision(s). "
                         f"Unresolved: {'; '.join(result.blocking_findings)[:600]}")
                return
            meta["revision"] = {"cycle": cycle + 1,
                                "findings": result.blocking_findings,
                                "summary": result.summary}
            # The reviewer station just completed successfully (a verdict,
            # not a crash) and is handing back to QUEUED for another
            # implementation pass — reset `attempts` so that pass gets its
            # own crash budget. `meta["review_cycle"]` (bumped separately by
            # `_execute`, from `revision.cycle` above) is a different counter
            # entirely and is untouched here.
            self.store.upsert(rid, meta=meta, attempts=0)
            self._comment(rid, f"review-changes-{cycle}", _fmt_review(result))
            self._set(rid, AIState.QUEUED)
            return

        # approve
        written = brain.append(
            self._brain_file(repo["key"]), result.brain_notes,
            max_bytes=int(self.limits.get("brain_max_bytes", brain.DEFAULT_MAX_BYTES)))
        if written:
            log.info("[%s] recorded %d brain note(s)", task.key, len(written))

        impl_wt = self.work_root / repo["key"] / vcs.slug(branch, 60)
        if not impl_wt.exists():
            impl_wt = vcs.ensure_worktree(repo, branch, self.work_root, env=env)
        number, url = vcs.create_pr(
            impl_wt, title=f"{task.key}: {task.title}",
            body=_pr_body(task, meta, result, vcs.diff_stat(impl_wt, base)),
            base=base, draft=bool(self.cfg.get("vcs.draft_prs", True)), env=env,
        )
        self._record_transition(rid, AIState.PR_OPEN)
        self.store.set_state(rid, AIState.PR_OPEN, pr_number=number,
                             pr_url=url, attempts=0)
        self._push_remote_state(rid)
        self._comment(rid, f"pr-{number}",
                      f"Review passed. Opened draft PR: {url}\n\n{result.summary}")
        # The row is already fully committed (state, PR, comment) — a failure
        # tidying up the review checkout must not surface as this task's
        # error.
        self._remove_review_worktree(repo, wt, env, task.key)

    def _remove_review_worktree(self, repo: dict[str, Any], wt: Path,
                                env: dict[str, str], task_key: str) -> None:
        """Best-effort cleanup of the reviewer's detached checkout.

        Called on every path out of REVIEW — approve, reject, and giving up
        on cycle exhaustion — so a rejected or exhausted ticket does not leak
        one worktree per attempt. Only the `request_changes`-and-continue
        path skips this: that row is coming straight back through REVIEW, so
        the checkout is about to be reused rather than abandoned.
        """
        try:
            vcs.remove_worktree(repo, wt, env=env)
        except Exception:  # noqa: BLE001
            log.warning("[%s] could not remove review worktree %s", task_key, wt,
                       exc_info=True)

    # --- PR polling ---------------------------------------------------
    def _poll_pr(self, row: dict[str, Any], now: dt.datetime,
                 windows: dict[str, Any]) -> None:
        rid, number = row["task_id"], int(row["pr_number"])
        repo = self.source.repo_by_key(row["repo_key"] or "")
        env = self._env_for(repo)
        wt = self.work_root / repo["key"] / vcs.slug(row["branch"], 60)
        if not wt.exists():
            wt = vcs.ensure_worktree(repo, row["branch"], self.work_root, env=env)

        st = vcs.pr_status(wt, number, env=env)
        if st["merged"]:
            self._set(rid, AIState.DONE)
            # Gated: on a two-repo ticket the first merge must not close the
            # ticket out from under the second, still-open PR.
            self._set_status_when(rid, AIState.DONE, "done_status", "done")
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
        meta = json.loads(row["meta"] or "{}")
        # Only feedback newer than the last round we acted on. Without this a
        # comment that needs no code change is re-read on every poll and runs
        # the fix agent again, forever, at full agent cost per tick.
        feedback = vcs.human_feedback(wt, number, allowlist,
                                      since_iso=meta.get("feedback_seen_at"),
                                      env=env)
        approvals = vcs.approving_reactions(wt, number, allowlist, env=env)
        needs_fix = st["ci"] == "failing" or bool(feedback)

        if approvals and not feedback and st["ci"] != "failing":
            # Reviewed and approved by reaction alone. Say so once, then keep
            # waiting for a merge rather than sitting mute or "fixing" nothing.
            self._comment(rid, f"approved-{number}",
                          "Reviewed with no changes requested "
                          f"({', '.join('@' + a for a in approvals)}). "
                          f"Waiting on merge: {st['url']}")

        if not needs_fix:
            if st["draft"] and st["ci"] in ("passing", "none"):
                vcs.mark_ready(wt, number, env=env)
                self._set_status_when(rid, AIState.PR_OPEN, "review_status", "code review")
                self._comment(rid, f"ready-{number}",
                              f"Checks are green. PR is ready for review: {st['url']}")
            return

        attempts = int(row["attempts"]) + 1
        if attempts > int(self.limits.get("max_fix_attempts", 3)):
            self._escalate(rid, f"Gave up after {attempts - 1} fix attempts on {st['url']}.")
            return
        if not _in_window(now, windows.get("execute", [])):
            return
        if not self._budget_available(now):
            return

        logs = vcs.failing_check_logs(wt, number, env=env) if st["ci"] == "failing" else ""
        prompt = _fix_prompt(row["task_key"], st, feedback, logs,
                             self._brain_block(repo["key"]))
        res = self.agent.work(prompt, wt,
                              log_path=self.log_root / agent_log_name(row["task_key"], repo["key"], f"fix-{attempts}"))
        self.store.record("fix", rid, res.error or "ok", res.cost_usd,
                          res.usage.total)
        # Second-precision and Z-suffixed to match GitHub's `createdAt`: these
        # are compared as strings, and an isoformat() fraction sorts *before*
        # a bare `Z`, which would let a same-second comment through forever.
        meta["feedback_seen_at"] = dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        if res.ok and vcs.commit_all(wt, f"{row['task_key']}: address review/CI feedback", env=env):
            vcs.push(wt, row["branch"], env=env)
            self._comment(rid, f"fix-{number}-{attempts}",
                          f"Pushed fixes (attempt {attempts}).\n\n{res.text[:1200]}")
            self.store.upsert(rid, attempts=attempts, meta=meta)
        elif res.ok:
            # The agent ran cleanly and changed nothing, which means the
            # feedback needed no code change -- an integration notice, a
            # reviewer saying it looks fine, a question already answered.
            # Escalating here blocks a perfectly healthy PR, so instead record
            # the watermark and keep waiting. Only a crashed agent is a fault.
            self.store.upsert(rid, meta=meta)
            log.info("[%s] PR %d feedback needed no code change; still waiting",
                     row["task_key"], number)
        else:
            # Deliberately not recording the watermark: this feedback was not
            # handled, and a human taking the row over should still see it.
            self._escalate(rid, f"Could not resolve feedback automatically: {res.error}")

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
            if _matches_any(body, approve) and not _matches_any(body, reject):
                self._set(rid, AIState.QUEUED)
                self._comment(rid, f"approved-{fresh[-1].id}", "Plan approved — queued for execution.")
            else:
                # Rejected: `_plan` always leaves a worktree behind for this
                # branch. The row is going back to `new`, and nothing else
                # will ever revisit this exact branch to clean it up, so do
                # it here before re-triage starts a fresh round.
                self._remove_planning_worktree(row)
                self._set(rid, AIState.NEW)  # re-triage with the new context
            return

        self._set(rid, AIState.NEW)  # re-triage now that questions were answered

    def _remove_planning_worktree(self, row: dict[str, Any]) -> None:
        """Best-effort cleanup of the analyst's worktree on plan rejection.

        `_plan` creates this worktree unconditionally the moment planning
        starts. If the human rejects the resulting plan, the row goes back to
        `new` and nothing else in the pipeline will revisit this exact branch
        again on its own — leaving one worktree behind per rejected round.
        """
        repo = self.source.repo_by_key(row["repo_key"] or "")
        branch = row["branch"]
        if not repo or not branch:
            return
        wt = self.work_root / repo["key"] / vcs.slug(branch, 60)
        try:
            vcs.remove_worktree(repo, wt, env=self._env_for(repo))
        except Exception:  # noqa: BLE001
            log.warning("[%s] could not remove planning worktree %s",
                       row.get("task_key"), wt, exc_info=True)

    # --- helpers -------------------------------------------------------
    def _limits_exceeded(self, now: dt.datetime) -> str | None:
        """Which daily limit, if any, is spent. Returns a reason or None.

        Two limits, because with a claude.ai login they measure different
        things. `total_cost_usd` from the CLI is an API-*equivalent* price for
        work that is actually drawn from the developer's 5-hourly/weekly
        subscription quota — real money for an `ANTHROPIC_API_KEY` run,
        notional for a subscription run. `daily_tokens` is denominated in the
        same units as the quota, so it is the limit that bites on the setup
        most people actually have. Whichever trips first stops the tick.
        """
        midnight = _midnight_epoch(now)
        budget = float(self.limits.get("daily_budget_usd", 20))
        spent = self.store.spend_since(midnight)
        if spent >= budget:
            return f"daily budget exhausted ({spent:.2f} / {budget:.2f} USD)"
        cap = int(self.limits.get("daily_tokens", 0) or 0)
        if cap:
            used = self.store.tokens_since(midnight)
            if used >= cap:
                return f"daily token cap exhausted ({used:,} / {cap:,} tokens)"
        return None

    def _budget_available(self, now: dt.datetime) -> bool:
        """Re-checked immediately before every agent run (analyst, implementer,
        reviewer, CI fix).

        `tick()` checks the daily limits once, before any work starts. But one
        tick can run several of those agent passes across several rows —
        without a re-check here, spend recorded by an earlier row in the same
        tick is invisible until the *next* tick's top-of-tick gate, by which
        point the limit may already be blown well past the configured cap.
        """
        reason = self._limits_exceeded(now)
        if reason:
            log.warning("[%s] %s mid-tick; deferring further agent runs to "
                        "the next tick", self.source.key, reason)
            return False
        return True

    def _pid(self, rid: str) -> str:
        row = self.store.get(rid) or {}
        # The fallback has to drop the "#repo" segment too, or a missing row
        # would address the provider with "EPD-101#shop-web".
        return str(row.get("provider_task_id") or rid.split(":", 1)[-1].split("#", 1)[0])

    # --- one ticket, several rows --------------------------------------
    # Least-advanced-wins ordering for the single remote state field shared by
    # every row of one ticket. A ticket is only `done` once every repo is; it
    # reads `in-progress` while any repo still has work. Because each row moves
    # forward independently, the minimum is the honest answer -- claiming
    # `pr-open` while the other repo has not been written yet would be a lie to
    # whoever is reading the board.
    _PROGRESS = [AIState.NEW, AIState.NEEDS_INPUT, AIState.PLANNING,
                 AIState.AWAITING_PLAN_OK, AIState.QUEUED, AIState.IN_PROGRESS,
                 AIState.REVIEW, AIState.PR_OPEN, AIState.DONE]

    def _sibling_rows(self, rid: str) -> list[dict[str, Any]]:
        pid = self._pid(rid)
        rows = self.store.rows_for_task(self.source.key, pid)
        return rows or ([r] if (r := self.store.get(rid)) else [])

    def _remote_state(self, rid: str) -> AIState:
        """What the ticket's single AI State field should say right now.

        With one row this is just that row's state, so the single-repo path is
        byte-identical to what it was before fan-out existed.
        """
        rows = self._sibling_rows(rid)
        states = []
        for r in rows:
            try:
                states.append(AIState(r["state"]))
            except ValueError:                      # unknown value in the DB
                continue
        if not states:
            return AIState(self.store.get(rid)["state"])
        # Anything needing a human outranks progress: surface it immediately
        # rather than hiding it behind the other repo's healthy state.
        for attention in (AIState.BLOCKED, AIState.HUMAN_ONLY):
            if attention in states:
                return attention
        rank = {st: i for i, st in enumerate(self._PROGRESS)}
        return min(states, key=lambda st: rank.get(st, 0))

    def _push_remote_state(self, rid: str) -> None:
        self.provider.set_ai_state(self._pid(rid), self._remote_state(rid))

    def _set_status_when(self, rid: str, reached: AIState,
                         name: str, default: str) -> None:
        """Move the provider's own workflow status, but only once EVERY repo
        on this ticket has reached `reached`.

        Without the gate, the first repo to merge would flip a two-repo ticket
        to the done status while the other PR was still open.
        """
        if self._remote_state(rid) is reached:
            self.provider.set_status(self._pid(rid), self._status(name, default))

    def _repo_tag(self, rid: str) -> str:
        """`[repo] ` prefix for comments, only when the ticket spans repos."""
        rows = self._sibling_rows(rid)
        if len(rows) < 2:
            return ""
        row = self.store.get(rid) or {}
        return f"`{row.get('repo_key') or '?'}` — " if row.get("repo_key") else ""

    def _record_transition(self, rid: str, new_state: AIState) -> None:
        """Ledger line for one state change.

        Called from every `store.set_state` site. The ledger otherwise holds
        agent runs only, so there is no history of when a row reached pr-open
        or done, and throughput cannot be reconstructed. `_set` is not the
        single funnel it looks like: three stations write state directly.
        """
        row = self.store.get(rid)
        if row:
            self.store.record("state", rid, f"{row['state']}->{new_state.value}")

    def _set(self, rid: str, state: AIState) -> None:
        row = self.store.get(rid)
        if row:
            graph.check(AIState(row["state"]), state)
        self._record_transition(rid, state)
        self.store.set_state(rid, state)
        self._push_remote_state(rid)

    def _comment(self, rid: str, key: str, markdown: str) -> None:
        """Post one comment, at most once ever.

        Everything bound for a ticket goes through here, which makes this the
        one place credential scrubbing has to happen. Comment bodies routinely
        carry agent output, `git` stderr and `gh` stderr verbatim, and a
        ticket comment is permanent and visible to the whole workspace.
        """
        if not self.store.claim_comment(f"{rid}:{key}", rid):
            return
        body = self._clean(markdown)
        self.provider.add_comment(self._pid(rid), f"{self._repo_tag(rid)}{body}")

    def _escalate(self, rid: str, reason: str) -> None:
        reason = self._clean(reason)
        self._record_transition(rid, AIState.BLOCKED)
        self.store.set_state(rid, AIState.BLOCKED, last_error=reason[:1000])
        pid = self._pid(rid)
        self._push_remote_state(rid)
        self.provider.add_comment(
            pid, f"{self._repo_tag(rid)}**Escalated to a human.**\n\n{reason}")

    def _clean(self, text: str) -> str:
        """Scrub credentials out of text on its way to a ticket or the store.

        `extra` is every token this runner injects into a subprocess
        environment, gathered across all configured vcs profiles: those are
        the exact values most likely to come back out of a failing `git push`
        or `gh` call, and an exact-value match catches formats the shape
        patterns in `redact` do not know about.
        """
        return redact(text, self._known_secrets())

    def _known_secrets(self) -> list[str]:
        if self._secrets is None:
            found: list[str] = []
            for repo in self.source.repos:
                try:
                    found.extend(self._env_for(repo).values())
                except Exception:  # noqa: BLE001
                    # A profile with a missing token env var is `doctor`'s
                    # problem. Redaction must never be the thing that raises.
                    continue
            self._secrets = sorted(set(found), key=len, reverse=True)
        return self._secrets


def _matches_any(body: str, keywords: list[str]) -> bool:
    """Whole word/phrase match against `body` (already lowercased).

    A plain substring test reads "no" out of "now" — "approved, go ahead
    now" would then look rejected because of the `reject` keyword list's
    "no", flipping a clean approval into a rejection. `\\b` anchors each
    keyword (including multi-word ones like "go ahead") to a real word
    boundary so this can't happen.
    """
    return any(re.search(rf"\b{re.escape(k)}\b", body) for k in keywords)


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


def _fmt_analyst_questions(r: AnalysisResult) -> str:
    qs = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(r.questions))
    return (f"**I read the code and hit something the ticket doesn't settle.**\n\n"
            f"{r.problem}\n\n{qs}\n\n"
            f"_Reply in this thread and I'll re-read the ticket on the next run._")


def _fmt_analysis(r: AnalysisResult, shadow: bool) -> str:
    steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(r.plan))
    ac = "\n".join(f"- {a}" for a in r.acceptance_criteria) or "- (none)"
    risks = "\n".join(f"- {x}" for x in r.risks) or "- (none flagged)"
    files = "\n".join(f"- `{f}`" for f in r.files_of_interest) or "- (none listed)"
    tail = ("\n\n_Shadow mode: no code will be written._" if shadow else
            "\n\n_Reply `approved` to let me start, or correct the plan and I'll re-read it._")
    return (f"**Plan** (written after reading the repository)\n\n"
            f"{r.problem}\n\n**Approach**\n{r.approach}\n\n"
            f"**Steps**\n{steps}\n\n**Acceptance criteria**\n{ac}\n\n"
            f"**Files I expect to touch**\n{files}\n\n**Risks**\n{risks}{tail}")


def _work_prompt(task: Task, repo: dict[str, Any], meta: dict[str, Any],
                 brain_block: str) -> str:
    steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(meta.get("plan", [])))
    criteria = "\n".join(f"- {c}" for c in meta.get("acceptance_criteria", []))
    return f"""You are on a fresh branch in an isolated worktree. Work only within this directory.

{task.as_prompt_block()}
{brain_block}
=== AGREED PLAN ===
{meta.get('approach', '')}

{steps or '(no steps recorded)'}

=== ACCEPTANCE CRITERIA ===
These are what a reviewer will check your diff against, one by one.
{criteria or '- (none recorded)'}

Rules:
- Follow the conventions already present in this codebase; read before you write.
- Make the smallest change that satisfies the ticket. No refactors, no drive-by fixes.
- Add or update tests. Run the test command: {repo.get('test', '(none configured)')}
- Do NOT modify CI config, dependency lockfiles, secrets, or .env files unless the ticket explicitly asks.
- Do NOT commit, push, or open a PR — the runner handles that.
- If the ticket turns out to be ambiguous or unsafe, make no changes and explain why.

Finish by printing a short summary: what you changed, which files, how you verified it,
and any point where you deviated from the plan.
"""


def _fix_prompt(task_key: str, st: dict[str, Any], feedback: list[str], logs: str,
                brain_block: str) -> str:
    fb = "\n".join(f"- {f}" for f in feedback) or "(none)"
    return f"""You opened PR {st['url']} for {task_key}. It needs another pass.

Failing checks: {', '.join(st['failed_checks']) or 'none'}
{brain_block}
Reviewer feedback:
{fb}

CI output (tail):
{logs}

Address the failures and the reviewer feedback. Same rules as before: smallest
possible change, no unrelated edits, do not commit or push. If a piece of feedback
requires a product decision you cannot make, leave it and say so explicitly."""


def _pr_body(task: Task, meta: dict[str, Any], r: ReviewResult,
             diffstat: str) -> str:
    checks = "\n".join(
        f"- [{'x' if c.get('pass') else ' '}] {c.get('criterion', '')}"
        f"{chr(10) + '      ' + str(c.get('evidence', '')) if c.get('evidence') else ''}"
        for c in r.criteria_results) or "- (no criteria were recorded)"
    notes = "\n".join(f"- {s}" for s in r.suggestions)
    return (f"Implements [{task.key}]({task.url}) — {task.title}\n\n"
            f"## Problem\n{meta.get('problem') or task.title}\n\n"
            f"## Approach\n{meta.get('approach', '(not recorded)')}\n\n"
            f"## What changed\n{meta.get('impl_summary', '')[:4000]}\n\n"
            f"## Acceptance criteria\n{checks}\n\n"
            + (f"## Reviewer suggestions (non-blocking)\n{notes}\n\n" if notes else "")
            + f"## Diffstat\n```\n{diffstat}\n```\n\n"
            f"---\nReviewed by an independent agent pass before this PR was opened. "
            f"Verdict: `{r.verdict}`. Review carefully before merging.")


def _fmt_review(r: ReviewResult) -> str:
    rows = "\n".join(
        f"- {'PASS' if c.get('pass') else 'FAIL'} — {c.get('criterion', '')}"
        f"{chr(10) + '  ' + str(c.get('evidence', '')) if c.get('evidence') else ''}"
        for c in r.criteria_results) or "- (no criteria were recorded)"
    blockers = "\n".join(f"- {f}" for f in r.blocking_findings) or "- (none)"
    notes = "\n".join(f"- {s}" for s in r.suggestions) or "- (none)"
    return (f"**Review: `{r.verdict}`**\n\n{r.summary}\n\n"
            f"**Acceptance criteria**\n{rows}\n\n"
            f"**Blocking**\n{blockers}\n\n**Suggestions**\n{notes}")


def _revision_prompt(task: Task, repo: dict[str, Any], meta: dict[str, Any],
                     revision: dict[str, Any], brain_block: str) -> str:
    findings = "\n".join(f"- {f}" for f in revision.get("findings", [])) or "(none)"
    criteria = "\n".join(f"- {c}" for c in meta.get("acceptance_criteria", [])) or "(none)"
    return (
        f"A reviewer looked at your branch and asked for changes. You are in the "
        f"same worktree, on the same branch.\n\n"
        f"{task.as_prompt_block()}\n{brain_block}\n"
        f"=== ACCEPTANCE CRITERIA ===\n{criteria}\n\n"
        f"=== REVIEWER SUMMARY ===\n{revision.get('summary', '')}\n\n"
        f"=== BLOCKING FINDINGS ===\n{findings}\n\n"
        f"Address every blocking finding. Same rules as before: smallest possible "
        f"change, no unrelated edits, no refactors, do not commit or push. "
        f"Run the test command: {repo.get('test', '(none configured)')}\n\n"
        f"If a finding is wrong, say so plainly in your summary and explain why "
        f"rather than changing code to satisfy it.\n\n"
        f"Finish by printing a short summary of what you changed and how you "
        f"verified it."
    )
