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
    # Every repo this ticket needs a change in. One entry is the common case;
    # a feature spanning a backend and a frontend has two, and the pipeline
    # fans out one independent row (branch, worktree, PR, review) per entry.
    repo_keys: list[str] = field(default_factory=list)

    @property
    def repo_key(self) -> str | None:
        """The primary repo. Convenience for the single-repo path."""
        return self.repo_keys[0] if self.repo_keys else None


@dataclass
class TokenUsage:
    """Tokens consumed by one agent run.

    Recorded *alongside* `cost_usd`, not instead of it, because the two gate
    different things. With a claude.ai login the CLI's `total_cost_usd` is an
    API-*equivalent* number: it prices work that is actually drawn from the
    developer's 5-hourly/weekly subscription quota, so `daily_budget_usd`
    limits money nobody is billed for while the constraint that really bites
    -- the quota -- goes unmeasured. Token counts are the same currency the
    quota is denominated in.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total(self) -> int:
        """All four counts summed.

        Cache reads are included: they are billed at a discount but are still
        tokens the model processed, so they count against rate limits. This is
        a proxy for quota consumption, not a billing figure.
        """
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_input_tokens + self.cache_read_input_tokens)

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=(self.cache_creation_input_tokens
                                         + other.cache_creation_input_tokens),
            cache_read_input_tokens=(self.cache_read_input_tokens
                                     + other.cache_read_input_tokens),
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "TokenUsage":
        """Read a `usage` object off a Claude Code result event.

        Missing and malformed fields both become 0 rather than raising: usage
        accounting must never be the thing that fails a run that otherwise
        produced a working change.
        """
        usage = (payload or {}).get("usage")
        if not isinstance(usage, dict):
            return cls()
        def count(name: str) -> int:
            try:
                return max(0, int(usage.get(name) or 0))
            except (TypeError, ValueError):
                return 0
        return cls(
            input_tokens=count("input_tokens"),
            output_tokens=count("output_tokens"),
            cache_creation_input_tokens=count("cache_creation_input_tokens"),
            cache_read_input_tokens=count("cache_read_input_tokens"),
        )


@dataclass
class AgentResult:
    ok: bool
    text: str
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    usage: TokenUsage = field(default_factory=TokenUsage)
    timed_out: bool = False


@dataclass
class Spend:
    """What one station cost, in both currencies.

    Stations used to return a bare `float` cost. Threading a second number
    through every return and every `StationError` as a separate positional
    argument invites the two drifting apart at one call site; one object
    cannot.
    """
    cost_usd: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)

    @property
    def tokens(self) -> int:
        return self.usage.total

    def __add__(self, other: "Spend") -> "Spend":
        return Spend(cost_usd=self.cost_usd + other.cost_usd,
                     usage=self.usage + other.usage)

    @classmethod
    def of(cls, res: AgentResult) -> "Spend":
        return cls(cost_usd=res.cost_usd, usage=res.usage)


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
