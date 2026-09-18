"""Pulling a JSON object out of an agent's reply.

Every station asks for "a single JSON object and nothing else" and every
station occasionally gets a code fence, a preamble, or both. One tolerant
parser is better than three subtly different ones.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .models import Spend

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

    Carries the spend already committed on that run. Without this, `raise
    RuntimeError(...)` discarded it on the way out — the caller never saw it,
    so `store.record(...)` never ran, and a failed run's spend was invisible
    to the daily limits forever even though the quota had already been drawn
    down.
    """

    def __init__(self, message: str, spend: "Spend | float | None" = None) -> None:
        super().__init__(message)
        if spend is None:
            spend = Spend()
        elif isinstance(spend, (int, float)):
            # Tolerated so a caller that only knows a dollar figure still
            # reports it rather than dropping the spend entirely.
            spend = Spend(cost_usd=float(spend))
        self.spend: Spend = spend

    @property
    def cost_usd(self) -> float:
        return self.spend.cost_usd

    @property
    def tokens(self) -> int:
        return self.spend.tokens


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
