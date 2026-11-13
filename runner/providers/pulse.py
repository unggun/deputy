"""Pulse Task Spaces implementation of TaskProvider.

Talks to Pulse's MCP server (`POST {base_url}/mcp`) with plain JSON-RPC over
`requests`. There is no LLM and no MCP SDK in the loop: Deputy already knows
which tool it wants, and Pulse's server is stateless and answers with plain
JSON, so every call is one `tools/call` POST.

Design notes (plan: docs/superpowers/plans/2026-11-12-pulse-provider.md):

  * The CONTAINER is the Pulse Space (D5). `Task.list_id` / `list_name` carry
    the Space id and name, so `repos_for_container` matches `containers:`
    against Space ids and `container_names:` against Space names.
  * State lives in a dropdown custom field (D4). Its value is an option id;
    `state_field_options` maps every AIState to one, as on ClickUp.
  * Comments are markdown with a VISIBLE footer (D6). Pulse renders a body that
    is not HTML with its markdown renderer; an `<!-- -->` marker would push the
    body onto the HTML path, so it is stripped before posting. `via: "mcp"` is
    NOT a bot marker: the owner's own AI assistant sets it too.
  * `fetch_candidates` costs 1 + N calls. `search_tasks` rows carry neither the
    custom fields (so no AI state) nor the description, and the pipeline reads
    both from candidates (`_triage_new` branches on `ai_state`, triage reads the
    description), so each candidate is re-read with `get_task`. Pulse allows
    100 MCP calls/min per user (D9); a ready queue of a few dozen is fine.
  * A 401 raises ProviderAuthError and is never caught here (D8): the tick
    fails this source as a whole and `deputy status` shows why.
  * `branch_key` is the default `task.key` = the short id (`PLS-1234`), which
    Pulse's GitHub App already links branches, commits and PRs by (D7).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
from html.parser import HTMLParser
from typing import Any

import requests

from ..models import AIState, Comment, Task
from .base import ProviderAuthError, TaskProvider
from .clickup import FOOTER_DEFAULT

log = logging.getLogger(__name__)

MAX_PAGE = 100                 # search_tasks / list_comments `limit` ceiling
MAX_PAGES = 50                 # 5,000 rows: a safety stop, not a real limit
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


class PulseAuthError(ProviderAuthError):
    """Pulse refused the token. Fail the whole source; never retry (D8)."""


class PulseToolError(RuntimeError):
    """A tool ran and said no (`isError`). The message is Pulse's own sentence."""


