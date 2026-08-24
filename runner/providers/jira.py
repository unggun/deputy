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
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from ..models import AIState, Comment, Task
from .base import TaskProvider

log = logging.getLogger(__name__)

PROPERTY_KEY = "ai-runner-state"
# Only these get a visible label; the rest are runner-internal.
HUMAN_VISIBLE = {AIState.NEEDS_INPUT, AIState.HUMAN_ONLY,
                 AIState.AWAITING_PLAN_OK, AIState.BLOCKED}
FIELDS = "summary,description,status,labels,assignee,project,updated,issuelinks"


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
        # Authoritative machine state.
        self._req("PUT", f"/rest/api/3/issue/{task_id}/properties/{PROPERTY_KEY}",
                  json={"state": state.value})
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
            ai_state=self._read_state(key),
            raw=issue,
        )

    def _read_state(self, key: str) -> AIState:
        try:
            data = self._req("GET", f"/rest/api/3/issue/{key}/properties/{PROPERTY_KEY}")
        except RuntimeError:
            return AIState.NEW      # 404 = never triaged
        try:
            return AIState(str((data.get("value") or {}).get("state", "new")))
        except ValueError:
            return AIState.NEW


def _epoch_ms(value: Any) -> int:
    if not value:
        return 0
    import datetime as _dt
    try:
        return int(_dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _flatten_adf(node: Any) -> str:
    """Atlassian Document Format -> plain text, good enough for a prompt."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_flatten_adf(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        inner = _flatten_adf(node.get("content"))
        return inner + ("\n" if node.get("type") in ("paragraph", "heading") else "")
    return ""
