"""Provider/agent-agnostic domain objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AIState(str, Enum):
    """Lifecycle of a ticket as far as the runner is concerned.

    This is the *single source of truth* and is mirrored into the task manager
    (ClickUp custom field or tag) so humans can see and override it.
    """

    NEW = "new"                     # never triaged
    NEEDS_INPUT = "needs-input"     # questions posted, waiting on a human
    HUMAN_ONLY = "human-only"       # off-limits for the agent
    AWAITING_PLAN_OK = "plan-review"  # plan posted, waiting for human approval
    PLANNING = "planning"           # analyst is reading the repo to write the plan
    QUEUED = "queued"               # approved, waiting for an execution slot
    IN_PROGRESS = "in-progress"     # agent is (or was) actively working
    REVIEW = "review"               # branch pushed, reviewer is judging it
    PR_OPEN = "pr-open"             # PR exists, polling CI + reviews
    BLOCKED = "blocked"             # escalated, needs a human
    DONE = "done"                   # PR merged

    @property
    def is_terminal(self) -> bool:
        return self in (AIState.HUMAN_ONLY, AIState.BLOCKED, AIState.DONE)


@dataclass
class Task:
    id: str                 # provider-internal id
    key: str                # human-facing id, e.g. DEV-1234
    title: str
    description: str
    status: str             # provider status, lowercased
    url: str
    list_id: str
    list_name: str
    tags: list[str]
    assignee_ids: list[str]
    updated_at: int         # epoch ms
    ai_state: AIState = AIState.NEW
    raw: dict[str, Any] = field(default_factory=dict)

    def as_prompt_block(self) -> str:
        return (
            f"Ticket: {self.key} — {self.title}\n"
            f"List: {self.list_name}\n"
            f"Status: {self.status}\n"
            f"Tags: {', '.join(self.tags) or 'none'}\n"
            f"URL: {self.url}\n\n"
            f"Description:\n{self.description.strip() or '(empty)'}\n"
        )


@dataclass
class Comment:
    id: str
    author: str
    author_id: str
    text: str
    created_at: int
    is_bot: bool = False


@dataclass
class TriageResult:
    decision: str            # execute | clarify | human_only
    confidence: float        # 0..1
    scope: str               # xs | s | m | l
    reasoning: str
    questions: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    repo_key: str | None = None


@dataclass
class AgentResult:
    ok: bool
    text: str
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    """The analyst station's output: a plan that has actually read the code."""
    verdict: str                     # ready | needs_input
    problem: str = ""
    approach: str = ""
    plan: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    files_of_interest: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


@dataclass
class ReviewResult:
    """The reviewer station's verdict on a pushed branch."""
    verdict: str                     # approve | request_changes | reject
    criteria_results: list[dict[str, Any]] = field(default_factory=list)
    blocking_findings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    summary: str = ""
    brain_notes: list[str] = field(default_factory=list)
