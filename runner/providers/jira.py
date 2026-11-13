"""Jira Cloud implementation of TaskProvider.

Design notes:
  * Machine state lives in an ISSUE ENTITY PROPERTY, not a custom field. No Jira
    admin needed, no screen configuration, atomic writes, invisible to humans.
  * Only the states a human must act on are mirrored to LABELS, so board filters
    and JQL still work without cluttering the UI with runner bookkeeping.
  * Comments use the /rest/api/2 endpoints, which take and return plain text.
    The v3 endpoints require Atlassian Document Format, which is a lot of JSON
    for no benefit here.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from ..models import AIState, Attachment, Comment, Task
from .base import TaskProvider, write_capped

log = logging.getLogger(__name__)

PROPERTY_KEY = "ai-runner-state"
# Only these get a visible label; the rest are runner-internal.
HUMAN_VISIBLE = {AIState.NEEDS_INPUT, AIState.HUMAN_ONLY,
                 AIState.AWAITING_PLAN_OK, AIState.BLOCKED}
# Parked states a human hands back by removing their label. The property
# that holds AI State has no editor in Jira's UI, so "set AI State back to
# `new`" cannot be done by hand; taking the label off is the reset.
RESETTABLE = {AIState.HUMAN_ONLY, AIState.BLOCKED}
CHANGELOG_PAGE = 100
# How far back to look when a parked property predates the `history` mark:
# the most recent this many entries, never the oldest.
CHANGELOG_LEGACY_WINDOW = 1000
FIELDS = "summary,description,status,labels,assignee,project,updated,issuelinks,attachment"


class JiraProvider(TaskProvider):
    def __init__(self, cfg: dict[str, Any], bot_marker: str, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.marker = bot_marker
        self.dry_run = dry_run
        self.base = str(cfg["site"]).rstrip("/")
        if not self.base.startswith("http"):
            self.base = f"https://{self.base}"
        self.ready_status = str(cfg.get("ready_status", "To Do"))
        self.projects = [str(p) for p in cfg.get("projects", [])]
        self.skip_labels = [str(x) for x in cfg.get("skip_labels", ["no-ai"])]
        # Opt-IN gate. Empty (the default) means "every ready ticket assigned to
        # me is a candidate". Set it when a non-engineer files the epics and a
        # human breaks them down by hand: only the leaves they mark are picked up.
        self.require_labels = [str(x) for x in cfg.get("require_labels", [])]
        self.s = requests.Session()
        self.s.auth = HTTPBasicAuth(str(cfg["email"]), str(cfg["token"]))
        self.s.headers.update({"Accept": "application/json",
                               "Content-Type": "application/json"})
        self._me: str | None = None

    def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        r = self.s.request(method, f"{self.base}{path}", timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Jira {method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else {}

    # --- identity -----------------------------------------------------
    def whoami(self) -> str:
        if self._me is None:
            self._me = str(self._req("GET", "/rest/api/3/myself")["accountId"])
        return self._me

    # --- reads --------------------------------------------------------
    def _jql(self) -> str:
        parts = ["assignee = currentUser()",
                 f'status = "{self.ready_status}"',
                 "resolution = EMPTY"]
        if self.projects:
            parts.append("project IN (" + ", ".join(f'"{p}"' for p in self.projects) + ")")
        if self.require_labels:
            # AND, not IN: every label listed must be present, so extra entries
            # narrow the funnel rather than widening it. Note this gates triage
            # only — fetch_candidates() feeds new tickets, while in-flight rows
            # are re-read by key, so pulling the label off a ticket the runner
            # already picked up does not strand it mid-PR.
            parts.extend(f'labels = "{x}"' for x in self.require_labels)
        if self.skip_labels:
            # NOTE: `labels NOT IN (...)` alone silently drops issues with no
            # labels at all. The IS EMPTY branch is required, not optional.
            joined = ", ".join(f'"{x}"' for x in self.skip_labels)
            parts.append(f"(labels IS EMPTY OR labels NOT IN ({joined}))")
        return " AND ".join(parts) + " ORDER BY updated ASC"

    def fetch_candidates(self) -> list[Task]:
        # /rest/api/3/search/jql is the current endpoint; the old /search is
        # deprecated. It paginates with nextPageToken rather than startAt.
        out: list[Task] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"jql": self._jql(), "maxResults": 50, "fields": FIELDS}
            if token:
                params["nextPageToken"] = token
            data = self._req("GET", "/rest/api/3/search/jql", params=params)
            out.extend(self._to_task(i) for i in data.get("issues", []))
            token = data.get("nextPageToken")
            if not token or data.get("isLast", True):
                break
        return out

    def get_task(self, task_id: str) -> Task:
        return self._to_task(
            self._req("GET", f"/rest/api/3/issue/{task_id}", params={"fields": FIELDS}))

    def list_comments(self, task_id: str) -> list[Comment]:
        data = self._req("GET", f"/rest/api/2/issue/{task_id}/comment",
                         params={"orderBy": "created", "maxResults": 100})
        out: list[Comment] = []
        for c in data.get("comments", []):
            body = c.get("body") or ""
            author = c.get("author") or {}
            out.append(Comment(
                id=str(c["id"]),
                author=author.get("displayName", "?"),
                author_id=str(author.get("accountId", "")),
                text=body if isinstance(body, str) else str(body),
                created_at=_epoch_ms(c.get("created")),
                is_bot=self.marker in str(body),
            ))
        out.sort(key=lambda c: c.created_at)
        return out

    # --- writes -------------------------------------------------------
    def add_comment(self, task_id: str, markdown: str) -> str:
        body = f"{self.marker}\n{markdown}"
        if self.dry_run:
            log.info("[dry-run] comment on %s:\n%s", task_id, markdown)
            return "dry-run"
        res = self._req("POST", f"/rest/api/2/issue/{task_id}/comment", json={"body": body})
        return str(res.get("id", ""))

    def set_ai_state(self, task_id: str, state: AIState) -> None:
        if self.dry_run:
            log.info("[dry-run] set state %s -> %s", task_id, state.value)
            return
        # Authoritative machine state. A parked state also records how long
        # the changelog was when it parked, so only a label removal made
        # after this point can count as a human's reset (`_effective_state`).
        prop: dict[str, Any] = {"state": state.value}
        if state in RESETTABLE:
            history = self._history_length(task_id)
            if history is not None:
                prop["history"] = history
        self._req("PUT", f"/rest/api/3/issue/{task_id}/properties/{PROPERTY_KEY}",
                  json=prop)
        # Human-visible mirror, one label at most.
        current = self._req("GET", f"/rest/api/3/issue/{task_id}",
                            params={"fields": "labels"})
        existing = (current.get("fields") or {}).get("labels", [])
        updates = [{"remove": lb} for lb in existing if lb.startswith("ai:")]
        if state in HUMAN_VISIBLE:
            want = f"ai:{state.value}"
            updates = [u for u in updates if u["remove"] != want]
            if want not in existing:
                updates.append({"add": want})
        if updates:
            self._req("PUT", f"/rest/api/3/issue/{task_id}",
                      json={"update": {"labels": updates}})

    def set_status(self, task_id: str, status: str) -> None:
        if self.dry_run:
            log.info("[dry-run] transition %s -> %s", task_id, status)
            return
        data = self._req("GET", f"/rest/api/3/issue/{task_id}/transitions")
        match = next((t for t in data.get("transitions", [])
                      if str(t.get("name", "")).lower() == status.lower()
                      or str((t.get("to") or {}).get("name", "")).lower() == status.lower()), None)
        if not match:
            names = [t.get("name") for t in data.get("transitions", [])]
            log.warning("No transition to '%s' on %s. Available: %s", status, task_id, names)
            return
        self._req("POST", f"/rest/api/3/issue/{task_id}/transitions",
                  json={"transition": {"id": match["id"]}})

    def blocked_by(self, task_id: str) -> list[dict[str, Any]]:
        """Jira issue links of type "is blocked by"."""
        data = self._req("GET", f"/rest/api/3/issue/{task_id}",
                         params={"fields": "issuelinks"})
        out: list[dict[str, Any]] = []
        for link in (data.get("fields") or {}).get("issuelinks", []) or []:
            inward = link.get("inwardIssue")
            if not inward:
                continue
            if "block" not in str((link.get("type") or {}).get("inward", "")).lower():
                continue
            f = inward.get("fields") or {}
            category = ((f.get("status") or {}).get("statusCategory") or {}).get("key", "")
            out.append({"key": inward.get("key"), "id": inward.get("key"),
                        "closed": category == "done"})
        return out

    # --- attachments --------------------------------------------------
    def download_attachment(self, task: Task, attachment: Attachment,
                            dest: Path, max_bytes: int) -> None:
        # The URL is built from the configured site, never taken from the
        # payload's `content` field, so the Basic-auth credential only ever
        # goes to `self.base`. Jira answers with a redirect to its media host;
        # requests drops Authorization on a cross-host redirect, and the
        # redirect carries its own short-lived token.
        if not attachment.id.isdigit():
            raise ValueError(f"unexpected Jira attachment id {attachment.id!r}")
        url = f"{self.base}/rest/api/3/attachment/content/{attachment.id}"
        with self.s.get(url, stream=True, timeout=30,
                        headers={"Accept": "*/*"}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"Jira GET attachment {attachment.id} -> {r.status_code}")
            write_capped(r.iter_content(64 * 1024), dest, max_bytes)

    # --- mapping ------------------------------------------------------
    def _to_task(self, issue: dict[str, Any]) -> Task:
        f = issue.get("fields") or {}
        project = f.get("project") or {}
        key = str(issue["key"])
        return Task(
            id=key,                       # Jira keys are stable identifiers
            key=key,
            title=f.get("summary", ""),
            description=_flatten_adf(f.get("description")),
            status=str((f.get("status") or {}).get("name", "")).lower(),
            url=f"{self.base}/browse/{key}",
            list_id=str(project.get("key", "")),
            list_name=str(project.get("name", "")),
            tags=[str(x).lower() for x in f.get("labels", [])],
            assignee_ids=[str((f.get("assignee") or {}).get("accountId", ""))],
            updated_at=_epoch_ms(f.get("updated")),
            ai_state=self._effective_state(
                key, *self._read_state(key),
                [str(x).lower() for x in f.get("labels", [])]),
            raw=issue,
            attachments=[_attachment(a) for a in f.get("attachment") or []
                         if isinstance(a, dict) and a.get("id")],
        )

    def reset_hint(self, state: AIState) -> str:
        if state in RESETTABLE:
            return f"remove the `ai:{state.value}` label"
        return super().reset_hint(state)

    def _effective_state(self, key: str, state: AIState, since: int | None,
                         labels: list[str]) -> AIState:
        """`new` when a human took a parked ticket's `ai:` label off.

        Absence alone is not enough: the runner writes the property before
        the label, so a label write that failed also leaves a parked ticket
        unlabelled, and reading that as a reset would re-triage it every
        tick. The changelog decides: the label was added and its latest
        change is a removal, both after `since` -- the changelog length when
        this park was written. That bound is what keeps an earlier reset's
        removal from counting again when a later park's label write fails.
        The runner never removes a parked label while the property is still
        parked (it moves the property first), so a removal in that window
        was a person's -- who cannot be told apart by author, since the
        runner acts as the developer's own account.
        """
        label = f"ai:{state.value}"
        if state not in RESETTABLE or label in labels:
            return state
        try:
            return AIState.NEW if self._label_removed(key, label, since) else state
        except (RuntimeError, requests.RequestException) as exc:
            # Never raise from here: `fetch_candidates` maps every issue, so
            # one unreadable history would stop every other ticket's triage.
            log.warning("[%s] could not read the changelog (%s); treating as %s",
                        key, exc, state.value)
            return state

    def _history_length(self, key: str) -> int | None:
        try:
            page = self._req("GET", f"/rest/api/3/issue/{key}/changelog",
                             params={"startAt": 0, "maxResults": 1})
            return int(page["total"])
        except (RuntimeError, requests.RequestException, KeyError, TypeError,
                ValueError) as exc:
            log.warning("[%s] changelog length unavailable (%s); a reset of this "
                        "park will be judged on recent history", key, exc)
            return None

    def _label_removed(self, key: str, label: str, since: int | None) -> bool:
        """Was `label` added and then last removed, at or after entry `since`?

        Reads forward from `since`. A property written before the mark
        existed has none: then the most recent `CHANGELOG_LEGACY_WINDOW`
        entries are read, never the oldest, so a fresh removal on a long-lived
        ticket is still seen.
        """
        if since is None:
            total = self._history_length(key)
            if total is None:
                return False
            since = max(0, total - CHANGELOG_LEGACY_WINDOW)
        seen_add = False
        last: str | None = None
        start = since
        while True:
            page = self._req("GET", f"/rest/api/3/issue/{key}/changelog",
                             params={"startAt": start, "maxResults": CHANGELOG_PAGE})
            values = page.get("values") or []
            for entry in values:
                for item in entry.get("items") or []:
                    if item.get("field") != "labels":
                        continue
                    before = str(item.get("fromString") or "").lower().split()
                    after = str(item.get("toString") or "").lower().split()
                    if label in after and label not in before:
                        last, seen_add = "add", True
                    elif label in before and label not in after:
                        last = "remove"
            start += len(values)
            if page.get("isLast", True) or not values:
                break
        return seen_add and last == "remove"

    def _read_state(self, key: str) -> tuple[AIState, int | None]:
        """(state, changelog length when it was written, if recorded)."""
        try:
            data = self._req("GET", f"/rest/api/3/issue/{key}/properties/{PROPERTY_KEY}")
        except RuntimeError:
            return AIState.NEW, None      # 404 = never triaged
        value = data.get("value") or {}
        try:
            state = AIState(str(value.get("state", "new")))
        except ValueError:
            return AIState.NEW, None
        since = value.get("history")
        return state, since if isinstance(since, int) and since >= 0 else None


def _epoch_ms(value: Any) -> int:
    if not value:
        return 0
    import datetime as _dt
    try:
        return int(_dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _attachment(a: dict[str, Any]) -> Attachment:
    try:
        size = int(a.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return Attachment(id=str(a["id"]), filename=str(a.get("filename") or a["id"]),
                      mime=str(a.get("mimeType") or ""), size=size)


def _media_ref(node: dict[str, Any], kind: str) -> str:
    """A marker where an embedded file was, so its absence is not invisible.

    ADF media nodes carry no text, only attrs. Dropping them made a pasted
    screenshot vanish without trace, and the model then told the reporter
    that the image they had attached was missing.
    """
    attrs = node.get("attrs") or {}
    if attrs.get("type") == "external" and attrs.get("url"):
        return f"[{kind}: {attrs['url']}]"
    name = str(attrs.get("alt") or "").strip()
    return f"[{kind}: {name}]" if name else f"[{kind} attached]"


def _flatten_adf(node: Any) -> str:
    """Atlassian Document Format -> plain text, good enough for a prompt."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_flatten_adf(n) for n in node)
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "text":
            text = str(node.get("text", ""))
            # A link's target lives in a mark, not the text; without it
            # "the artifact" reaches the model with nowhere to point.
            href = next((str((m.get("attrs") or {}).get("href") or "")
                         for m in node.get("marks") or []
                         if isinstance(m, dict) and m.get("type") == "link"), "")
            return f"{text} ({href})" if href and href != text else text
        # Smart links carry only a URL attr and no text: dropping them told
        # the model a pasted link "is not in the ticket".
        if kind in ("inlineCard", "blockCard", "embedCard"):
            url = str((node.get("attrs") or {}).get("url") or "")
            return url + ("\n" if kind != "inlineCard" else "") if url else ""
        # mediaSingle wraps one embedded image; mediaGroup and mediaInline
        # are file cards, which may be any type.
        if kind == "media":
            return _media_ref(node, "image")
        if kind == "mediaInline":
            return _media_ref(node, "attachment")
        if kind == "mediaGroup":
            return "\n".join(_media_ref(n, "attachment")
                             for n in node.get("content") or []
                             if isinstance(n, dict)) + "\n"
        content = node.get("content")
        # A paragraph's children are inline runs of one line: bold, a link,
        # a smart link. Joined with newlines they split mid-sentence.
        if kind in ("paragraph", "heading") and isinstance(content, list):
            inner = "".join(_flatten_adf(n) for n in content)
        else:
            inner = _flatten_adf(content)
        return inner + ("\n" if kind in ("paragraph", "heading", "mediaSingle") else "")
    return ""
