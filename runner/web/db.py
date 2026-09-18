"""Read-only queries over the runner's SQLite store."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..models import AIState
from ..redact import redact

# States that mean a human has to act before anything else happens.
_WAITING = (AIState.BLOCKED.value, AIState.HUMAN_ONLY.value,
            AIState.NEEDS_INPUT.value, AIState.AWAITING_PLAN_OK.value)
# Mirrors Store.active_count(): what occupies a max_active_tasks slot.
_ACTIVE = (AIState.IN_PROGRESS.value, AIState.REVIEW.value,
           AIState.PR_OPEN.value)
_TERMINAL = tuple(s.value for s in AIState if s.is_terminal)


def _midnight_epoch(now: dt.datetime) -> int:
    """Local midnight, matching pipeline._limits_exceeded.

    Duplicated rather than imported: importing `pipeline` would pull the
    providers, the agent adapter and `requests` into a process whose only job
    is to read a file. The two must agree, so the definition is one line and
    a test pins it.
    """
    return int(now.replace(hour=0, minute=0, second=0,
                           microsecond=0).timestamp())


class Reader:
    """Every read the dashboard performs. Opens the DB read-only."""

    # Fields that can carry subprocess output and must be scrubbed on egress.
    _DIRTY = ("last_error", "detail", "title")

    def __init__(self, db_path: str | Path, *,
                 secrets: Sequence[str] = (),
                 limits: dict[str, Any] | None = None) -> None:
        path = Path(db_path).expanduser()
        # mode=ro fails writes at the SQLite layer, so a future convenience
        # helper cannot quietly gain one. timeout covers the brief window
        # where a tick holds the write lock.
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                    timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.secrets = list(secrets)
        self.limits = limits or {}

    def close(self) -> None:
        self.conn.close()

    # --- egress ------------------------------------------------------
    def _clean(self, text: str | None) -> str | None:
        if not text:
            return text
        return redact(text, self.secrets)

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        for key in self._DIRTY:
            if key in out:
                out[key] = self._clean(out[key])
        return out

    # --- tasks -------------------------------------------------------
    def tasks(self, *, source: str | None = None, state: str | None = None,
              include_terminal: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        args: list[Any] = []
        if not include_terminal:
            sql += f" AND state NOT IN ({','.join('?' * len(_TERMINAL))})"
            args += list(_TERMINAL)
        if source:
            sql += " AND source = ?"
            args.append(source)
        if state:
            sql += " AND state = ?"
            args.append(state)
        sql += " ORDER BY updated_at DESC"
        return [self._row(r) for r in self.conn.execute(sql, args)]

    def task(self, row_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (row_id,)).fetchone()
        if row is None:
            return None
        out = self._row(row)
        out["events"] = [self._row(e) for e in self.conn.execute(
            "SELECT * FROM events WHERE task_id = ? ORDER BY ts ASC, id ASC",
            (row_id,))]
        return out

    # --- stuck -------------------------------------------------------
    def stuck(self, now_epoch: int) -> list[dict[str, Any]]:
        """Rows that need a human. The definition lives here, once.

        Each row carries a `reason` so the UI renders an explanation rather
        than a color.
        """
        cap = int(self.limits.get("max_fix_attempts", 3) or 3)
        out: list[dict[str, Any]] = []
        for row in self.conn.execute("SELECT * FROM tasks"):
            r = self._row(row)
            reasons: list[str] = []
            if r["state"] in _WAITING:
                reasons.append(f"waiting on a human ({r['state']})")
            if (r["state"] == AIState.IN_PROGRESS.value
                    and int(r["lease_until"] or 0) < now_epoch):
                reasons.append("lease expired -- the run that held it is gone")
            if int(r["attempts"] or 0) >= cap:
                reasons.append(f"attempts exhausted ({r['attempts']}/{cap})")
            if r["last_error"]:
                reasons.append("last run recorded an error")
            if reasons:
                r["reason"] = "; ".join(reasons)
                out.append(r)
        out.sort(key=lambda r: r["updated_at"], reverse=True)
        return out

    # --- accounting --------------------------------------------------
    def _since(self, days: int) -> int:
        start = dt.datetime.now() - dt.timedelta(days=days - 1)
        return _midnight_epoch(start)

    def spend_by_day(self, days: int = 30) -> list[dict[str, Any]]:
        """Per local day, split by event kind.

        `date(ts,'unixepoch','localtime')` buckets on the same local midnight
        the runner's daily gates use. `ts / 86400` would bucket on UTC and
        split an evening's work across two bars.
        """
        rows = self.conn.execute(
            "SELECT date(ts,'unixepoch','localtime') day, kind,"
            "       SUM(cost_usd) cost_usd, SUM(tokens) tokens"
            "  FROM events WHERE ts >= ? AND kind != 'state'"
            " GROUP BY day, kind ORDER BY day ASC", (self._since(days),))
        by_day: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = by_day.setdefault(r["day"], {"day": r["day"], "cost_usd": 0.0,
                                             "tokens": 0, "kinds": {}})
            d["kinds"][r["kind"]] = {"cost_usd": float(r["cost_usd"] or 0),
                                     "tokens": int(r["tokens"] or 0)}
            d["cost_usd"] += float(r["cost_usd"] or 0)
            d["tokens"] += int(r["tokens"] or 0)
        return list(by_day.values())

    def cost_by_task(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT task_key, source, repo_key, cost_usd, tokens FROM tasks"
            " WHERE cost_usd > 0 OR tokens > 0"
            " ORDER BY tokens DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]

    # --- throughput --------------------------------------------------
    def throughput_by_day(self, days: int = 30) -> list[dict[str, Any]]:
        """Transitions into pr-open / done / blocked, per local day.

        Reads the `state` events written by pipeline._set and _escalate,
        whose detail is exactly "<old>-><new>".
        """
        rows = self.conn.execute(
            "SELECT date(ts,'unixepoch','localtime') day, detail FROM events"
            " WHERE kind = 'state' AND ts >= ? ORDER BY day ASC",
            (self._since(days),))
        by_day: dict[str, dict[str, Any]] = {}
        for r in rows:
            dest = str(r["detail"] or "").split("->")[-1]
            d = by_day.setdefault(r["day"], {"day": r["day"], "pr_open": 0,
                                             "done": 0, "blocked": 0})
            if dest == AIState.PR_OPEN.value:
                d["pr_open"] += 1
            elif dest == AIState.DONE.value:
                d["done"] += 1
            elif dest == AIState.BLOCKED.value:
                d["blocked"] += 1
        return list(by_day.values())

    def cycle_times(self, days: int = 30) -> list[dict[str, Any]]:
        """Seconds from a row's first recorded event to its transition to done."""
        first: dict[str, int] = {}
        done: dict[str, int] = {}
        for r in self.conn.execute(
                "SELECT ts, task_id, kind, detail FROM events"
                " WHERE ts >= ? ORDER BY ts ASC", (self._since(days),)):
            tid = r["task_id"]
            if not tid:
                continue
            first.setdefault(tid, int(r["ts"]))
            if (r["kind"] == "state"
                    and str(r["detail"] or "").endswith(f"->{AIState.DONE.value}")):
                done[tid] = int(r["ts"])
        return [{"task_id": tid, "seconds": done[tid] - first[tid]}
                for tid in sorted(done) if tid in first]

    def review_cycles(self, days: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT task_id, COUNT(*) n FROM events"
            " WHERE kind = 'review' AND ts >= ? GROUP BY task_id",
            (self._since(days),))
        return [{"task_id": r["task_id"], "cycles": int(r["n"])} for r in rows]

    # --- overview ----------------------------------------------------
    def overview(self, now: dt.datetime) -> dict[str, Any]:
        midnight = _midnight_epoch(now)
        counts = {r["state"]: int(r["n"]) for r in self.conn.execute(
            "SELECT state, COUNT(*) n FROM tasks GROUP BY state")}
        spent = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(tokens),0) t"
            " FROM events WHERE ts >= ?", (midnight,)).fetchone()
        last = self.conn.execute(
            "SELECT COALESCE(MAX(ts), 0) ts FROM events").fetchone()
        return {
            "counts": counts,
            "active": sum(counts.get(s, 0) for s in _ACTIVE),
            "max_active_tasks": int(self.limits.get("max_active_tasks", 3) or 3),
            "today": {"cost_usd": float(spent["c"]), "tokens": int(spent["t"])},
            "daily_budget_usd": float(self.limits.get("daily_budget_usd", 0) or 0),
            "daily_tokens": int(self.limits.get("daily_tokens", 0) or 0),
            "stuck": len(self.stuck(int(now.timestamp()))),
            "last_event_ts": int(last["ts"]),
        }
