"""Pulling a JSON object out of an agent's reply.

Every station asks for "a single JSON object and nothing else" and every
station occasionally gets a code fence, a preamble, or both. One tolerant
parser is better than three subtly different ones.
"""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Return the outermost JSON object in `text`.

    Everything before the first `{` and after the last `}` is discarded, which
    is what makes this tolerant of a model that explains itself first.

    Raises:
        ValueError: no object-looking span is present at all.
        json.JSONDecodeError: the span is present but malformed.
    """
    cleaned = _FENCE.sub("", text.strip()).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in agent output: {cleaned[:300]}")
    return json.loads(cleaned[start:end + 1])


class StationError(RuntimeError):
    """Raised when a station's underlying agent run itself failed (`res.ok`
    is False) rather than merely returning unparseable text.

    Carries the cost already spent on that run. Without this, `raise
    RuntimeError(...)` discarded `res.cost_usd` on the way out — the caller
    never saw it, so `store.record(...)` never ran, and a failed run's spend
    was invisible to `daily_budget_usd` forever even though the provider had
    already billed it.
    """

    def __init__(self, message: str, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


def str_list(value: Any) -> list[str]:
    """Coerce a model's list field into a list of strings.

    A model that returns prose where an array was asked for is common enough
    to handle deliberately: iterating a bare string yields one entry per
    character, and a non-empty list of single letters silently defeats the
    emptiness guards the stations rely on.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return [str(value)]
