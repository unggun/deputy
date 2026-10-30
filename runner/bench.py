"""Triage benchmark: replay past triage runs under another model or prompt.

Triage is one isolated, tool-less turn, so replaying fifty past tickets costs
cents, and history already holds the answers. The inputs are the snapshots
`Pipeline._snapshot_triage` kept -- the ticket and its comments as triage saw
them, not as they read today, which would include the replies written after
it -- and the answer key is what happened next, read from the ledger:

- merged    triage said execute and every repo it routed to merged: execute
            was right, and so were those repos.
- answered  triage asked and a human answered: the question was needed.
- reset     triage said human_only and a human set the ticket back to new:
            anything but human_only.
- stood     triage said human_only and nobody reset it in `SETTLED_AFTER`.

A human_only only counts as either once the row actually reached human-only.

Everything else is left out and counted: a question nobody answered, a run
still in flight, an execute that ended blocked or was triaged again after a
rejected plan. History does not say what the right call was there, and a
benchmark that guessed would grade the guess.

Operator-run only (`deputy bench`), never in the tick. Every call goes through
`triage.triage()`, the tick's own function, so the denylist, the override tag
and the post-conditions are the ones production applies; only the model and
the prompt text vary.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import triage
from .agents.base import CodingAgent
from .jsonio import StationError
from .models import Comment, Spend, Task, wilson
from .redact import redact

log = logging.getLogger(__name__)

SUITE = "triage"
BUILTIN = "builtin"
# A human_only verdict nobody reset counts as accepted once it has stood this
# long. Younger than that, silence may only mean nobody has looked yet.
SETTLED_AFTER = 7 * 86400
# How far a snapshot's time may sit from its ledger line. They are written a
# moment apart; a bigger gap means the ledger belongs to another history (a
# recreated database), and its outcomes are not this snapshot's.
SNAPSHOT_SLACK = 600

DENYLISTED = "the denylist decides it"


# --- the answer key ------------------------------------------------------
@dataclass
class Case:
    """One past triage run, with what history says the right call was."""
    name: str                     # the snapshot's file stem
    snap: dict[str, Any]
    expected: list[str]           # decisions history accepts
    repos: list[str] | None       # repos that merged, when every routed one did
    why: str                      # merged | merged in part | answered | reset | stood

    @property
    def row_id(self) -> str:
        return str(self.snap.get("row_id") or "")

    @property
    def task_key(self) -> str:
        return str((self.snap.get("task") or {}).get("key") or "")

    @property
    def source(self) -> str:
        return str(self.snap.get("source") or "")

    @property
    def recorded(self) -> str | None:
        return (self.snap.get("result") or {}).get("decision")

    @property
    def candidates(self) -> int:
        return len(self.snap.get("repos") or [])


def _moves(events: Iterable[dict[str, Any]]) -> list[tuple[str, str, str]]:
    out = []
    for e in events:
        old, sep, new = str(e.get("detail") or "").partition("->")
        if e["kind"] == "state" and sep:
            out.append((str(e["task_id"]), old, new))
    return out


def answer_key(history: list[dict[str, Any]], snap: dict[str, Any], *,
               now: int, repo_of: Callable[[str], str | None]
               ) -> tuple[list[str] | None, list[str] | None, str]:
    """(decisions history accepts, repos that merged, why) for one snapshot.

    The first is None when history cannot say, and `why` then says why the
    snapshot is left out. Read from the events between this triage run and
    the ticket's next one: they are this run's consequences, and nothing
    later is.
    """
    result = snap.get("result") or {}
    decision = result.get("decision")
    if "policy-denylist" in (result.get("risk_flags") or []):
        return None, None, DENYLISTED
    if not decision:
        return None, None, "triage failed"
    runs = [e for e in history if e["kind"] == "triage"]
    n = int(snap.get("run") or 0)
    if not 1 <= n <= len(runs) or abs(int(runs[n - 1]["ts"]) - int(snap.get("ts") or 0)) > SNAPSHOT_SLACK:
        return None, None, "no history"
    start = runs[n - 1]["id"]
    nxt = runs[n]["id"] if n < len(runs) else None
    moves = _moves(e for e in history
                   if e["id"] > start and (nxt is None or e["id"] < nxt))
    placeholder = str(snap.get("row_id") or "")

    if decision == "clarify":
        answered = any(t == placeholder and old == "needs-input" and new == "new"
                       for t, old, new in moves)
        return (["clarify"], None, "answered") if nxt and answered \
            else (None, None, "nobody answered")
    if decision == "human_only":
        # The verdict has to have landed. A verdict comment that failed to
        # post leaves the row at `new`, and the next tick triages it again
        # with no human involved -- which is not a reset.
        if not any(t == placeholder and new == "human-only" for t, _, new in moves):
            return None, None, "verdict not applied"
        # human-only is terminal, so the only road to another triage run is
        # a human setting AI State back to new.
        if nxt:
            return ["execute", "clarify"], None, "reset"
        if now - int(snap.get("ts") or 0) >= SETTLED_AFTER:
            return ["human_only"], None, "stood"
        return None, None, "too recent"
    if decision != "execute":
        return None, None, "unknown decision"
    if nxt:
        # A rejected plan, or an analyst's question: the call to execute may
        # have been right and the plan wrong, and history cannot tell which.
        return None, None, "triaged again"
    final: dict[str, str] = {}
    for t, _, new in moves:
        final[t] = new
    if not final:
        return None, None, "still in flight"
    terminal = ("done", "blocked", "human-only")
    if any(st not in terminal for st in final.values()):
        return None, None, "still in flight"
    if all(st == "done" for st in final.values()):
        repos = [repo_of(t) for t in final]
        return ["execute"], (sorted({r for r in repos if r})
                             if all(repos) else None), "merged"
    if any(st == "done" for st in final.values()):
        return ["execute"], None, "merged in part"
    return None, None, "ended blocked"


def _task(snap: dict[str, Any]) -> Task:
    t = snap.get("task") or {}
    return Task(id=str(t.get("id") or ""), key=str(t.get("key") or ""),
                title=str(t.get("title") or ""),
                description=str(t.get("description") or ""),
                status=str(t.get("status") or ""), url=str(t.get("url") or ""),
                list_id=str(t.get("list_id") or ""),
                list_name=str(t.get("list_name") or ""),
                tags=[str(x) for x in t.get("tags") or []],
                assignee_ids=[], updated_at=0)


def _comments(snap: dict[str, Any]) -> list[Comment]:
    return [Comment(id="", author=str(c.get("author") or ""), author_id="",
                    text=str(c.get("text") or ""),
                    created_at=int(c.get("created_at") or 0),
                    is_bot=bool(c.get("is_bot")))
            for c in snap.get("comments") or []]


def _denylisted(snap: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Would today's denylist stop this ticket before the model saw it?

    Those tickets are left out: no model or prompt decides them, so they
    would only add the same answer to every variant.
    """
    task = _task(snap)
    tag = str(policy.get("override_tag", triage.OVERRIDE_TAG_DEFAULT)).lower()
    if tag and tag in task.tags:
        return False
    patterns = policy.get("denylist_patterns") or triage.DEFAULT_DENYLIST
    return triage.hard_rule_violation(task, patterns, _comments(snap)) is not None


