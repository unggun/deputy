"""Local SQLite store: idempotency, leases, retry counts, cost accounting.

The task manager holds the *visible* state; this holds the bookkeeping that
makes crashes and overlapping runs safe.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import AIState

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id              TEXT PRIMARY KEY,   -- namespaced: "<source_key>:<provider id>"
    source               TEXT NOT NULL DEFAULT '',
    provider_task_id     TEXT,
    task_key             TEXT,
    title                TEXT,
    state                TEXT NOT NULL,
    repo_key             TEXT,
    branch               TEXT,
    pr_number            INTEGER,
    pr_url               TEXT,
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_seen_comment_id TEXT,
    provider_updated_at  INTEGER NOT NULL DEFAULT 0,
    lease_owner          TEXT,
    lease_until          INTEGER NOT NULL DEFAULT 0,
    cost_usd             REAL NOT NULL DEFAULT 0,
    last_error           TEXT,
    meta                 TEXT NOT NULL DEFAULT '{}',
    updated_at           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    task_id  TEXT,
    kind     TEXT NOT NULL,
    detail   TEXT,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source, state);
CREATE TABLE IF NOT EXISTS posted_comments (
    idempotency_key TEXT PRIMARY KEY,
    task_id TEXT,
    ts INTEGER
);
"""


def _now() -> int:
    return int(time.time())


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- task rows ----------------------------------------------------
    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert(self, task_id: str, **fields: Any) -> None:
        fields.setdefault("state", AIState.NEW.value)
        fields["updated_at"] = _now()
        existing = self.get(task_id)
        if existing is None:
            cols = ["task_id", *fields.keys()]
            vals = [task_id, *[_coerce(v) for v in fields.values()]]
            ph = ",".join("?" * len(cols))
            self.conn.execute(
                f"INSERT INTO tasks ({','.join(cols)}) VALUES ({ph})", vals
            )
        else:
            sets = ",".join(f"{k} = ?" for k in fields)
            self.conn.execute(
                f"UPDATE tasks SET {sets} WHERE task_id = ?",
                [*[_coerce(v) for v in fields.values()], task_id],
            )
        self.conn.commit()

    def set_state(self, task_id: str, state: AIState, **extra: Any) -> None:
        self.upsert(task_id, state=state.value, **extra)

    def in_flight(self, source: str | None = None) -> list[dict[str, Any]]:
        terminal = [s.value for s in AIState if s.is_terminal]
        ph = ",".join("?" * len(terminal))
        sql = f"SELECT * FROM tasks WHERE state NOT IN ({ph})"
        args: list[Any] = list(terminal)
        if source is not None:
            sql += " AND source = ?"
            args.append(source)
        rows = self.conn.execute(sql + " ORDER BY updated_at ASC", args).fetchall()
        return [dict(r) for r in rows]

    def active_count(self) -> int:
        active = (AIState.IN_PROGRESS.value, AIState.PR_OPEN.value)
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE state IN (?, ?)", active
        ).fetchone()
        return int(row["c"])

    # --- leases -------------------------------------------------------
    def acquire_lease(self, task_id: str, owner: str, ttl_seconds: int) -> bool:
        now = _now()
        cur = self.conn.execute(
            "UPDATE tasks SET lease_owner = ?, lease_until = ? "
            "WHERE task_id = ? AND (lease_until < ? OR lease_owner = ?)",
            (owner, now + ttl_seconds, task_id, now, owner),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def release_lease(self, task_id: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET lease_owner = NULL, lease_until = 0 WHERE task_id = ?",
            (task_id,),
        )
        self.conn.commit()

    # --- accounting ---------------------------------------------------
    def record(self, kind: str, task_id: str | None = None,
               detail: str = "", cost_usd: float = 0.0) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, task_id, kind, detail, cost_usd) VALUES (?,?,?,?,?)",
            (_now(), task_id, kind, detail[:4000], cost_usd),
        )
        if task_id and cost_usd:
            self.conn.execute(
                "UPDATE tasks SET cost_usd = cost_usd + ? WHERE task_id = ?",
                (cost_usd, task_id),
            )
        self.conn.commit()

    def spend_since(self, epoch: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) s FROM events WHERE ts >= ?", (epoch,)
        ).fetchone()
        return float(row["s"])

    # --- comment idempotency -------------------------------------------
    def claim_comment(self, key: str, task_id: str) -> bool:
        """Returns True if this comment has not been posted before."""
        try:
            self.conn.execute(
                "INSERT INTO posted_comments (idempotency_key, task_id, ts) VALUES (?,?,?)",
                (key, task_id, _now()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def _coerce(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    if isinstance(v, AIState):
        return v.value
    return v
