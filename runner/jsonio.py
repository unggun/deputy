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
