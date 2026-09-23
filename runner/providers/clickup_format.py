"""Markdown -> ClickUp rich-text comment ops.

ClickUp's `comment_text` field is plain text: every `**bold**`, backtick and
`<!-- marker -->` the pipeline writes shows up literally. The structured
`comment` field takes Quill-style ops instead -- inline runs carry `bold`,
`italic`, `code` or `link` attributes, and block formatting (lists, code
blocks) rides on the `"\\n"` op that *ends* each line.

Only the markdown the pipeline's own `_fmt_*` helpers emit is handled: bold,
italic, inline code, links, `- ` and `1. ` lists, and ``` fences. Anything
else passes through as plain text, which is what it looked like before.
"""
from __future__ import annotations

import re
from typing import Any

Op = dict[str, Any]

# One alternation, tried left to right at each position. Italic needs a
# non-word character on both sides so snake_case identifiers stay intact.
_INLINE = re.compile(
    r"\*\*(?P<bold>.+?)\*\*"
    r"|`(?P<code>[^`\n]+)`"
    r"|\[(?P<ltext>[^\]\n]+)\]\((?P<href>https?://[^)\s]+)\)"
    r"|(?<![\w_])_(?P<italic>[^_\n]+?)_(?![\w_])"
)
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _inline(text: str) -> list[Op]:
    ops: list[Op] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            ops.append({"text": text[pos:m.start()]})
        if m.group("bold") is not None:
            # Bold can wrap inline code (`**Review: `approved`**`); keep both.
            for op in _inline(m.group("bold")):
                op.setdefault("attributes", {})["bold"] = True
                ops.append(op)
        elif m.group("code") is not None:
            ops.append({"text": m.group("code"), "attributes": {"code": True}})
        elif m.group("href") is not None:
            ops.append({"text": m.group("ltext"), "attributes": {"link": m.group("href")}})
        else:
            ops.append({"text": m.group("italic"), "attributes": {"italic": True}})
        pos = m.end()
    if pos < len(text):
        ops.append({"text": text[pos:]})
    return ops


def to_ops(markdown: str) -> list[Op]:
    ops: list[Op] = []
    in_fence = False
    for line in markdown.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            if line:
                ops.append({"text": line})
            ops.append({"text": "\n", "attributes": {"code-block": {"code-block": "plain"}}})
            continue
        if m := _BULLET.match(line):
            ops += _inline(m.group(1))
            ops.append({"text": "\n", "attributes": {"list": {"list": "bullet"}}})
        elif m := _ORDERED.match(line):
            ops += _inline(m.group(1))
            ops.append({"text": "\n", "attributes": {"list": {"list": "ordered"}}})
        else:
            ops += _inline(line)
            ops.append({"text": "\n"})
    return ops


def plain_text(ops: list[Op]) -> str:
    """What ClickUp will report back as `comment_text` for these ops."""
    return "".join(str(op["text"]) for op in ops)
