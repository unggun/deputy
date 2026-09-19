"""Read-only queries over the runner's SQLite store."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from ..models import AIState
from ..redact import redact

# States that mean a human has to act before anything else happens.
_WAITING = (AIState.BLOCKED.value, AIState.HUMAN_ONLY.value,
            AIState.NEEDS_INPUT.value, AIState.AWAITING_PLAN_OK.value)
# Mirrors Store.active_count(): what occupies a max_active_tasks slot.
_ACTIVE = (AIState.IN_PROGRESS.value, AIState.REVIEW.value,
           AIState.PR_OPEN.value)
_TERMINAL = tuple(s.value for s in AIState if s.is_terminal)
# Terminal, but terminal *because a human has to act* -- that is the entire
# point of these two states, so they stay in the stuck list even though
# nothing will move them on its own. Every other terminal state (`done`) is
# finished, not wedged, and must never appear there.
_ATTENTION = (AIState.BLOCKED.value, AIState.HUMAN_ONLY.value)
# `state` rows are transition markers written by Pipeline._record_transition;
# they carry no cost and no tokens. One definition, used by every spend sum,
# so the "today" figure on the overview and the per-day chart cannot drift
# apart the day a state row starts carrying a number.
_SPEND_ONLY = "kind != 'state'"


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
        try:
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                        timeout=5, check_same_thread=False)
        except sqlite3.OperationalError as exc:
            # mode=ro refuses to create the file, so a missing db surfaces as
            # "unable to open database file" -- true, but useless to whoever
            # has to act on it. Name the path so the message alone is enough.
            if not path.exists():
                raise FileNotFoundError(
                    f"no state database at {path}") from exc
            raise
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

        Two rules keep this list emptiable, which is the only thing that makes
        it worth reading:

        * **The scan skips finished rows.** A `done` row is not stuck no
          matter what happened on the way there. `blocked` and `human-only`
          are terminal too, but they are terminal *because* a human has to
          act, so they stay. Written as "non-terminal, plus the two attention
          states" rather than "not `done`": a terminal state added later is
          excluded by default, which is the safe direction.
        * **`last_error` counts only on a non-terminal row.** Nothing in this
          codebase ever clears `last_error` -- one transient `gh` timeout
          during the implement pass is still on the row weeks later. On a row
          that has since moved on it is history, not a symptom. On a `blocked`
          row it is the escalation reason, already said by the waiting reason
          and rendered underneath it, so repeating it adds nothing. Only a
          row that is still supposed to be moving gets flagged for it.
        """
        cap = int(self.limits.get("max_fix_attempts", 3) or 3)
        sql = ("SELECT * FROM tasks WHERE state NOT IN (%s) OR state IN (%s)"
               % (",".join("?" * len(_TERMINAL)),
                  ",".join("?" * len(_ATTENTION))))
        out: list[dict[str, Any]] = []
        for row in self.conn.execute(sql, [*_TERMINAL, *_ATTENTION]):
            r = self._row(row)
            live = r["state"] not in _TERMINAL
            reasons: list[str] = []
            if r["state"] in _WAITING:
                reasons.append(f"waiting on a human ({r['state']})")
            if (r["state"] == AIState.IN_PROGRESS.value
                    and int(r["lease_until"] or 0) < now_epoch):
                reasons.append("lease expired -- the run that held it is gone")
            if int(r["attempts"] or 0) >= cap:
                reasons.append(f"attempts exhausted ({r['attempts']}/{cap})")
            if live and r["last_error"]:
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
            f"  FROM events WHERE ts >= ? AND {_SPEND_ONLY}"
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

        Reads the `state` events written by `Pipeline._record_transition`,
        called from all five `store.set_state` sites in pipeline.py -- not
        just `_set`/`_escalate`, which are two of the five. Each row's detail
        is exactly "<old>-><new>".
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
        """Seconds from a row's true first event to its transition to done.

        Two queries, deliberately: the `done` transition has to be found
        inside the reporting window (that's what "which rows completed in
        the last N days" means), but the row's *first* event can be far
        outside it -- a ticket blocked for weeks before finishing is not
        "born" on whatever day happens to be the window edge. Bounding the
        first-event scan by the same `ts >= since` as the done scan would
        silently understate the cycle time for exactly the rows this metric
        exists to catch, with nothing in the output to show the true start
        was excluded. So the unbounded MIN(ts) scan is restricted instead by
        task_id, to only the rows that actually completed in the window.
        """
        done_rows = self.conn.execute(
            "SELECT task_id, MAX(ts) done_ts FROM events"
            " WHERE kind = 'state' AND detail LIKE ? AND ts >= ?"
            " AND task_id IS NOT NULL GROUP BY task_id",
            (f"%->{AIState.DONE.value}", self._since(days))).fetchall()
        if not done_rows:
            return []
        done = {r["task_id"]: int(r["done_ts"]) for r in done_rows}
        task_ids = list(done)
        placeholders = ",".join("?" * len(task_ids))
        first_rows = self.conn.execute(
            f"SELECT task_id, MIN(ts) first_ts FROM events"
            f" WHERE task_id IN ({placeholders}) GROUP BY task_id",
            task_ids).fetchall()
        first = {r["task_id"]: int(r["first_ts"]) for r in first_rows}
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
            f" FROM events WHERE ts >= ? AND {_SPEND_ONLY}",
            (midnight,)).fetchone()
        last = self.conn.execute(
            "SELECT COALESCE(MAX(ts), 0) ts FROM events").fetchone()
        return {
            "counts": counts,
            "active": sum(counts.get(s, 0) for s in _ACTIVE),
            "max_active_tasks": int(self.limits.get("max_active_tasks", 3) or 3),
            "today": {"cost_usd": float(spent["c"]), "tokens": int(spent["t"])},
            # 20 matches pipeline._limits_exceeded's own default, deliberately
            # -- with the key unset in config, the pipeline still enforces a
            # $20 cap, and a dashboard that showed $0 here would disagree with
            # the gate it exists to report on.
            "daily_budget_usd": float(self.limits.get("daily_budget_usd", 20) or 0),
            "daily_tokens": int(self.limits.get("daily_tokens", 0) or 0),
            "stuck": len(self.stuck(int(now.timestamp()))),
            "last_event_ts": int(last["ts"]),
        }