def load(log_root: Path, store: Any, *, now: int, policy: dict[str, Any],
         sources: list[str] | None = None) -> tuple[list[Case], Counter[str]]:
    """Every snapshot history can grade, newest first, and why the rest are not."""
    cases: list[Case] = []
    left_out: Counter[str] = Counter()
    histories: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((Path(log_root) / "triage").glob("*.json")):
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(snap, dict) or not snap.get("row_id"):
                raise ValueError("not a triage snapshot")
        except (OSError, ValueError):
            left_out["unreadable"] += 1
            continue
        if sources and snap.get("source") not in sources:
            continue
        if _denylisted(snap, policy):
            left_out[DENYLISTED] += 1
            continue
        rid = str(snap["row_id"])
        if rid not in histories:
            histories[rid] = store.ticket_history(rid)
        expected, repos, why = answer_key(histories[rid], snap, now=now,
                                          repo_of=store.repo_of)
        if expected is None:
            left_out[why] += 1
            continue
        cases.append(Case(name=path.stem, snap=snap, expected=expected,
                          repos=repos, why=why))
    cases.sort(key=lambda c: (int(c.snap.get("ts") or 0), c.name), reverse=True)
    return cases, left_out


# --- variants ------------------------------------------------------------
@dataclass
class Variant:
    """One configuration under test: a model and a triage prompt."""
    model: str | None             # None: the agent's own triage_model
    prompt: str                   # BUILTIN, or the file it was read from
    system: str                   # the text that stands in for triage.SYSTEM
    label: str
    current_repos: bool = False

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.system.encode()).hexdigest()[:8]

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "model": self.model, "prompt": self.prompt,
                "prompt_sha": self.sha, "current_repos": self.current_repos}


