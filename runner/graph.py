"""The workflow graph, as data rather than as control flow.

Two things live here:

  1. TRANSITIONS - the legal state graph. Making it data (instead of `if`
     branches scattered through pipeline.py) buys three things: illegal
     transitions raise instead of silently corrupting state, the diagram
     is generated from the same object the runtime uses so it cannot drift,
     and the whole graph is testable without a provider or an agent.

  2. Scheduling constraints - the small amount of graph reasoning that
     actually earns its keep: don't start a ticket whose blockers are open,
     and don't run two agents in one repo at once.

Deliberately NOT here: an orchestration framework. The state machine is small
enough that a framework would add a dependency and remove nothing, and the
tick-and-exit design (which is what makes this survive a closed laptop) is
against the grain of every long-running-process orchestrator.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .models import AIState

log = logging.getLogger(__name__)

# Edge -> why it exists. The label is what gets rendered in the diagram.
TRANSITIONS: dict[AIState, dict[AIState, str]] = {
    AIState.NEW: {
        AIState.NEEDS_INPUT:      "triage: ambiguous",
        AIState.HUMAN_ONLY:       "triage: unsafe or needs a human",
        AIState.AWAITING_PLAN_OK: "triage: executable, scope needs sign-off",
        AIState.QUEUED:           "triage: executable, below approval gate",
        AIState.BLOCKED:          "triage failed",
    },
    AIState.NEEDS_INPUT: {
        AIState.NEW:              "human answered — re-triage",
        AIState.BLOCKED:          "orphaned or errored",
    },
    AIState.AWAITING_PLAN_OK: {
        AIState.QUEUED:           "plan approved",
        AIState.NEW:              "plan rejected — re-triage",
        AIState.BLOCKED:          "orphaned or errored",
    },
    AIState.QUEUED: {
        AIState.IN_PROGRESS:      "slot free, in execute window",
        AIState.BLOCKED:          "no repo mapped",
    },
    AIState.IN_PROGRESS: {
        AIState.PR_OPEN:          "draft PR opened",
        AIState.QUEUED:           "run crashed — retry",
        AIState.BLOCKED:          "agent failed or produced nothing",
    },
    AIState.PR_OPEN: {
        AIState.PR_OPEN:          "pushed fixes for CI or review",
        AIState.DONE:             "PR merged",
        AIState.BLOCKED:          "PR closed, or out of fix attempts",
    },
    # Terminal.
    AIState.HUMAN_ONLY: {},
    AIState.BLOCKED: {},
    AIState.DONE: {},
}


class IllegalTransition(RuntimeError):
    pass


def check(src: AIState, dst: AIState) -> None:
    if dst not in TRANSITIONS.get(src, {}):
        legal = ", ".join(s.value for s in TRANSITIONS.get(src, {})) or "(terminal)"
        raise IllegalTransition(f"{src.value} -> {dst.value} is not a legal transition. "
                                f"From {src.value} you may go to: {legal}")


def validate_graph() -> list[str]:
    """Structural checks. Run in doctor and in tests."""
    problems: list[str] = []
    reachable = {AIState.NEW}
    frontier = [AIState.NEW]
    while frontier:
        for nxt in TRANSITIONS.get(frontier.pop(), {}):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    for state in AIState:
        if state not in reachable:
            problems.append(f"{state.value} is unreachable from new")
        if state not in TRANSITIONS:
            problems.append(f"{state.value} has no entry in TRANSITIONS")
        if not TRANSITIONS.get(state) and not state.is_terminal:
            problems.append(f"{state.value} is a dead end but not marked terminal")
        if TRANSITIONS.get(state) and state.is_terminal:
            problems.append(f"{state.value} is terminal but has outgoing edges")
    return problems


def to_mermaid() -> str:
    """Render the live transition table. Paste into ClickUp docs or Confluence."""
    waits = {AIState.NEEDS_INPUT, AIState.HUMAN_ONLY,
             AIState.AWAITING_PLAN_OK, AIState.BLOCKED}
    lines = ["stateDiagram-v2", "    [*] --> new: TO DO + assigned to me"]
    for src, edges in TRANSITIONS.items():
        for dst, label in edges.items():
            lines.append(f"    {src.name} --> {dst.name}: {label}")
    lines.append("    DONE --> [*]")
    for s in waits:
        lines.append(f"    note right of {s.name}: waiting on a human")
    return "\n".join(lines)


# --- scheduling constraints ------------------------------------------------
@dataclass
class Gate:
    ok: bool
    reason: str = ""


def repo_is_free(repo_key: str, active_rows: Iterable[dict[str, Any]],
                 exclude_row_id: str | None = None) -> Gate:
    """One agent per repo at a time.

    Two agents in one repo branch from the same base and will produce PRs that
    conflict. File-overlap detection is the tempting fix, but you don't know
    which files a ticket touches until after the agent has run — so the useful
    constraint is the coarse one.
    """
    for row in active_rows:
        if row.get("task_id") == exclude_row_id:
            continue
        if row.get("repo_key") == repo_key and row.get("state") == AIState.IN_PROGRESS.value:
            return Gate(False, f"another task ({row.get('task_key')}) is already "
                               f"running in {repo_key}")
    return Gate(True)


def blockers_clear(blockers: list[dict[str, Any]]) -> Gate:
    """Don't start a ticket whose upstream dependencies are still open.

    `blockers` comes from the provider's native dependency graph: ClickUp task
    dependencies, Jira "is blocked by" issue links.
    """
    open_ones = [b for b in blockers if not b.get("closed")]
    if not open_ones:
        return Gate(True)
    names = ", ".join(str(b.get("key") or b.get("id")) for b in open_ones[:5])
    return Gate(False, f"blocked by {len(open_ones)} unfinished ticket(s): {names}")