class PulseProvider(TaskProvider):
    def __init__(self, cfg: dict[str, Any], bot_marker: str, dry_run: bool = False) -> None:
        missing = [k for k in ("base_url", "token", "state_field_id") if not cfg.get(k)]
        if missing:
            # Name the keys, never their values: this message reaches logs.
            raise RuntimeError(f"pulse provider: missing {', '.join(missing)}")
        options = {str(k): str(v) for k, v in (cfg.get("state_field_options") or {}).items()}
        unmapped = [s.value for s in AIState if not options.get(s.value)]
        if unmapped:
            raise RuntimeError(
                "pulse provider: state_field_options is missing " + ", ".join(unmapped))

        self.cfg = cfg
        self.marker = bot_marker
        self.footer = FOOTER_DEFAULT if bot_marker.lstrip().startswith("<!--") else bot_marker
        self.dry_run = dry_run
        self.endpoint = str(cfg["base_url"]).rstrip("/") + "/mcp"
        self.web_url = str(cfg.get("web_url") or "").rstrip("/")
        self.spaces = [str(s) for s in cfg.get("spaces", []) or []]
        self.ready_status = str(cfg.get("ready_status", "To Do"))
        self.skip_tags = {str(t).lower() for t in cfg.get("skip_tags", ["no-ai"])}
        self.state_field_id = str(cfg["state_field_id"])
        self.state_options = options
        self._state_by_option = {v: k for k, v in options.items()}
        self._token = str(cfg["token"])
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        self._rpc_id = 0
        # True after Pulse accepted the token, False after it refused it.
        # `_tick_all` clears a recorded auth failure only on True.
        self.authenticated = False
        self._me: str | None = None
        self.identity: str | None = None   # "Name <email>", for `deputy doctor`
        self._space_name_cache: dict[str, str] | None = None

    def __repr__(self) -> str:   # never the token
        return f"PulseProvider(endpoint={self.endpoint!r})"

    # --- transport ----------------------------------------------------
    def _call(self, tool: str, **arguments: Any) -> Any:
        self._rpc_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "tools/call",
            "params": {"name": tool,
                       "arguments": {k: v for k, v in arguments.items() if v is not None}},
        }
        r = self.s.post(self.endpoint, json=body, timeout=30)
        if r.status_code == 401:
            self.authenticated = False
            raise PulseAuthError(f"Pulse token rejected: {_error_message(r)}")
        if r.status_code == 429:
            raise RuntimeError("Pulse rate limit hit (100 MCP calls/min per user)")
        if r.status_code >= 400:
            raise RuntimeError(f"Pulse {tool} -> {r.status_code}: {r.text[:400]}")
        self.authenticated = True
        data = r.json()
        if data.get("error"):
            err = data["error"]
            raise RuntimeError(f"Pulse {tool}: {err.get('message', err)}")
        result = data.get("result") or {}
        text = _first_text(result)
        if result.get("isError"):
            raise PulseToolError(f"Pulse {tool}: {text or 'tool failed'}")
        return json.loads(text) if text else {}

    def _paged(self, tool: str, key: str, **arguments: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            res = self._call(tool, page=page, limit=MAX_PAGE, **arguments)
            batch = res.get(key) or []
            rows.extend(batch)
            if not batch or page * int(res.get("limit") or MAX_PAGE) >= int(res.get("total") or 0):
                return rows
        log.warning("pulse %s stopped at %d pages", tool, MAX_PAGES)
        return rows

    # --- identity -----------------------------------------------------
    def whoami(self) -> str:
        if self._me is None:
            try:
                me = self._call("get_me")
                self._me = str(me["id"])
                self.identity = f"{me.get('name') or '?'} <{me.get('email') or '?'}>"
            except PulseToolError:
                # `get_me` ships with Pulse's personal-access-token ticket.
                # Until then an explicit `user_id` in config stands in.
                fallback = self.cfg.get("user_id")
                if not fallback:
                    raise
                log.warning("pulse get_me unavailable; using configured user_id")
                self._me = str(fallback)
        return self._me

    def _space_names(self) -> dict[str, str]:
        if self._space_name_cache is None:
            self._space_name_cache = {
                str(s["id"]): str(s.get("name", ""))
                for s in self._paged("list_spaces", "spaces")}
        return self._space_name_cache

    # --- reads --------------------------------------------------------
    def fetch_candidates(self) -> list[Task]:
        # One provider instance per tick, so a fresh Space list per tick.
        self._space_name_cache = None
        me = self.whoami()
        rows: list[dict[str, Any]] = []
        for space in self.spaces or [None]:
            rows.extend(self._paged("search_tasks", "tasks", assigneeId=me,
                                    status=self.ready_status, spaceId=space))
        out: list[Task] = []
        seen: set[str] = set()
        for row in rows:
            tid = str(row["id"])
            if tid in seen or (row.get("status") or {}).get("isCompleted"):
                continue
            seen.add(tid)
            if self.skip_tags.intersection(_tag_names(row)):
                continue
            # The summary lacks custom fields and the description; see docstring.
            out.append(self.get_task(tid))
        return out

    def get_task(self, task_id: str) -> Task:
        return self._to_task(self._call("get_task", id=task_id))

    def list_comments(self, task_id: str) -> list[Comment]:
        out: list[Comment] = []
        for c in self._paged("list_comments", "comments", taskId=task_id):
            if c.get("isDeleted") or c.get("body") is None:
                continue
            body = str(c.get("body") or "")
            text = html_to_text(body) if body.lstrip().startswith("<") else body
            author = c.get("author") or {}
            out.append(Comment(
                id=str(c["id"]),
                author=str(author.get("name") or author.get("email") or "?"),
                author_id=str(author.get("id") or ""),
                text=text,
                created_at=_epoch_ms(c.get("createdAt")),
                is_bot=self.footer in text or self.marker in body,
            ))
        out.sort(key=lambda c: c.created_at)
        return out

    def blocked_by(self, task_id: str) -> list[dict[str, Any]]:
        """Tasks this one waits on.

        `links` lists every relationship; `blockedBy` lists only the blockers
        that are still open (Pulse clears a block when the blocker reaches a
        completed status). So "closed" is simply "not in blockedBy", with no
        extra read per blocker. A restricted blocker (a Space we cannot read)
        is still in both lists, so it is judged correctly too.
        """
        t = self._call("get_task", id=task_id)
        open_ids = {str(b.get("id")) for b in t.get("blockedBy") or []}
        out: list[dict[str, Any]] = []
        for link in t.get("links") or []:
            if link.get("relationship") != "blocked_by":
                continue
            other = str(link.get("taskId") or "")
            out.append({"key": str(link.get("shortId") or other), "id": other,
                        "closed": other not in open_ids})
        return out

    # --- writes -------------------------------------------------------
    def add_comment(self, task_id: str, markdown: str) -> str:
        clean = _HTML_COMMENT.sub("", markdown)
        if clean != markdown:
            log.warning("stripped an HTML comment from a Pulse comment; "
                        "it would switch Pulse to HTML rendering")
        md = f"{clean.rstrip()}\n\n_{self.footer}_"
        if self.dry_run:
            log.info("[dry-run] comment on %s:\n%s", task_id, md)
            return "dry-run"
        res = self._call("add_comment", taskId=task_id, body=md)
        return str(res.get("id", ""))

    def set_ai_state(self, task_id: str, state: AIState) -> None:
        option_id = self.state_options.get(state.value)
        if not option_id:
            raise RuntimeError(f"No custom-field option mapped for state {state.value}")
        if self.dry_run:
            log.info("[dry-run] set state %s -> %s", task_id, state.value)
            return
        self._call("update_task", id=task_id,
                   customFields={self.state_field_id: option_id})

    def set_status(self, task_id: str, status: str) -> None:
        if self.dry_run:
            log.info("[dry-run] set status %s -> %s", task_id, status)
            return
        # Pulse matches the name per Space, case-insensitively. A Space with
        # no status of that name answers isError, which the pipeline escalates.
        self._call("update_task", id=task_id, status=status)

    # --- mapping ------------------------------------------------------
    def _to_task(self, t: dict[str, Any]) -> Task:
        space_id = str(t.get("spaceId") or "")
        list_id = str(t.get("listId") or "")
        url = str(t.get("url") or "")
        if not url and self.web_url and space_id and list_id:
            url = (f"{self.web_url}/operational/workspace/{space_id}"
                   f"/lists/{list_id}/tasks/{t['id']}")
        description = str(t.get("description") or "")
        if description.lstrip().startswith("<"):
            description = html_to_text(description)
        return Task(
            id=str(t["id"]),
            key=str(t.get("shortId") or t["id"]),
            title=str(t.get("title") or ""),
            description=description,
            status=str((t.get("status") or {}).get("name", "")).lower(),
            url=url,
            list_id=space_id,
            list_name=self._space_names().get(space_id, space_id) if space_id else "",
            tags=sorted(_tag_names(t)),
            assignee_ids=[str(a.get("id")) for a in t.get("assignees") or [] if a.get("id")],
            updated_at=_epoch_ms(t.get("updatedAt")),
            ai_state=self._read_state(t),
            raw=t,
        )

    def _read_state(self, t: dict[str, Any]) -> AIState:
        for f in t.get("customFields") or []:
            if str(f.get("id")) != self.state_field_id:
                continue
            value = f.get("value")
            if isinstance(value, dict):
                value = value.get("id")
            name = self._state_by_option.get(str(value)) if value is not None else None
            if name:
                try:
                    return AIState(name)
                except ValueError:
                    return AIState.NEW
        return AIState.NEW


# --- helpers ----------------------------------------------------------
def _first_text(result: dict[str, Any]) -> str:
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text") or "")
    return ""


def _error_message(r: requests.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:200]
    if isinstance(data, dict):
        for key in ("error_description", "message", "error"):
            if isinstance(data.get(key), str) and data[key]:
                return data[key]
    return r.text[:200]


def _tag_names(t: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for tag in t.get("tags") or []:
        name = tag.get("name") if isinstance(tag, dict) else tag
        if name:
            out.add(str(name).lower())
    return out


def _epoch_ms(value: Any) -> int:
    if not value:
        return 0
    try:
        return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                   .timestamp() * 1000)
    except ValueError:
        return 0


class _TextExtractor(HTMLParser):
    _BREAKS = {"br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self.parts.append("\n- ")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BREAKS and tag != "br":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Pulse stores rich text as HTML; triage and the analyst want plain text."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
