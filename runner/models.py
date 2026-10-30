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
    # A human tagged the ticket with `policy.override_tag`, so the denylist
    # was skipped. The pipeline then always gates on plan approval: the one
    # automatic check was waived, so a human reads the plan before code.
    policy_override: bool = False

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
    # What `--model` asked for, or None when the CLI default was used.
    requested_model: str | None = None

    @property
    def models(self) -> list[str]:
        """Models that answered, per the result payload. See `answered_models`."""
        return answered_models(self.raw)


def answered_models(payload: dict[str, Any] | None) -> list[str]:
    """Every model `modelUsage` says answered, the most expensive first.

    A `work()` run legitimately reports several -- subagents bill separately --
    so this is a list, and its first entry is the one that did most of the
    work. Each key is followed by its `canonicalModel` when that differs, so
    an alias request (`sonnet`) can be matched against either spelling.

    An absent or malformed `modelUsage` is an empty list, never an error:
    telemetry must not be what fails a run.
    """
    reported = (payload or {}).get("modelUsage")
    if not isinstance(reported, dict):
        return []

    def spent(item: tuple[Any, Any]) -> float:
        entry = item[1]
        try:
            return -float(entry.get("costUSD") or 0) if isinstance(entry, dict) else 0.0
        except (TypeError, ValueError):
            return 0.0

    out: list[str] = []
    for key, entry in sorted(reported.items(), key=spent):
        canonical = entry.get("canonicalModel") if isinstance(entry, dict) else None
        for name in (key, canonical):
            if name and str(name) not in out:
                out.append(str(name))
    return out


def model_present(wanted: str, ran: list[str]) -> bool:
    """Did the requested model answer?

    `--model` accepts an alias ("sonnet") as readily as a canonical id
    ("claude-sonnet-5"), so an alias counts as present when it names part of
    an id that ran. Presence, not exclusivity: subagents may add others.
    """
    return any(wanted == r or wanted in r for r in ran)


@dataclass
class Spend:
    """What one station cost, in both currencies, and what its runs were.

    Stations used to return a bare `float` cost. Threading a second number
    through every return and every `StationError` as a separate positional
    argument invites the two drifting apart at one call site; one object
    cannot. The run telemetry (wall time, turns, models) rides along for the
    same reason: a station that retries once has two runs, and the ledger
    line has to describe both.
    """
    cost_usd: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)
    duration_ms: int = 0
    num_turns: int = 0
    models: list[str] = field(default_factory=list)
    requested_model: str | None = None

    @property
    def tokens(self) -> int:
        return self.usage.total

    def __add__(self, other: "Spend") -> "Spend":
        return Spend(cost_usd=self.cost_usd + other.cost_usd,
                     usage=self.usage + other.usage,
                     duration_ms=self.duration_ms + other.duration_ms,
                     num_turns=self.num_turns + other.num_turns,
                     models=list(dict.fromkeys(self.models + other.models)),
                     requested_model=self.requested_model or other.requested_model)

    @classmethod
    def of(cls, res: AgentResult) -> "Spend":
        return cls(cost_usd=res.cost_usd, usage=res.usage,
                   duration_ms=res.duration_ms, num_turns=res.num_turns,
                   models=res.models, requested_model=res.requested_model)

    def run_fields(self) -> dict[str, Any]:
        """The telemetry columns of an `events` row, as `Store.record` takes them."""
        return {"duration_ms": self.duration_ms, "turns": self.num_turns,
                "model": ",".join(self.models) or None,
                "model_requested": self.requested_model}


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


def agent_log_name(task_key: str, repo_key: str, station: str) -> str:
    """Filename for one station's JSONL run log.

    The repo key is part of the name because a ticket routed to two repos has
    two independent rows, and `_EventLog` opens its file with "w". Without the
    repo key the second repo's run truncates the first repo's evidence.

    `__` separates the fields, matching `brain.py`'s `{source}__{repo}`, and
    because `-` appears inside both ticket keys (DEV-1234) and repo keys
    (pulse-backend) and so cannot be parsed back out.

    Both fields are sanitised, not just the repo key: a ClickUp Custom Task
    ID is whatever the workspace typed, and a `/` in either one would put the
    log somewhere other than `runner.log_dir`.
    """
    safe_key = task_key.replace("/", "_")
    safe_repo = repo_key.replace("/", "_")
    return f"{safe_key}__{safe_repo}__{station}.log"


def run_station(station: str, n: int) -> str:
    """The log station name of the `n`th run (1-based) of a station on a row.

    `_EventLog` opens with "w", so a second implement or review pass on the
    same row used to overwrite the first pass's transcript -- the evidence a
    later reviewer, scorer or human most wants. The first run keeps the bare
    name, so rows from before numbering existed still find their log; later
    runs are `implement-2`, `implement-3`. `fix` is always numbered, matching
    the `fix-N` names it has had all along.
    """
    if station == "fix" or n > 1:
        return f"{station}-{max(n, 1)}"
    return station
