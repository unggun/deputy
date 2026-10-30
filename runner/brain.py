"""Durable per-repository notes, injected into every station prompt.

Foreman calls this the "factory brain": the things a run learns about a
repository that would otherwise be relearned every time, like which env var
the tests need or which review finding keeps recurring.

Two deliberate choices:

  * It is plain markdown in a known directory. Editing or deleting it by hand
    is a supported operation, not corruption.
  * At the size cap it refuses to append and says so, rather than evicting the
    oldest note. A brain that quietly forgets is worse than one that tells you
    to prune it.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 8000

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_DATE_PREFIX = re.compile(r"^- \d{4}-\d{2}-\d{2}: ")


def _safe(part: str) -> str:
    return _UNSAFE.sub("-", str(part))[:60]


def _norm(text: str) -> str:
    """Compare notes ignoring case and whitespace, so near-repeats collapse."""
    return " ".join(str(text).split()).casefold()


def brain_path(brain_dir: Path, source_key: str, repo_key: str) -> Path:
    """One file per repository, namespaced by source.

    Namespacing matters for the same reason row ids are namespaced: two
    workspaces can map different repositories to the same key.
    """
    return Path(brain_dir) / f"{_safe(source_key)}__{_safe(repo_key)}.md"


def read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def as_prompt_block(path: Path) -> str:
    """The brain as a prompt section, or an empty string when there is nothing."""
    text = read(path)
    if not text:
        return ""
    return (
        "=== REPO NOTES (learned on earlier runs; treat as background, "
        "verify before relying on any of it) ===\n"
        f"{text}\n"
    )


def has_note(path: Path, note: str) -> bool:
    """Is `note` already in the brain, by the same comparison `append` uses?"""
    return _norm(" ".join(str(note).split())) in {
        _norm(_DATE_PREFIX.sub("", line))
        for line in read(path).splitlines() if line.strip()}


def append(path: Path, notes: list[str], *,
           max_bytes: int = DEFAULT_MAX_BYTES,
           today: dt.date | None = None) -> list[str]:
    """Append notes that are not already present. Returns what was written."""
    path = Path(path)
    existing = read(path)
    seen = {
        _norm(_DATE_PREFIX.sub("", line))
        for line in existing.splitlines()
        if line.strip()
    }
    stamp = (today or dt.date.today()).isoformat()

    body = existing
    written: list[str] = []
    for note in notes:
        clean = " ".join(str(note).split())
        if not clean or _norm(clean) in seen:
            continue
        line = f"- {stamp}: {clean}"
        candidate = f"{body}\n{line}" if body else line
        if len(candidate.encode("utf-8")) > max_bytes:
            log.warning(
                "brain %s is at its %d-byte cap; refusing to append %d more "
                "note(s). Prune it by hand.",
                path, max_bytes, len(notes) - len(written),
            )
            break
        body = candidate
        seen.add(_norm(clean))
        written.append(clean)

    if written:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.rstrip() + "\n", encoding="utf-8")
    return written
