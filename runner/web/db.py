"""Read-only queries over the runner's SQLite store."""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

from ..models import AIState, model_present
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
# Ledger lines that mark something happening rather than an agent run:
# `state` rows from Pipeline._record_transition, and the PR milestones the
# poll notes (`pr_ready`, `pr_review`, `pr_merged`). None carries cost or
# tokens. One definition, used by every spend sum, so the "today" figure on
# the overview and the per-day chart cannot drift apart -- and so a marker
# never grows a zero-height station in the spend chart's legend.
_MARKERS = ("state", "pr_ready", "pr_review", "pr_merged")
_SPEND_ONLY = f"kind NOT IN ({','.join(repr(k) for k in _MARKERS)})"
# Agent runs, the rows `runs()` lists.
_RUN_KINDS = ("triage", "plan", "implement", "review", "fix")
_HUMAN_COMMITS = re.compile(r"human_commits=(\d+|\?)")


def _placeholder(task_id: str) -> str:
    """The pre-triage row a routed row grew from: the id before its `#`.

    Triage, and any clarification round before routing, is recorded against
    `{source}:{ticket}`; the routed `{source}:{ticket}#{repo}` rows replace it.
    A ticket's history is both ids' events together.
    """
    return task_id.split("#", 1)[0]


def _outcome(kind: str, detail: str | None) -> tuple[str, bool]:
    """(one-word outcome, failed?) for an agent run's ledger line.

    Every station writes `failed: ...` when its agent run itself failed.
    Otherwise implement and fix write `ok` or the error (fix prefixed with
    its cause, `ci: ok`), and the rest write their verdict.
    """
    text = (detail or "").strip()
    if text.startswith("failed"):
        return "failed", True
    if kind in ("implement", "fix"):
        result = text.split(": ", 1)[1] if kind == "fix" and _fix_cause(text) else text
        return ("ok", False) if result == "ok" else ("failed", True)
    if kind == "triage":
        return text.split(" ", 1)[0] or "?", False
    return text or "?", False


def _fix_cause(detail: str | None) -> set[str] | None:
    """`{"ci"}`, `{"feedback"}`, both, or None for a line that predates causes."""
    head, sep, _ = (detail or "").partition(": ")
    parts = set(head.split("+")) if sep else set()
    return parts if parts and parts <= {"ci", "feedback"} else None


