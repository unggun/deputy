"""ClickUp v2 implementation of TaskProvider.

State backend is configurable:
  * custom_field (recommended) - a dropdown field, atomic and filterable
  * tag          - simpler to set up, but a shared global namespace
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from ..models import AIState, Comment, Task
from .base import TaskProvider

log = logging.getLogger(__name__)
API = "https://api.clickup.com/api/v2"


class ClickUpProvider(TaskProvider):
    def __init__(self, cfg: dict[str, Any], bot_marker: str, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.marker = bot_marker
        self.dry_run = dry_run
        self.team_id = str(cfg["team_id"])
        self.ready_status = str(cfg.get("ready_status", "to do")).lower()
        self.state_backend = cfg.get("state_backend", "custom_field")
        self.state_field_id = cfg.get("state_field_id")
        self.state_options: dict[str, str] = cfg.get("state_field_options", {})
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": cfg["token"],
            "Content-Type": "application/json",
        })
        self._me: str | None = None

    # --- http ---------------------------------------------------------
    def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        r = self.s.request(method, f"{API}{path}", timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"ClickUp {method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else {}

    # --- identity -----------------------------------------------------
    def whoami(self) -> str:
        if self._me is None:
            self._me = str(self._req("GET", "/user")["user"]["id"])
        return self._me

    # --- reads --------------------------------------------------------
    def fetch_candidates(self) -> list[Task]:
        params = [
            ("assignees[]", self.whoami()),
            ("statuses[]", self.ready_status),
            ("include_closed", "false"),
            ("subtasks", "true"),
            ("page", "0"),
        ]
        for lid in self.cfg.get("lists", []):
            params.append(("list_ids[]", str(lid)))
        data = self._req("GET", f"/team/{self.team_id}/task", params=params)
        tasks = [self._to_task(t) for t in data.get("tasks", [])]
        skip = {str(t).lower() for t in self.cfg.get("skip_tags", ["no-ai"])}
        return [t for t in tasks if not skip.intersection(t.tags)]

    def get_task(self, task_id: str) -> Task:
        return self._to_task(self._req("GET", f"/task/{task_id}"))

    def list_comments(self, task_id: str) -> list[Comment]:
        data = self._req("GET", f"/task/{task_id}/comment")
        out: list[Comment] = []
        for c in data.get("comments", []):
            text = c.get("comment_text") or ""
            out.append(Comment(
                id=str(c["id"]),
                author=(c.get("user") or {}).get("username", "?"),
                author_id=str((c.get("user") or {}).get("id", "")),
                text=text,
                created_at=int(c.get("date", 0)),
                is_bot=self.marker in text,
            ))
        out.sort(key=lambda c: c.created_at)
        return out

    # --- writes -------------------------------------------------------
    def add_comment(self, task_id: str, markdown: str) -> str:
        body = f"{self.marker}\n{markdown}"
        if self.dry_run:
            log.info("[dry-run] comment on %s:\n%s", task_id, markdown)
            return "dry-run"
        res = self._req("POST", f"/task/{task_id}/comment",
                        json={"comment_text": body, "notify_all": False})
        return str(res.get("id", ""))

    def set_ai_state(self, task_id: str, state: AIState) -> None:
        if self.dry_run:
            log.info("[dry-run] set state %s -> %s", task_id, state.value)
            return
        if self.state_backend == "custom_field":
            option_id = self.state_options.get(state.value)
            if not option_id:
                raise RuntimeError(f"No custom-field option mapped for state {state.value}")
            self._req("POST", f"/task/{task_id}/field/{self.state_field_id}",
                      json={"value": option_id})
        else:
            current = self.get_task(task_id)
            for tag in current.tags:
                if tag.startswith("ai:"):
                    self._req("DELETE", f"/task/{task_id}/tag/{tag}")
            self._req("POST", f"/task/{task_id}/tag/ai:{state.value}")

    def set_status(self, task_id: str, status: str) -> None:
        if self.dry_run:
            log.info("[dry-run] set status %s -> %s", task_id, status)
            return
        self._req("PUT", f"/task/{task_id}", json={"status": status})

    def blocked_by(self, task_id: str) -> list[dict[str, Any]]:
        """ClickUp task dependencies.

        Each entry carries `task_id` and `depends_on`. The row where
        `task_id == this task` means this task is waiting on `depends_on`.
        (ClickUp also returns the mirror row for the other direction, so the
        comparison rather than the `type` field is what disambiguates.)
        """
        data = self._req("GET", f"/task/{task_id}")
        out: list[dict[str, Any]] = []
        closed = {str(s).lower() for s in self.cfg.get("closed_statuses",
                                                       ["done", "closed", "complete"])}
        for dep in data.get("dependencies", []) or []:
            if str(dep.get("task_id")) != str(task_id):
                continue
            upstream = str(dep.get("depends_on") or "")
            if not upstream:
                continue
            try:
                other = self.get_task(upstream)
            except RuntimeError:
                continue    # deleted or not visible to us
            out.append({"key": other.key, "id": upstream,
                        "closed": other.status in closed})
        return out

    def branch_key(self, task: Task) -> str:
        """The task id in the form ClickUp's git integration parses.

        ClickUp links a branch, commit or PR to a task by finding the task id
        in its name, and a bare native id (`86abc1234`) is not enough — the
        integration looks for the `CU-` prefix. A Custom Task ID (`DEV-1234`)
        is matched verbatim instead, and is the more readable of the two, so it
        wins when the workspace has them enabled.
        """
        if task.raw.get("custom_id"):
            return task.key
        return f"CU-{task.id}"

    # --- mapping ------------------------------------------------------
    def _to_task(self, t: dict[str, Any]) -> Task:
        tags = [str(x.get("name", "")).lower() for x in t.get("tags", [])]
        lst = t.get("list") or {}
        return Task(
            id=str(t["id"]),
            key=str(t.get("custom_id") or t["id"]),
            title=t.get("name", ""),
            description=t.get("description") or t.get("text_content") or "",
            status=str((t.get("status") or {}).get("status", "")).lower(),
            url=t.get("url", ""),
            list_id=str(lst.get("id", "")),
            list_name=str(lst.get("name", "")),
            tags=tags,
            assignee_ids=[str(a.get("id")) for a in t.get("assignees", [])],
            updated_at=int(t.get("date_updated") or 0),
            ai_state=self._read_state(t, tags),
            raw=t,
        )

    def _read_state(self, t: dict[str, Any], tags: list[str]) -> AIState:
        if self.state_backend == "custom_field":
            for f in t.get("custom_fields", []):
                if str(f.get("id")) != str(self.state_field_id):
                    continue
                val = f.get("value")
                opts = (f.get("type_config") or {}).get("options", [])
                for i, opt in enumerate(opts):
                    if val in (i, str(opt.get("id")), opt.get("orderindex")):
                        name = str(opt.get("name", "")).lower()
                        try:
                            return AIState(name)
                        except ValueError:
                            return AIState.NEW
            return AIState.NEW
        for tag in tags:
            if tag.startswith("ai:"):
                try:
                    return AIState(tag[3:])
                except ValueError:
                    continue
        return AIState.NEW