def variants(models: list[str], prompts: list[str], *, default_model: str | None,
             current_repos: bool = False) -> list[Variant]:
    """The baseline, then every model × prompt asked for, without repeats.

    The baseline -- the configured triage model and the built-in prompt --
    is always first: a candidate is only better or worse than something.
    A prompt's label carries a hash of its text, so editing a variant file
    between runs shows up as a different variant instead of the same name
    meaning two prompts. Raises ValueError for a prompt file that cannot be
    used.
    """
    def text_of(p: str) -> tuple[str, str]:
        if p == BUILTIN:
            return BUILTIN, triage.SYSTEM
        path = Path(p).expanduser()
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read prompt {p}: {exc}") from exc
        if not text:
            raise ValueError(f"prompt {p} is empty")
        return path.stem, text

    out: list[Variant] = []
    seen: set[tuple[str, str]] = set()
    asked = [(m, p) for m in (models or [None]) for p in (prompts or [BUILTIN])]
    for m, p in [(None, BUILTIN), *asked]:
        name, text = text_of(p)
        shown = m or default_model or "default"
        v = Variant(model=m, prompt=p if p == BUILTIN else str(Path(p).expanduser()),
                    system=text, label="", current_repos=current_repos)
        v.label = (f"{shown} · {name}@{v.sha}"
                   + (" · current repo notes" if current_repos else ""))
        # `--model sonnet` on a setup whose triage model is sonnet is the
        # baseline again, not a second variant.
        if (shown, v.sha) not in seen:
            seen.add((shown, v.sha))
            out.append(v)
    return out


# --- running -------------------------------------------------------------
def replay(agent: CodingAgent, case: Case, variant: Variant,
           policy: dict[str, Any], repos_now: dict[tuple[str, str], dict[str, Any]],
           secrets: list[str]) -> tuple[dict[str, Any], Spend]:
    """One trial: the variant's verdict on one past ticket, graded."""
    repos = [dict(r) for r in case.snap.get("repos") or []]
    if variant.current_repos:
        def today(r: dict[str, Any]) -> dict[str, Any]:
            now = repos_now.get((case.source, str(r.get("key"))))
            if now is None:         # gone from config: the snapshot is all there is
                return r
            # Both fields from today's config, absent where it has none: a
            # hint someone deleted must not ride along from the snapshot.
            return {"key": r.get("key"),
                    **{k: now[k] for k in ("language", "notes") if k in now}}
        repos = [today(r) for r in repos]
    fields: dict[str, Any] = {
        "variant": variant.label, "case_name": case.name, "task_id": case.row_id,
        "task_key": case.task_key, "why": case.why, "expected": case.expected,
        "expected_repos": case.repos, "recorded": case.recorded,
        "decision": None, "repos": [], "correct": None, "routed": None,
        "reasoning": None, "error": None}
    try:
        result, spend = triage.triage(agent, _task(case.snap), _comments(case.snap),
                                      repos, policy, model=variant.model,
                                      system=variant.system)
    except StationError as exc:
        fields["error"] = redact(str(exc), secrets)[:1000]
        return fields, exc.spend
    except Exception as exc:  # noqa: BLE001 -- one bad trial must not end the run
        log.warning("bench trial %s / %s failed", case.name, variant.label, exc_info=True)
        fields["error"] = redact(f"{type(exc).__name__}: {exc}", secrets)[:1000]
        return fields, Spend()
    fields.update(
        decision=result.decision, repos=result.repo_keys,
        correct=int(result.decision in case.expected),
        reasoning=redact(result.reasoning, secrets)[:2000])
    # Routing is only judged where there was a choice to make: with one
    # candidate `_pick_repos` never consults the model.
    if case.repos is not None and case.candidates >= 2 and result.decision == "execute":
        fields["routed"] = int(set(result.repo_keys) == set(case.repos))
    return fields, spend


