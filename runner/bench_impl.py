"""Implementation benchmark: replay merged tickets under another model.

A case is one routed row whose PR merged: the repo, the commit its branch
forked from (`meta.base_sha`), the ticket as triage last saw it before the
work started, and the plan and acceptance criteria the implementer was given.
A trial is one `work()` run on that case under one model, with the tick's own
implement prompt and tool allowlist, in a scratch repository holding the
base commit's history and nothing later. The runner pushes nothing from it,
comments on nothing and reviews nothing; it has no remote, and nothing here
builds a provider.

Each trial is graded twice, and neither grade stands in for the other:

- tests  The repo's `test` command, run on the trial's checkout. It only
         runs where the change that merged passes the same command in a
         scratch clone of its own. A suite that fails on the merged code
         here (missing services, an unbuilt dependency) says nothing about
         the trial.
- judge  A tool-less `reason()` call. It reads the ticket, the criteria, the
         plan, the merged diff and the trial's diff, and answers correct,
         partial or incorrect. The merged diff is one right answer, not the
         only one. Diffs that do not fit the prompt ceiling are never cut:
         the verdict is insufficient_evidence and nothing is called.

A trial is correct when the judge says so and the tests, where they ran,
passed. Operator-run only (`deputy bench implement`), never in the tick.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bench, brain, vcs
from .agents.base import CodingAgent
from .agents.claude_code import _kill_tree
from .jsonio import parse_json_object
from .models import Spend, Task, agent_log_name, wilson
from .redact import redact

log = logging.getLogger(__name__)

SUITE = "implement"
CORRECT, PARTIAL, INCORRECT = "correct", "partial", "incorrect"
LABELS = (CORRECT, PARTIAL, INCORRECT)
INSUFFICIENT = "insufficient_evidence"
NO_CHANGES = "no changes"
# Wall-clock cap on one run of a repo's setup or test command.
TEST_TIMEOUT = 1800

JUDGE_SYSTEM = (
    "You grade a code change that an autonomous coding agent made for a ticket. "
    "You have no tools and no access to any files, repository, shell or "
    "environment. Judge only from the ticket, the diffs and the context in the "
    "user's message. When they do not show enough to decide, say so with the "
    "insufficient_evidence label instead of guessing. Reply in the format the "
    "message asks for.")


# --- the cases -------------------------------------------------------------
@dataclass
class Case:
    """One merged ticket in one repo, as the work on it started."""
    name: str                     # {task_key}__{source}__{repo_key}
    row: dict[str, Any]           # the routed row, now `done`
    repo: dict[str, Any]          # the repo as today's config has it
    task: Task                    # as triage last saw it before the work began
    meta: dict[str, Any]
    started: int                  # the ledger time of the implement run that merged
    cost_usd: float               # what that run cost: the estimate for a trial

    @property
    def row_id(self) -> str:
        return str(self.row["task_id"])

    @property
    def task_key(self) -> str:
        return str(self.row.get("task_key") or "")

    @property
    def source(self) -> str:
        return str(self.row.get("source") or "")

    @property
    def repo_key(self) -> str:
        return str(self.row.get("repo_key") or "")

    @property
    def base_sha(self) -> str:
        return str(self.meta.get("base_sha") or "")

    @property
    def pr_number(self) -> int:
        return int(self.row.get("pr_number") or 0)


def _snapshot(log_root: Path, row: dict[str, Any], placeholder: str, n: int,
              at: int) -> dict[str, Any] | None:
    """The `n`th triage snapshot of this ticket, if it is the one the ledger
    says ran at `at` and it routed the ticket to this row's repo."""
    name = agent_log_name(str(row.get("task_key") or ""), str(row.get("source") or ""),
                          f"triage-{n}")
    try:
        snap = json.loads((Path(log_root) / "triage" / name).with_suffix(".json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    result = snap.get("result") if isinstance(snap, dict) else None
    if (not isinstance(result, dict) or snap.get("row_id") != placeholder
            or abs(int(snap.get("ts") or 0) - at) > bench.SNAPSHOT_SLACK
            or result.get("decision") != "execute"
            or row.get("repo_key") not in (result.get("repo_keys") or [])):
        return None
    return snap


def load(store: Any, log_root: Path, repos_now: dict[tuple[str, str], dict[str, Any]], *,
         sources: list[str] | None = None, repos: list[str] | None = None
         ) -> tuple[list[Case], Counter[str]]:
    """Every merged row a trial can replay, newest merge first, and why the
    rest cannot.

    The attempt that merged is the last triage run before the merge that
    routed the ticket, and the first implement run after it: a ticket a
    human reset has older runs under the same row id, for a plan that never
    merged.
    """
    cases: list[Case] = []
    left_out: Counter[str] = Counter()
    for row in store.merged_rows():
        src, rk = str(row.get("source") or ""), str(row.get("repo_key") or "")
        if (sources and src not in sources) or (repos and rk not in repos):
            continue
        repo = repos_now.get((src, rk))
        if repo is None or not repo.get("path"):
            left_out["repo gone from config"] += 1
            continue
        try:
            meta = json.loads(row.get("meta") or "{}")
        except ValueError:
            meta = {}
        if not meta.get("base_sha"):
            left_out["no base commit"] += 1
            continue
        if not row.get("pr_number"):
            left_out["no PR number"] += 1
            continue
        placeholder = str(row["task_id"]).split("#", 1)[0]
        merged = int(row["merged_id"])
        routed = [(n, t) for n, t in enumerate(store.row_runs(placeholder, "triage"), 1)
                  if t["id"] < merged and not str(t["detail"] or "").startswith("failed")]
        if not routed:
            left_out["no triage run"] += 1
            continue
        n, tri = routed[-1]
        runs = [r for r in store.row_runs(row["task_id"], "implement")
                if tri["id"] < r["id"] < merged]
        if not runs:
            left_out["no implement run"] += 1
            continue
        snap = _snapshot(log_root, row, placeholder, n, int(tri["ts"]))
        if snap is None:
            left_out["no ticket snapshot"] += 1
            continue
        # Sources may share ticket and repo keys, and the name is also a
        # directory, so it carries all three, sanitised as `agent_log_name` does.
        name = "__".join(str(x).replace("/", "_")
                         for x in (row.get("task_key") or "", src, rk))
        cases.append(Case(name=name, row=row,
                          repo=dict(repo, key=rk), task=bench._task(snap), meta=meta,
                          started=int(runs[0]["ts"]),
                          cost_usd=float(runs[0]["cost_usd"] or 0)))
    return cases, left_out


# --- variants --------------------------------------------------------------
@dataclass
class Variant:
    """One configuration under test: the implementer's model."""
    model: str | None             # None: the agent's own model
    label: str

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "model": self.model}


def variants(models: list[str], *, default_model: str | None) -> list[Variant]:
    """The configured model first, then every model asked for, without repeats."""
    out: list[Variant] = []
    for m in [None, *models]:
        shown = m or default_model or "default"
        if all(v.label != shown for v in out):
            out.append(Variant(model=m, label=shown))
    return out


# --- grading ---------------------------------------------------------------
@dataclass
class Reference:
    """What merged for a case, and whether the tests can grade against it."""
    diff: str                     # redacted
    tests: int | None             # the test command on the merged change: 1, 0, None
    note: str                     # why `tests` is not 1, when it is not


def _shell(cmd: str, wt: Path, env: dict[str, str]) -> int | None:
    """Exit code of `bash -lc cmd` in `wt`, or None when it timed out.

    In its own process group, signalled whole on a timeout, as the agent is:
    `subprocess.run(timeout=)` kills the shell only, and a test runner's
    workers would go on running, and writing, after the trial had moved on
    and deleted the checkout under them. Only on a timeout: once `wait()`
    has reaped the shell its pid may belong to someone else, and looking up
    a group by it could signal an unrelated one.
    """
    proc = subprocess.Popen(["bash", "-lc", cmd], cwd=wt, env={**os.environ, **env},
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return proc.wait(timeout=TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return None


def _setup(repo: dict[str, Any], wt: Path, env: dict[str, str]) -> None:
    # As `_execute` does it: a failed setup is the agent's to notice, not a stop.
    if repo.get("setup"):
        _shell(str(repo["setup"]), wt, env)


def _tests(repo: dict[str, Any], wt: Path, env: dict[str, str]) -> int:
    return int(_shell(str(repo["test"]), wt, env) == 0)    # a timeout is a failure


def reference(case: Case, root: Path, *, env: dict[str, str],
              secrets: list[str]) -> Reference:
    """Fetch what merged and, where there is a test command, run it there.

    Raises when GitHub cannot give the diff: a case with no answer is not
    replayed.
    """
    raw = vcs.pr_diff(Path(str(case.repo["path"])).expanduser(), case.pr_number, env=env)
    diff = redact(raw, secrets)
    if not case.repo.get("test"):
        return Reference(diff, None, "no test command is configured")
    wt = root / "reference"
    patch = root / "reference.patch"           # beside the checkout, not in it
    try:
        vcs.scratch_checkout(case.repo, case.base_sha, wt, env=env)
        patch.write_text(raw, encoding="utf-8")
        if vcs.run(["git", "apply", str(patch)], cwd=wt, check=False,
                   env=env).returncode != 0:
            return Reference(diff, None, "the merged diff does not apply at the base commit")
        _setup(case.repo, wt, env)
        if _tests(case.repo, wt, env):
            return Reference(diff, 1, "")
        return Reference(diff, 0, "the merged change fails the test command here too")
    finally:
        shutil.rmtree(wt, ignore_errors=True)
        patch.unlink(missing_ok=True)


def _diff(wt: Path, base: str, env: dict[str, str]) -> tuple[str, dict[str, int]]:
    """Everything the trial changed against the base, committed or not."""
    vcs.run(["git", "add", "-A"], cwd=wt, env=env)
    diff = vcs.run(["git", "diff", "--cached", base], cwd=wt, env=env).stdout
    stat = {"additions": 0, "deletions": 0, "files": 0}
    for line in vcs.run(["git", "diff", "--cached", "--numstat", base],
                        cwd=wt, env=env).stdout.splitlines():
        added, deleted, _ = (line.split("\t", 2) + ["", ""])[:3]
        stat["files"] += 1
        stat["additions"] += int(added) if added.isdigit() else 0
        stat["deletions"] += int(deleted) if deleted.isdigit() else 0
    return diff, stat


def _lines(items: Any, numbered: bool = False) -> str:
    items = [str(x) for x in items or []]
    if not items:
        return "(none recorded)"
    return "\n".join(f"{i}. {x}" if numbered else f"- {x}" for i, x in enumerate(items, 1))


def judge_prompt(case: Case, ref: Reference, diff: str, tests: int | None) -> str:
    test = str(case.repo.get("test") or "")
    tested = {1: f"The repository's test command (`{test}`) passed on the candidate.",
              0: f"The repository's test command (`{test}`) failed on the candidate."
              }.get(tests, f"The tests were not run on the candidate: {ref.note}.")
    return f"""Grade the candidate change below against its ticket.

=== TICKET ===
{case.task.as_prompt_block()}
=== ACCEPTANCE CRITERIA ===
{_lines(case.meta.get("acceptance_criteria"))}

=== AGREED PLAN ===
{case.meta.get("approach") or ""}
{_lines(case.meta.get("plan"), numbered=True)}

=== REFERENCE: THE CHANGE THAT MERGED ===
A person reviewed and merged this change for the same ticket. It is one
correct answer, not the only one: a candidate that does the same job another
way is still correct.

{ref.diff}
=== CANDIDATE: THE CHANGE TO GRADE ===
{diff}
=== TESTS ===
{tested}

Labels:
- correct: meets every acceptance criterion, with nothing a reviewer would send back.
- partial: meets some of the criteria, or meets them with a defect a reviewer would send back.
- incorrect: misses the point of the ticket, breaks something, or changes unrelated code.
- insufficient_evidence: the diffs do not show enough to decide.

Reply with a single JSON object and nothing else:
{{"label": "correct" | "partial" | "incorrect" | "insufficient_evidence",
 "reasoning": "two or three sentences"}}"""


def judge(agent: CodingAgent, case: Case, ref: Reference, diff: str, tests: int | None, *,
          model: str | None, max_bytes: int) -> tuple[str | None, str, Spend]:
    """(label, reasoning, spend). The label is None when the judge failed."""
    prompt = judge_prompt(case, ref, diff, tests)
    size = len(prompt.encode("utf-8"))
    if size > max_bytes:
        # Never cut a diff: half a change buys a confident verdict on the
        # wrong code, as half a transcript does for a scorer.
        return INSUFFICIENT, (f"the diffs are too large to judge in one prompt "
                              f"({size} bytes, ceiling {max_bytes}); nothing was cut "
                              f"and nothing was called"), Spend()
    res = agent.reason(prompt, system_prompt=JUDGE_SYSTEM,
                       **({"model": model} if model else {}))
    spend = Spend.of(res)
    if not res.ok:
        return None, f"judge run failed: {res.error}", spend
    try:
        data = parse_json_object(res.text)
    except ValueError as exc:
        return None, f"unparseable verdict: {str(exc)[:300]}", spend
    label = str(data.get("label") or "")
    if label not in (*LABELS, INSUFFICIENT):
        return None, f"the judge answered with an unknown label {label[:40]!r}", spend
    return label, str(data.get("reasoning") or ""), spend


# --- running ---------------------------------------------------------------
class _Stop(Exception):
    def __init__(self, kind: str, note: str) -> None:
        super().__init__(note)
        self.kind, self.note = kind, note


def trial(agent: CodingAgent, case: Case, variant: Variant, ref: Reference, *,
          checkout: Path, log_file: Path, log_rel: str, brain_root: Path,
          work_prompt: Callable[[Task, dict[str, Any], dict[str, Any], str], str],
          judge_model: str | None, max_prompt_bytes: int, env: dict[str, str],
          secrets: list[str], keep: bool,
          on_work: Callable[[Spend, bool], None] | None = None,
          before_judge: Callable[[], None] | None = None
          ) -> tuple[dict[str, Any], Spend, Spend]:
    """One trial: the variant implements one merged ticket, graded.

    Returns the trial's fields and the spend of its agent run and its judge
    call, apart, since each has its own ledger line. `on_work` gets the agent
    run's spend the moment it returns, before the tests and the judge: a ^C
    there must not lose dollars the daily limits have to see. `before_judge`
    runs before the judge call, which is an agent run too, and raises `_Stop`
    to end the run there.
    """
    fields: dict[str, Any] = {
        "variant": variant.label, "case_name": case.name, "task_id": case.row_id,
        "task_key": case.task_key, "why": "merged", "decision": None, "correct": None,
        "reasoning": None, "error": None, "tests": None, "ref_tests": ref.tests,
        "turns": 0, "log": log_rel, "diff": None,
        "additions": None, "deletions": None, "files": None}
    work = judged = Spend()
    try:
        vcs.scratch_checkout(case.repo, case.base_sha, checkout, env=env)
        _setup(case.repo, checkout, env)
        # The brain as it stood when the work began: a note the approving
        # review wrote about this very ticket would hand the trial its answer.
        notes = brain.as_prompt_block(
            brain.brain_path(brain_root, case.source, case.repo_key),
            before=dt.date.fromtimestamp(case.started))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        res = agent.work(work_prompt(case.task, case.repo, case.meta, notes), checkout,
                         log_path=log_file, model=variant.model)
        work = Spend.of(res)
        if on_work:
            on_work(work, res.ok)
        fields["turns"] = res.num_turns
        if not res.ok:
            fields["error"] = redact(f"agent run failed: {res.error}", secrets)[:1000]
            return fields, work, judged
        diff, stat = _diff(checkout, case.base_sha, env)
        diff = redact(diff, secrets)
        path = log_file.with_suffix(".diff")
        path.write_text(diff, encoding="utf-8")
        fields.update(stat, diff=str(Path(log_rel).with_suffix(".diff")))
        if not diff.strip():
            fields.update(decision=NO_CHANGES, correct=0)
            return fields, work, judged
        if ref.tests == 1:
            fields["tests"] = _tests(case.repo, checkout, env)
        if before_judge:
            before_judge()
        label, reasoning, judged = judge(agent, case, ref, diff, fields["tests"],
                                         model=judge_model, max_bytes=max_prompt_bytes)
        reasoning = redact(reasoning, secrets)
        if label is None:
            fields["error"] = reasoning[:1000]
        else:
            fields.update(decision=label, reasoning=reasoning[:2000])
        if label in LABELS:
            fields["correct"] = int(label == CORRECT and fields["tests"] != 0)
        elif fields["tests"] == 0:
            fields["correct"] = 0       # the judge could not say; the tests could
    except _Stop:
        raise
    except Exception as exc:  # noqa: BLE001 -- one bad trial must not end the run
        log.warning("bench trial %s / %s failed", case.name, variant.label, exc_info=True)
        fields["error"] = redact(f"{type(exc).__name__}: {exc}", secrets)[:1000]
        if fields["tests"] == 0:
            fields["correct"] = 0
    finally:
        if not keep:
            shutil.rmtree(checkout, ignore_errors=True)
    return fields, work, judged


def run(agent: CodingAgent, store: Any, cases: list[Case], variants_: list[Variant], *,
        repeat: int, max_usd: float, limits_check: Callable[[], str | None],
        work_prompt: Callable[[Task, dict[str, Any], dict[str, Any], str], str],
        profile_env: Callable[[dict[str, Any]], dict[str, str]],
        work_root: Path, log_root: Path, brain_root: Path, judge_model: str | None,
        max_prompt_bytes: int, secrets: list[str], keep: bool = False,
        on_trial: Callable[[Case, dict[str, Any], Spend], None] | None = None,
        on_skip: Callable[[Case, str], None] | None = None) -> tuple[int, str | None]:
    """Replay `cases` under every variant and record each trial.

    Returns the run id and why it stopped early: "cap", "limits",
    "interrupted", or None when it ran to the end.

    The cap is checked before each ticket, against what has been spent plus
    what that ticket's own implement run cost, times the trials it is about
    to get. The daily limits are checked before every agent run, as the tick
    checks them: a single trial costs dollars, not cents. A stop inside a
    ticket leaves it half-compared, and `summarize` leaves such a ticket out.
    """
    run_id = store.start_bench(SUITE, [v.as_dict() for v in variants_], len(cases))
    per_case = len(variants_) * max(1, repeat)
    root = Path(work_root) / "bench" / str(run_id)
    spent, stop, note = 0.0, None, None
    skipped: Counter[str] = Counter()
    try:
        for i, case in enumerate(cases):
            ahead = case.cost_usd * per_case
            if spent >= max_usd or (ahead and spent + ahead > max_usd):
                raise _Stop("cap", f"stopped at the ${max_usd:.2f} cap after {i} of "
                                   f"{len(cases)} tickets (${spent:.2f} spent; the next "
                                   f"was estimated at ${ahead:.2f})")
            hit = limits_check()
            if hit:
                raise _Stop("limits", f"{hit}; stopped after {i} of {len(cases)} tickets")
            try:
                env = profile_env(case.repo)
                ref = reference(case, root / case.name, env=env, secrets=secrets)
            except Exception as exc:  # noqa: BLE001 -- credentials, GitHub, git: skip it
                skipped["reference unavailable"] += 1
                if on_skip:
                    on_skip(case, redact(str(exc), secrets)[:300])
                continue

            def gate(i: int = i) -> None:
                # Before every agent run, the judge's included, as the tick does.
                hit = limits_check()
                if hit:
                    raise _Stop("limits", f"{hit}; stopped partway through ticket "
                                          f"{i + 1} of {len(cases)}, which is left "
                                          f"out of the comparison")

            for vi, v in enumerate(variants_, 1):
                for rep in range(1, max(1, repeat) + 1):
                    gate()
                    # The source is a directory: two sources may share both keys.
                    name = agent_log_name(case.task_key, case.repo_key, f"v{vi}-{rep}")
                    log_rel = f"bench/{run_id}/{case.source.replace('/', '_')}/{name}"
                    head = f"#{run_id} {case.name} {v.label}"

                    def ran(work: Spend, ok: bool, head: str = head,
                            log_rel: str = log_rel) -> None:
                        store.record("bench", None,
                                     f"{head}: implement {'ok' if ok else 'failed'}",
                                     work.cost_usd, work.tokens, log=log_rel,
                                     **work.run_fields())

                    fields, work, judged = trial(
                        agent, case, v, ref, checkout=root / case.name / f"v{vi}-{rep}",
                        log_file=Path(log_root) / log_rel, log_rel=log_rel,
                        brain_root=brain_root, work_prompt=work_prompt,
                        judge_model=judge_model, max_prompt_bytes=max_prompt_bytes,
                        env=env, secrets=secrets, keep=keep, on_work=ran,
                        before_judge=gate)
                    if judged.cost_usd or judged.tokens:
                        store.record("bench", None,
                                     f"{head}: judge {fields['decision'] or 'failed'}",
                                     judged.cost_usd, judged.tokens, **judged.run_fields())
                    total = work + judged
                    store.record_trial(run_id, rep=rep, cost_usd=total.cost_usd,
                                       tokens=total.tokens, duration_ms=work.duration_ms,
                                       model=",".join(work.models) or None, **fields)
                    spent += total.cost_usd
                    if on_trial:
                        on_trial(case, fields, total)
    except _Stop as s:
        stop, note = s.kind, s.note
    except KeyboardInterrupt:
        stop = note = "interrupted"
    finally:
        if skipped:
            extra = ", ".join(f"{n} skipped: {k}" for k, n in skipped.items())
            note = f"{note}; {extra}" if note else extra
        store.finish_bench(run_id, note)
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
    return run_id, stop


# --- reading results -------------------------------------------------------
def complete(trials: list[dict[str, Any]], labels: list[str]) -> set[str]:
    """Tickets every variant finished as many trials on as any other did.

    A run stopped inside a ticket, by the daily limits or a ^C, has some
    variants' trials on it and not others'. Counting it would compare the
    variants on different tickets.
    """
    n = Counter((t["case_name"], t["variant"]) for t in trials)
    names = dict.fromkeys(t["case_name"] for t in trials)
    return {c for c in names if len({n[(c, v)] for v in labels}) == 1 and n[(c, labels[0])]}


def summarize(trials: list[dict[str, Any]],
              labels: list[str] | None = None) -> list[dict[str, Any]]:
    """Per variant, in the order they ran: correctness, tests, cost.

    `labels` are the run's variants (`bench_runs.variants`); without them,
    the variants that have trials. Correctness is over trials with a grade;
    a failed agent run or judge call is counted beside it and never in it,
    unless the tests failed, which grades the trial wrong without a judge.
    Every rate carries its n and its 95% range.
    """
    labels = labels or list(dict.fromkeys(t["variant"] for t in trials))
    whole = complete(trials, labels)
    by = {v: {"variant": v, "trials": 0, "errors": 0, "n": 0, "correct": 0,
              "partial": 0, "incorrect": 0, "no_changes": 0, "insufficient": 0,
              "tests_n": 0, "tests_ok": 0, "cost_usd": 0.0, "tokens": 0,
              "duration_ms": 0, "turns": 0, "models": set(), "left_out": 0}
          for v in labels}
    for t in trials:
        v = by.get(t["variant"])
        if v is None:
            continue
        if t["case_name"] not in whole:
            v["left_out"] += 1
            continue
        v["trials"] += 1
        v["cost_usd"] += float(t.get("cost_usd") or 0)
        v["tokens"] += int(t.get("tokens") or 0)
        v["duration_ms"] += int(t.get("duration_ms") or 0)
        v["turns"] += int(t.get("turns") or 0)
        v["models"].update(m for m in str(t.get("model") or "").split(",") if m)
        decision = t.get("decision")
        if decision is None:
            v["errors"] += 1
        elif decision == NO_CHANGES:
            v["no_changes"] += 1
        elif decision == INSUFFICIENT:
            v["insufficient"] += 1
        elif decision in (PARTIAL, INCORRECT):
            v[decision] += 1
        if t.get("tests") is not None:
            v["tests_n"] += 1
            v["tests_ok"] += int(t["tests"])
        if t.get("correct") is not None:
            v["n"] += 1
            v["correct"] += int(t["correct"])
    out = []
    for v in by.values():
        ci = wilson(v["correct"], v["n"])
        k = v["trials"]
        out.append({
            **v, "models": sorted(v["models"]), "cost_usd": round(v["cost_usd"], 6),
            "rate": v["correct"] / v["n"] if v["n"] else None,
            "low": ci[0] if ci else None, "high": ci[1] if ci else None,
            "tests_rate": v["tests_ok"] / v["tests_n"] if v["tests_n"] else None,
            "avg_cost_usd": v["cost_usd"] / k if k else 0.0,
            "avg_tokens": v["tokens"] / k if k else 0.0,
            "avg_minutes": v["duration_ms"] / k / 60000 if k else 0.0,
        })
    return out


def misses(trials: list[dict[str, Any]],
           labels: list[str] | None = None) -> list[dict[str, Any]]:
    """Trials graded wrong: by the judge, by the tests, or by changing nothing.

    Only on tickets `summarize` compares, so the list never shows a ticket
    the numbers beside it leave out.
    """
    whole = complete(trials, labels or list(dict.fromkeys(t["variant"] for t in trials)))
    return [t for t in trials if t.get("correct") == 0 and t["case_name"] in whole]