def _median(values: list[int]) -> float | None:
    xs = sorted(values)
    if not xs:
        return None
    mid = len(xs) // 2
    return float(xs[mid]) if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


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
        """One row, with its whole history.

        The events include the placeholder's: triage and any clarification
        round before routing were recorded against `{source}:{ticket}`, and a
        routed row's timeline that starts at `queued` hides the part of the
        cycle a human spent answering questions.
        """
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (row_id,)).fetchone()
        if row is None:
            return None
        out = self._row(row)
        out["events"] = [self._row(e) for e in self.conn.execute(
            "SELECT * FROM events WHERE task_id IN (?, ?)"
            " ORDER BY ts ASC, id ASC", (row_id, _placeholder(row_id)))]
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
        called from all six state-write sites in pipeline.py -- not just
        `_set`/`_escalate`, which are two of them. The other four write state
        directly: the crash-retry back to `queued`, the start of
        `in-progress`, the move to `pr-open`, and `_orphan()`, which is the
        only one that *creates* a row at `blocked` rather than moving one.
        Each row's detail is exactly "<old>-><new>".
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

    # --- runs ----------------------------------------------------------
    def _task_index(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Rows by id, and one routed row per ticket by placeholder id."""
        by_id: dict[str, dict[str, Any]] = {}
        by_ticket: dict[str, dict[str, Any]] = {}
        for row in self.conn.execute(
                "SELECT task_id, task_key, repo_key, title, pr_url, pr_number"
                " FROM tasks ORDER BY task_id ASC"):
            r = self._row(row)
            by_id[r["task_id"]] = r
            by_ticket.setdefault(_placeholder(r["task_id"]), r)
        return by_id, by_ticket

    def runs(self, days: int = 30, *, station: str | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
        """Every agent run in the window, newest first.

        `started` is derived, not stored: a run's ledger line is written when
        it ends, `duration_ms` after it began. A triage run belongs to the
        ticket's placeholder row, which routing deletes, so it is attributed
        to the ticket through any surviving row and `open_id` names a row
        the task sheet can actually open.
        """
        sql = (f"SELECT * FROM events WHERE kind IN ({','.join('?' * len(_RUN_KINDS))})"
               " AND ts >= ?")
        args: list[Any] = [*_RUN_KINDS, self._since(days)]
        if station:
            sql += " AND kind = ?"
            args.append(station)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(int(limit))
        by_id, by_ticket = self._task_index()
        out: list[dict[str, Any]] = []
        for row in self.conn.execute(sql, args):
            e = self._row(row)
            tid = e["task_id"] or ""
            owner = by_id.get(tid) or by_ticket.get(_placeholder(tid)) or {}
            outcome, failed = _outcome(e["kind"], e["detail"])
            ran = [m for m in (e.get("model") or "").split(",") if m]
            wanted = e.get("model_requested")
            out.append({
                "id": e["id"], "ts": e["ts"],
                "started": e["ts"] - int(e.get("duration_ms") or 0) // 1000,
                "task_id": tid, "open_id": owner.get("task_id"),
                "task_key": owner.get("task_key"),
                # A placeholder's run (triage) covered every repo, not one.
                "repo_key": owner.get("repo_key") if tid in by_id else None,
                "title": owner.get("title"),
                "kind": e["kind"], "detail": e["detail"],
                "outcome": outcome, "failed": failed,
                "cost_usd": float(e["cost_usd"] or 0), "tokens": int(e["tokens"] or 0),
                "duration_ms": int(e.get("duration_ms") or 0),
                "turns": int(e.get("turns") or 0),
                "model": ran[0] if ran else None, "models": ran,
                "model_requested": wanted,
                "model_mismatch": bool(wanted and ran and not model_present(wanted, ran)),
                "log": e.get("log"),
            })
        return out

    # --- flow ----------------------------------------------------------
    def flow(self, days: int = 30) -> dict[str, Any]:
        """Where time and human attention went, per PR merged in the window.

        Per merged row: time to PR, PR to first human review, first review to
        merge, the time spent waiting on people, and the human touches along
        the way. Each row's history is its own events plus its placeholder's,
        so a clarification round before routing is counted.

        Waiting on people = time in `needs-input` and `plan-review`, plus the
        PR's wait from ready-for-review (`pr_ready`, else `pr-open`) to the
        first human review (else the merge). Timestamps come from ticks, so
        state durations are only as fine as the tick interval; the first
        review is GitHub's own time.

        Autonomous = merged with no fix pass triggered by a human comment and
        no commit on the PR that this runner did not make. Rows that cannot
        say -- pushed before SHA tracking, a fix line with no cause -- are
        `None`, and the rate is over the rows that can. Clarifications and
        plan approvals are gates by design; they are counted as touches but
        never held against autonomy.
        """
        since = self._since(days)
        done = {r["task_id"]: int(r["done_ts"]) for r in self.conn.execute(
            "SELECT task_id, MAX(ts) done_ts FROM events"
            " WHERE kind = 'state' AND detail LIKE ? AND ts >= ?"
            " AND task_id IS NOT NULL GROUP BY task_id",
            (f"%->{AIState.DONE.value}", since))}
        by_id, _ = self._task_index()
        rows = [self._flow_row(tid, done_at, by_id.get(tid) or {})
                for tid, done_at in sorted(done.items(), key=lambda kv: -kv[1])]
        return {"rows": rows, "summary": _flow_summary(rows)}

    def _flow_row(self, tid: str, done_at: int, owner: dict[str, Any]) -> dict[str, Any]:
        ids = list(dict.fromkeys([tid, _placeholder(tid)]))
        evs = [dict(r) for r in self.conn.execute(
            f"SELECT id, ts, task_id, kind, detail FROM events"
            f" WHERE task_id IN ({','.join('?' * len(ids))}) AND ts <= ?"
            " ORDER BY ts ASC, id ASC", (*ids, done_at))]
        start = evs[0]["ts"] if evs else done_at
        states = [e for e in evs if e["kind"] == "state"]
        in_state: dict[str, int] = defaultdict(int)
        for cur, nxt in zip(states, states[1:] + [{"ts": done_at}]):
            dest = str(cur["detail"] or "").split("->")[-1]
            if dest != AIState.DONE.value:
                in_state[dest] += max(0, int(nxt["ts"]) - int(cur["ts"]))

        def first(kind: str, dest: str | None = None) -> int | None:
            hits = [e["ts"] for e in evs if e["kind"] == kind and e["task_id"] == tid
                    and (dest is None or str(e["detail"]).endswith(f"->{dest}"))]
            return min(hits) if hits else None

        pr_open = first("state", AIState.PR_OPEN.value)
        ready = first("pr_ready") or pr_open
        review = first("pr_review")
        if review is not None and pr_open is not None and review < pr_open:
            review = None           # a stale latch from an earlier PR; ignore it
        pr_wait = (max(0, (review or done_at) - ready) if ready is not None else 0)
        # `.get`, not `[]`: indexing the defaultdict would plant zero-second
        # states in `in_state` for every row that never visited them.
        waiting = (in_state.get(AIState.NEEDS_INPUT.value, 0)
                   + in_state.get(AIState.AWAITING_PLAN_OK.value, 0) + pr_wait)

        fixes = [e for e in evs if e["kind"] == "fix" and e["task_id"] == tid]
        causes = [_fix_cause(e["detail"]) for e in fixes]
        merged = [e for e in evs if e["kind"] == "pr_merged" and e["task_id"] == tid]
        m = _HUMAN_COMMITS.search(str(merged[-1]["detail"])) if merged else None
        human_commits = int(m.group(1)) if m and m.group(1).isdigit() else None
        fix_feedback = sum(1 for c in causes if c and "feedback" in c)
        known = human_commits is not None and all(c is not None for c in causes)
        transitions = [str(e["detail"]) for e in states]
        return {
            "task_id": tid, "task_key": owner.get("task_key"),
            "repo_key": owner.get("repo_key"), "title": owner.get("title"),
            "pr_url": owner.get("pr_url"), "pr_number": owner.get("pr_number"),
            "done_at": done_at,
            "total": done_at - start,
            "to_pr": pr_open - start if pr_open is not None else None,
            # Clamped: a person can review the draft before the runner marks
            # it ready, and `pr_ready` is tick time while `pr_review` is
            # GitHub's. That PR waited no time for review, not negative time.
            "pr_to_review": (max(0, review - ready)
                             if review is not None and ready is not None else None),
            "review_to_merge": done_at - review if review is not None else None,
            "waiting": waiting,
            "in_state": dict(in_state),
            "clarifications": transitions.count(
                f"{AIState.NEEDS_INPUT.value}->{AIState.NEW.value}"),
            "plan_rejections": transitions.count(
                f"{AIState.AWAITING_PLAN_OK.value}->{AIState.NEW.value}"),
            "review_rounds": sum(1 for e in evs if e["kind"] == "review"
                                 and e["task_id"] == tid
                                 and e["detail"] == "request_changes"),
            "fix_ci": sum(1 for c in causes if c and "ci" in c),
            "fix_feedback": fix_feedback,
            "human_commits": human_commits,
            "autonomous": (fix_feedback == 0 and human_commits == 0) if known else None,
        }

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


_FLOW_STAGES = ("to_pr", "pr_to_review", "review_to_merge", "total", "waiting")
_TOUCHES = ("clarifications", "plan_rejections", "review_rounds", "fix_ci",
            "fix_feedback")


def _flow_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Medians per stage, the waiting share, autonomy and touch totals.

    Every median carries its own `n`: a stage only some rows reached (a PR
    merged with no human review has no "first review to merge") is the
    median of those rows, and saying how many is the honest way to show it.
    `waiting_share` is total waiting over total cycle time, not a median of
    ratios, so a one-hour ticket cannot outvote a one-week one.
    """
    stages = {k: [r[k] for r in rows if r[k] is not None] for k in _FLOW_STAGES}
    time_by_state: dict[str, int] = defaultdict(int)
    for r in rows:
        for state, secs in r["in_state"].items():
            time_by_state[state] += secs
    total = sum(r["total"] for r in rows)
    known = [r for r in rows if r["autonomous"] is not None]
    auto = sum(1 for r in known if r["autonomous"])
    return {
        "merged": len(rows),
        "median": {k: _median(v) for k, v in stages.items()},
        "n": {k: len(v) for k, v in stages.items()},
        "waiting_share": (sum(r["waiting"] for r in rows) / total) if total else None,
        "time_by_state": dict(time_by_state),
        "autonomy": {"known": len(known), "autonomous": auto,
                     "rate": (auto / len(known)) if known else None},
        "touches": {k: sum(r[k] for r in rows) for k in _TOUCHES},
        "human_commits": sum(r["human_commits"] or 0 for r in rows),
    }
