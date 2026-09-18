"""Scrub credential material out of text before it leaves this process.

Agent stderr, `git` stderr and `gh` stderr all end up in three places that
outlive the run: the per-run event log on disk, `tasks.last_error` in SQLite,
and -- via `_escalate()` -- a **comment on the ticket itself**, visible to
everyone with read access to the workspace. A `gh` auth failure, or a `git
push` against a remote that carries credentials in its URL, is enough to put a
live token there permanently.

Two mechanisms, because neither alone is sufficient:

  * `KNOWN` matches the shapes of the credentials this runner actually
    handles. It catches secrets nobody told us about (a token baked into a
    repo's `setup:` command, one the agent printed itself).
  * `extra=` redacts exact values the caller already knows are secret -- the
    `GH_TOKEN` it just injected into the subprocess env. Pattern matching is
    best-effort; an exact-value match is certain, and it covers formats that
    have not been invented yet.

Patterns are deliberately narrow. A greedy "anything that looks secret" regex
eats branch names, SHAs and file paths, which is worse than the leak it
prevents: a human who cannot read the error just re-runs the failing command
by hand, with the same credentials, outside the runner.
"""
from __future__ import annotations

import re
from typing import Iterable, Pattern

PLACEHOLDER = "[REDACTED]"

# Below this length an "exact value" match is more likely to be a coincidence
# in ordinary prose than a credential, and redacting it destroys the message.
MIN_LITERAL_LEN = 8

# (pattern, replacement). Where a pattern needs surrounding context to anchor
# itself, that context is captured in group 1 and put back, so the message
# still says *which* header or URL was involved.
KNOWN: tuple[tuple[Pattern[str], str], ...] = (
    # https://x-access-token:ghs_xxx@github.com/acme/api -- how a tokenised
    # git remote leaks. Keep the scheme and host: the error is useless without
    # naming the remote it failed against.
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"), PLACEHOLDER),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), PLACEHOLDER),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), PLACEHOLDER),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), PLACEHOLDER),
    # Atlassian API tokens (Jira) and ClickUp personal tokens.
    (re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}"), PLACEHOLDER),
    (re.compile(r"\bpk_\d+_[A-Za-z0-9]{20,}"), PLACEHOLDER),
    # Authorization: Bearer xxx / token xxx, as echoed by curl -v and by the
    # `gh` CLI in debug mode.
    (re.compile(r"(?i)\b((?:authorization\s*:\s*)?(?:bearer|token)\s+)"
                r"[A-Za-z0-9._\-]{16,}"), r"\1" + PLACEHOLDER),
)


def redact(text: str, extra: Iterable[str] = ()) -> str:
    """Return `text` with credential material replaced by `PLACEHOLDER`.

    `extra` holds literal secret values the caller knows -- typically
    `vcs.profile_env(...).values()`, i.e. the token this very run injected.
    Values shorter than `MIN_LITERAL_LEN` are ignored; a two-character
    "secret" would redact half the message.
    """
    if not text:
        return text
    # Exact values first: a literal pass leaves nothing for the shape-based
    # patterns to half-match and mangle.
    for value in extra:
        value = str(value or "")
        if len(value) >= MIN_LITERAL_LEN:
            text = text.replace(value, PLACEHOLDER)
    for pattern, replacement in KNOWN:
        text = pattern.sub(replacement, text)
    return text
