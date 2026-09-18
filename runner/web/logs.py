"""Read the per-run JSONL agent logs under `runner.log_dir`.

`_EventLog` writes and flushes per event, so a run in progress is readable
while it runs. Reads are by byte offset: the UI polls with the offset the
previous poll returned and gets only what was appended since.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from ..models import agent_log_name
from ..redact import redact

# A station is a fixed vocabulary plus numbered fix passes. Anchored, because
# this value reaches a filesystem path and the alternative is path traversal.
_STATION = re.compile(r"^(plan|implement|review|fix-\d+)$")

DEFAULT_MAX_BYTES = 262144  # one poll's worth; the UI pages with next_offset


def _candidates(log_root: Path, task_key: str, repo_key: str,
                station: str) -> list[Path]:
    """Current name first, then the pre-per-repo name.

    A row that was mid-flight when per-repo names shipped still has its log
    under the old name; showing it beats an empty tab.
    """
    return [log_root / agent_log_name(task_key, repo_key, station),
            log_root / f"{task_key}-{station}.log"]


def stations(log_root: Path, task_key: str, repo_key: str) -> list[str]:
    """Which station logs exist for this row."""
    out: list[str] = []
    prefix = agent_log_name(task_key, repo_key, "")[:-len(".log")]
    for path in sorted(log_root.glob(f"{prefix}*.log")):
        name = path.name[len(prefix):-len(".log")]
        if _STATION.match(name):
            out.append(name)
    for station in ("plan", "implement", "review"):
        if station not in out and (log_root / f"{task_key}-{station}.log").exists():
            out.append(station)
    return out


def tail(log_root: Path, task_key: str, repo_key: str, station: str,
         offset: int = 0, *, secrets: Sequence[str] = (),
         max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Records appended after `offset`, redacted.

    Returns `next_offset` so the caller polls forward rather than re-reading,
    and `missing` rather than raising, because a station that has not run yet
    is the normal case, not an error.
    """
    if not _STATION.match(station):
        return {"lines": [], "next_offset": offset, "eof": True,
                "missing": True}
    path = next((p for p in _candidates(log_root, task_key, repo_key, station)
                 if p.exists()), None)
    if path is None:
        return {"lines": [], "next_offset": offset, "eof": True,
                "missing": True}

    size = path.stat().st_size
    start = min(max(int(offset), 0), size)
    with path.open("rb") as fh:
        fh.seek(start)
        chunk = fh.read(max_bytes)

    # Stop at the last newline: a partial record at the end of the chunk is a
    # record the writer has not finished, and re-reading it next poll is
    # cheaper than guessing at it.
    cut = chunk.rfind(b"\n")
    if cut == -1:
        return {"lines": [], "next_offset": start, "eof": start >= size,
                "missing": False}
    consumed = cut + 1
    text = redact(chunk[:consumed].decode("utf-8", "replace"), secrets)

    lines: list[dict[str, Any]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            lines.append(json.loads(raw))
        except ValueError:
            # Redaction can straddle a JSON escape, and a killed writer can
            # leave a half-record. Surface it rather than dropping it.
            lines.append({"type": "unparsed", "text": raw})

    next_offset = start + consumed
    return {"lines": lines, "next_offset": next_offset,
            "eof": next_offset >= size, "missing": False}