def run(agent: CodingAgent, store: Any, cases: list[Case], variants_: list[Variant], *,
        policy: dict[str, Any], repos_now: dict[tuple[str, str], dict[str, Any]],
        repeat: int, max_usd: float, limits_check: Callable[[], str | None],
        secrets: list[str],
        on_trial: Callable[[Case, dict[str, Any], Spend], None] | None = None
        ) -> tuple[int, str | None]:
    """Replay `cases` under every variant and record each trial.

    Returns the run id and why it stopped early: "cap", "limits",
    "interrupted", or None when it ran to the end.

    Case by case, every variant on one case before the next, so a run
    stopped by the cap or the daily limits still compares the variants on
    the same triage runs. Both are checked before each case, never inside
    one, so a stop never leaves a case half-compared; a run can pass
    `max_usd` by one case's calls. Each call's spend goes on the ledger as
    kind `bench`, with no task id, so the daily limits see it.
    """
    run_id = store.start_bench(SUITE, [v.as_dict() for v in variants_], len(cases))
    spent, note, stop = 0.0, None, None
    try:
        for i, case in enumerate(cases):
            if spent >= max_usd:
                stop, note = "cap", (f"stopped at the ${max_usd:.2f} cap after {i} of "
                                     f"{len(cases)} triage runs")
                break
            hit = limits_check()
            if hit:
                stop, note = "limits", f"{hit}; stopped after {i} of {len(cases)} triage runs"
                break
            for v in variants_:
                for rep in range(1, max(1, repeat) + 1):
                    fields, spend = replay(agent, case, v, policy, repos_now, secrets)
                    store.record("bench", None,
                                 f"#{run_id} {case.name} {v.label}: "
                                 f"{fields['decision'] or 'failed'}",
                                 spend.cost_usd, spend.tokens, **spend.run_fields())
                    store.record_trial(run_id, rep=rep, cost_usd=spend.cost_usd,
                                       tokens=spend.tokens,
                                       duration_ms=spend.duration_ms,
                                       model=",".join(spend.models) or None, **fields)
                    spent += spend.cost_usd
                    if on_trial:
                        on_trial(case, fields, spend)
    except KeyboardInterrupt:
        stop = note = "interrupted"
    finally:
        store.finish_bench(run_id, note)
    return run_id, stop


# --- reading results -----------------------------------------------------
def summarize(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per variant, in the order they ran: accuracy, routing, agreement, cost.

    Accuracy is over trials that produced a decision; a failed trial is
    counted beside it and never in it, as a judge error is beside a pass
    rate. Every rate carries its n and its 95% range: twenty triage runs do not
    make a percentage.
    """
    by: dict[str, dict[str, Any]] = {}
    for t in trials:
        v = by.setdefault(t["variant"], {
            "variant": t["variant"], "trials": 0, "errors": 0, "n": 0,
            "correct": 0, "routed_n": 0, "routed_ok": 0, "compared": 0,
            "same": 0, "cost_usd": 0.0, "tokens": 0, "duration_ms": 0,
            "models": set()})
        v["trials"] += 1
        v["cost_usd"] += float(t.get("cost_usd") or 0)
        v["tokens"] += int(t.get("tokens") or 0)
        v["duration_ms"] += int(t.get("duration_ms") or 0)
        v["models"].update(m for m in str(t.get("model") or "").split(",") if m)
        if t.get("decision") is None:
            v["errors"] += 1
            continue
        if t.get("correct") is not None:
            v["n"] += 1
            v["correct"] += int(t["correct"])
        if t.get("routed") is not None:
            v["routed_n"] += 1
            v["routed_ok"] += int(t["routed"])
        if t.get("recorded"):
            v["compared"] += 1
            v["same"] += int(t["decision"] == t["recorded"])
    out = []
    for v in by.values():
        ci = wilson(v["correct"], v["n"])
        out.append({
            **v, "models": sorted(v["models"]),
            "cost_usd": round(v["cost_usd"], 6),
            "accuracy": v["correct"] / v["n"] if v["n"] else None,
            "low": ci[0] if ci else None, "high": ci[1] if ci else None,
            "routing": v["routed_ok"] / v["routed_n"] if v["routed_n"] else None,
            "avg_cost_usd": v["cost_usd"] / v["trials"] if v["trials"] else 0.0,
            "avg_tokens": v["tokens"] / v["trials"] if v["trials"] else 0.0,
        })
    return out


def misses(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trials that disagreed with history, on the decision or the repos."""
    return [t for t in trials if t.get("correct") == 0 or t.get("routed") == 0]


def decode(trial: dict[str, Any]) -> dict[str, Any]:
    """A `bench_trials` row with its JSON columns read back."""
    out = dict(trial)
    for k in ("expected", "repos", "expected_repos"):
        raw = out.get(k)
        try:
            out[k] = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            out[k] = None
    return out
