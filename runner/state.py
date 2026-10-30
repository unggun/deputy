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
    tokens               INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    meta                 TEXT NOT NULL DEFAULT '{}',
    updated_at           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    task_id         TEXT,
    kind            TEXT NOT NULL,
    detail          TEXT,
    cost_usd        REAL NOT NULL DEFAULT 0,
    tokens          INTEGER NOT NULL DEFAULT 0,
    -- Agent runs only. `ts` is when the run was recorded, i.e. when it ended;
    -- it started `duration_ms` earlier.
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    turns           INTEGER NOT NULL DEFAULT 0,
    model           TEXT,              -- what answered, most expensive first
    model_requested TEXT,              -- what `--model` asked for
    log             TEXT               -- station log name, e.g. implement-2
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source, state);
CREATE INDEX IF NOT EXISTS idx_tasks_ticket ON tasks(source, provider_task_id);
-- One verdict per (scorer, agent run). `label` is NULL while a judge holds
-- the claim, `insufficient_evidence` or `error` when there is no score.
CREATE TABLE IF NOT EXISTS scores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,       -- claimed at, then judged at
    scorer     TEXT NOT NULL,
    event_id   INTEGER NOT NULL,       -- events.id of the run judged
    task_id    TEXT,
    station    TEXT,                   -- events.kind
    log        TEXT,                   -- events.log
    label      TEXT,
    score      REAL,
    passed     INTEGER,
    pass_score REAL,
    reasoning  TEXT,
    cost_usd   REAL NOT NULL DEFAULT 0,
    tokens     INTEGER NOT NULL DEFAULT 0,
    model      TEXT,
    UNIQUE (scorer, event_id)
);
CREATE INDEX IF NOT EXISTS idx_scores_ts ON scores(ts);
-- One row per improve run on one repo, and the proposal it made, if any.
-- `evidence` is every ref the run was shown ("score:<scores.id>",
-- "review:<events.id>"); `cited` is the subset its proposal names.
CREATE TABLE IF NOT EXISTS improvements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,       -- claimed at, then finished at
    source     TEXT NOT NULL,
    repo_key   TEXT NOT NULL,
    -- running | pending | none | error | abandoned | accepted | rejected
    status     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT 'brain',
    problem    TEXT,
    solution   TEXT,
    note       TEXT,                   -- the exact text accepting would write
    reasoning  TEXT,                   -- the run's account, or why it failed
    evidence   TEXT NOT NULL DEFAULT '[]',
    cited      TEXT NOT NULL DEFAULT '[]',
    reason     TEXT,                   -- the human's, on a rejection
    decided_at INTEGER,
    cost_usd   REAL NOT NULL DEFAULT 0,
    tokens     INTEGER NOT NULL DEFAULT 0,
    model      TEXT,
    log        TEXT
);
-- At most one improve run holds a repo at a time; see claim_improvement.
CREATE UNIQUE INDEX IF NOT EXISTS idx_improvements_running
    ON improvements(source, repo_key) WHERE status = 'running';
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
        # WAL lets the read-only dashboard query this file while a tick is
        # writing. Without it a reader hits SQLITE_BUSY against the writer's
        # lock. Persistent once set; harmless for the single-writer cron path.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Additive column migrations for databases created by older versions.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so new
        columns in `SCHEMA` never reach a DB that already has the table. The
        alternative -- recreating the DB -- would drop every in-flight row's
        branch and PR number and orphan real work on the remote.
        """
        for table, column, decl in (
            ("events", "tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("tasks", "tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("events", "duration_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("events", "turns", "INTEGER NOT NULL DEFAULT 0"),
            ("events", "model", "TEXT"),
            ("events", "model_requested", "TEXT"),
            ("events", "log", "TEXT"),
        ):
            names = {r["name"] for r in
                     self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in names:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.conn.commit()

    # --- task rows ----------------------------------------------------
    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = _now()
        existing = self.get(task_id)
        if existing is None:
            fields.setdefault("state", AIState.NEW.value)
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

    def rows_for_task(self, source: str, provider_task_id: str) -> list[dict[str, Any]]:
        """Every local row for one remote ticket.

        A ticket that spans two repos has one row per repo, each with its own
        branch, PR and state. Row ids are not predictable before triage has
        chosen the repos, so "do I already know this ticket?" has to be a
        query on (source, provider_task_id) rather than a `get(row_id)`.
        """
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE source = ? AND provider_task_id = ?"
            " ORDER BY task_id ASC", (source, provider_task_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, task_id: str) -> None:
        """Drop one row. Used when a pre-triage placeholder is replaced by the
        per-repo rows triage actually routed the ticket to."""
        self.conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        self.conn.commit()

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
        """States that occupy one of `max_active_tasks` slots.

        `REVIEW` counts: the branch is pushed but the ticket will come back
        through `IN_PROGRESS` if the reviewer asks for changes, so it is real
        work in flight. `PLANNING` does not: it is read-only and pre-approval,
        and gating it would stall triage throughput for no safety gain.
        """
        active = (AIState.IN_PROGRESS.value, AIState.REVIEW.value,
                  AIState.PR_OPEN.value)
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE state IN (?, ?, ?)", active
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
               detail: str = "", cost_usd: float = 0.0,
               tokens: int = 0, *, duration_ms: int = 0, turns: int = 0,
               model: str | None = None, model_requested: str | None = None,
               log: str | None = None, ts: int | None = None) -> None:
        """One ledger line.

        `ts` defaults to now. Pass it only for a fact whose time is known
        better elsewhere -- the first human review of a PR happened when
        GitHub says it did, not when the next tick noticed.
        """
        self.conn.execute(
            "INSERT INTO events (ts, task_id, kind, detail, cost_usd, tokens,"
            " duration_ms, turns, model, model_requested, log)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_now() if ts is None else int(ts), task_id, kind, detail[:4000],
             cost_usd, int(tokens), int(duration_ms or 0), int(turns or 0),
             model, model_requested, log),
        )
        if task_id and (cost_usd or tokens):
            self.conn.execute(
                "UPDATE tasks SET cost_usd = cost_usd + ?, tokens = tokens + ?"
                " WHERE task_id = ?",
                (cost_usd, int(tokens), task_id),
            )
        self.conn.commit()

    def runs_recorded(self, task_id: str, kind: str) -> int:
        """How many `kind` runs this row has on the ledger.

        Numbers the next run's log (`models.run_station`). Counted from the
        ledger rather than a counter in `meta` so a run that crashed before
        it was recorded reuses its name instead of leaving a gap.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE task_id = ? AND kind = ?",
            (task_id, kind)).fetchone()
        return int(row["c"])

    def spend_since(self, epoch: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) s FROM events WHERE ts >= ?", (epoch,)
        ).fetchone()
        return float(row["s"])

    def tokens_since(self, epoch: int) -> int:
        """Tokens consumed since `epoch`.

        The counterpart to `spend_since`. On a subscription login the dollar
        figure is an API-equivalent estimate of work drawn from the
        developer's quota; this is the number that tracks the quota itself.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) s FROM events WHERE ts >= ?", (epoch,)
        ).fetchone()
        return int(row["s"])

    # --- scores ---------------------------------------------------------
    # A claim older than this with no label belongs to a judge that died
    # (a killed tick). Twice `reason()`'s own 300 s timeout, so a live judge
    # is never reclaimed from under itself.
    SCORE_CLAIM_TTL = 600

    def claim_score(self, scorer: str, event_id: int, *, task_id: str | None,
                    station: str | None, log: str | None) -> bool:
        """Take the right to judge one run. False if it is judged or held.

        Claimed before the judge runs, like `claim_comment`, so two
        overlapping ticks cannot both pay for the same verdict. A stale
        unlabelled claim is retaken rather than left to block the run forever.
        """
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO scores (ts, scorer, event_id, task_id, station, log)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(scorer, event_id) DO UPDATE SET ts = excluded.ts"
            " WHERE scores.label IS NULL AND scores.ts < ?",
            (now, scorer, int(event_id), task_id, station, log,
             now - self.SCORE_CLAIM_TTL))
        self.conn.commit()
        return cur.rowcount > 0

    def finish_score(self, scorer: str, event_id: int, *, label: str,
                     score: float | None, passed: bool | None,
                     pass_score: float | None, reasoning: str,
                     cost_usd: float = 0.0, tokens: int = 0,
                     model: str | None = None) -> None:
        self.conn.execute(
            "UPDATE scores SET ts = ?, label = ?, score = ?, passed = ?,"
            " pass_score = ?, reasoning = ?, cost_usd = ?, tokens = ?, model = ?"
            " WHERE scorer = ? AND event_id = ?",
            (_now(), label, score, None if passed is None else int(passed),
             pass_score, reasoning[:4000], cost_usd, int(tokens), model,
             scorer, int(event_id)))
        self.conn.commit()

    def unscored_runs(self, scorer: str, source: str, kinds: list[str],
                      since: int) -> list[dict[str, Any]]:
        """Runs of `kinds` on `source` since `since` that `scorer` has not
        judged, newest first. Only runs with a log: no transcript, no verdict.
        """
        ph = ",".join("?" * len(kinds))
        prefix = f"{source}:"
        rows = self.conn.execute(
            "SELECT e.* FROM events e LEFT JOIN scores s"
            "   ON s.event_id = e.id AND s.scorer = ?"
            f" WHERE e.kind IN ({ph}) AND e.log IS NOT NULL AND e.ts >= ?"
            "   AND substr(e.task_id, 1, ?) = ?"
            "   AND (s.id IS NULL OR (s.label IS NULL AND s.ts < ?))"
            " ORDER BY e.ts DESC, e.id DESC",
            (scorer, *kinds, int(since), len(prefix), prefix,
             _now() - self.SCORE_CLAIM_TTL)).fetchall()
        return [dict(r) for r in rows]

    def routed_after(self, task_id: str, event_id: int) -> bool:
        """Whether triage routed `task_id` again after ledger line `event_id`.

        `_route()` replaces a row's `meta` as it moves the row out of `new`
        into `planning` or `queued`: on a re-triage after a rejected plan, and
        on a human reset, which deletes the row but keeps its events. A run
        recorded before that was given a plan the row no longer holds. Ids,
        not timestamps: the ledger is append-only, and a fast tick records a
        run and the next transition inside the same second.
        """
        routed = [f"{AIState.NEW.value}->{s.value}"
                  for s in (AIState.PLANNING, AIState.QUEUED)]
        return self.conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND kind = 'state' AND id > ?"
            " AND detail IN (?, ?) LIMIT 1",
            (task_id, int(event_id), *routed)).fetchone() is not None

    def last_scored(self) -> dict[str, int]:
        """When each scorer last claimed a run: who has waited longest."""
        return {r["scorer"]: int(r["ts"]) for r in self.conn.execute(
            "SELECT scorer, MAX(ts) ts FROM scores GROUP BY scorer")}

    def event(self, event_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE id = ?", (int(event_id),)).fetchone()
        return dict(row) if row else None

    def recent_runs(self, kinds: list[str], limit: int) -> list[dict[str, Any]]:
        """The latest runs of `kinds` that left a log, newest first."""
        ph = ",".join("?" * len(kinds))
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE kind IN ({ph}) AND log IS NOT NULL"
            " ORDER BY ts DESC, id DESC LIMIT ?", (*kinds, int(limit))).fetchall()
        return [dict(r) for r in rows]

    # --- improvements --------------------------------------------------
    # Statuses whose evidence is spent: no later run is shown it again. An
    # `error` run produced nothing a human could act on, and an `abandoned`
    # claim belonged to a killed tick, so their evidence stays available.
    IMPROVE_CONSUMED = ("running", "pending", "none", "accepted", "rejected")

    def repo_of(self, task_id: str) -> str | None:
        """The repo a run belongs to, even after its row is gone.

        A routed id carries it after `#`, which survives a human reset that
        deleted the row. A legacy two-segment row only has it in the column.
        """
        if "#" in task_id:
            return task_id.split("#", 1)[1]
        return (self.get(task_id) or {}).get("repo_key")

    def _improve_seen(self, source: str, repo_key: str) -> set[str]:
        ph = ",".join("?" * len(self.IMPROVE_CONSUMED))
        seen: set[str] = set()
        for r in self.conn.execute(
                "SELECT evidence FROM improvements WHERE source = ? AND repo_key = ?"
                f" AND status IN ({ph})", (source, repo_key, *self.IMPROVE_CONSUMED)):
            try:
                seen.update(str(x) for x in json.loads(r["evidence"] or "[]"))
            except (TypeError, ValueError):
                continue
        return seen

    def improve_evidence(self, source: str, repo_key: str,
                         since: int) -> list[dict[str, Any]]:
        """Failures on one repo since `since` that no improve run has seen yet.

        Two kinds, oldest first: a scorer verdict that failed
        (`score:<scores.id>`, dated when it was judged) and a review that
        asked for changes or rejected the approach (`review:<events.id>`).
        Only runs that left a log: the log is the record of what the run did.
        """
        prefix = f"{source}:"
        found: list[dict[str, Any]] = []
        for r in self.conn.execute(
                "SELECT * FROM scores WHERE passed = 0 AND ts >= ?"
                " AND log IS NOT NULL AND substr(task_id, 1, ?) = ?",
                (int(since), len(prefix), prefix)):
            found.append({"ref": f"score:{r['id']}", "kind": "score",
                          "ts": int(r["ts"]), "task_id": r["task_id"],
                          "event_id": r["event_id"], "station": r["station"],
                          "log": r["log"], "scorer": r["scorer"],
                          "label": r["label"], "reasoning": r["reasoning"]})
        for r in self.conn.execute(
                "SELECT * FROM events WHERE kind = 'review'"
                " AND detail IN ('request_changes', 'reject')"
                " AND log IS NOT NULL AND ts >= ? AND substr(task_id, 1, ?) = ?",
                (int(since), len(prefix), prefix)):
            found.append({"ref": f"review:{r['id']}", "kind": "review",
                          "ts": int(r["ts"]), "task_id": r["task_id"],
                          "event_id": r["id"], "station": "review",
                          "log": r["log"], "verdict": r["detail"]})
        seen = self._improve_seen(source, repo_key)
        out = [e for e in found if e["ref"] not in seen
               and self.repo_of(e["task_id"]) == repo_key]
        # Ids break a tie numerically: "review:10" sorts after "review:9".
        out.sort(key=lambda e: (e["ts"], e["kind"], int(e["ref"].partition(":")[2])))
        return out

    def claim_improvement(self, source: str, repo_key: str, *, ttl: int) -> int | None:
        """Take one repo for an improve run. None if another run holds it.

        The partial unique index allows one `running` row per repo, so two
        overlapping ticks cannot both pay for the same proposal. A claim older
        than `ttl` belongs to a tick that was killed mid-run; it is marked
        `abandoned`, which hands its evidence back, and retaken.
        """
        now = _now()
        self.conn.execute(
            "UPDATE improvements SET status = 'abandoned',"
            " reasoning = 'the run holding this claim never finished'"
            " WHERE source = ? AND repo_key = ? AND status = 'running' AND ts < ?",
            (source, repo_key, now - int(ttl)))
        try:
            cur = self.conn.execute(
                "INSERT INTO improvements (ts, source, repo_key, status)"
                " VALUES (?, ?, ?, 'running')", (now, source, repo_key))
        except sqlite3.IntegrityError:
            self.conn.commit()
            return None
        self.conn.commit()
        return int(cur.lastrowid)

    _IMPROVE_FIELDS = ("target", "problem", "solution", "note", "reasoning",
                       "evidence", "cited", "cost_usd", "tokens", "model", "log")

    def finish_improvement(self, improvement_id: int, status: str,
                           **fields: Any) -> None:
        """Close a claim. A claim already abandoned stays abandoned: its run
        came back after another tick had given its evidence away."""
        sets = {k: _coerce(v) for k, v in fields.items() if k in self._IMPROVE_FIELDS}
        sets.update(status=status, ts=_now())
        self.conn.execute(
            f"UPDATE improvements SET {', '.join(f'{k} = ?' for k in sets)}"
            " WHERE id = ? AND status = 'running'",
            (*sets.values(), int(improvement_id)))
        self.conn.commit()

    def decide_improvement(self, improvement_id: int, status: str,
                           reason: str | None = None) -> bool:
        """Accept or reject a pending proposal. False if it is not pending."""
        cur = self.conn.execute(
            "UPDATE improvements SET status = ?, reason = ?, decided_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (status, reason, _now(), int(improvement_id)))
        self.conn.commit()
        return cur.rowcount > 0

    def undecide_improvement(self, improvement_id: int, status: str) -> None:
        """Put a decision back to pending: an accept the brain's cap refused."""
        self.conn.execute(
            "UPDATE improvements SET status = 'pending', reason = NULL,"
            " decided_at = NULL WHERE id = ? AND status = ?",
            (int(improvement_id), status))
        self.conn.commit()

    def score(self, score_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM scores WHERE id = ?",
                                (int(score_id),)).fetchone()
        return dict(row) if row else None

    def improvement(self, improvement_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM improvements WHERE id = ?",
                                (int(improvement_id),)).fetchone()
        return dict(row) if row else None

    def improvements(self, *, source: str | None = None,
                     repo_key: str | None = None,
                     statuses: tuple[str, ...] | None = None,
                     limit: int | None = 200) -> list[dict[str, Any]]:
        """Improve runs, newest first. `limit=None` returns every one."""
        sql, args = "SELECT * FROM improvements WHERE 1 = 1", []
        if source is not None:
            sql += " AND source = ?"
            args.append(source)
        if repo_key is not None:
            sql += " AND repo_key = ?"
            args.append(repo_key)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        rows = self.conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?",
                                 (*args, -1 if limit is None else int(limit)))
        return [dict(r) for r in rows]

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

    def forget_comments(self, task_id: str) -> None:
        """Release every comment claim for one row.

        Only for a row a human has reset to start over: its old claims (the
        `human_only` verdict, the plan) would otherwise silence the fresh
        run's comments for good, and the new verdict would never be posted.
        """
        self.conn.execute("DELETE FROM posted_comments WHERE task_id = ?", (task_id,))
        self.conn.commit()


def _coerce(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    if isinstance(v, AIState):
        return v.value
    return v
