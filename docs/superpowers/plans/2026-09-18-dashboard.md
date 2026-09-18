# Local Read-Only Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A localhost, read-only web dashboard that answers what is stuck, what is running, what it costs, and whether the work lands.

**Architecture:** A separate process reads the same SQLite file and JSONL log directory the cron tick writes, over a physically read-only connection (`mode=ro`). A thin FastAPI layer serves JSON plus a committed Vite build. The runner is unaware the dashboard exists; nothing is added to a tick.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, SQLite. Vite + React + TypeScript + Tailwind + shadcn/ui + TanStack Query + TanStack Table + Recharts + mermaid.

**Spec:** `docs/superpowers/specs/2026-09-18-dashboard-design.md`

## Global Constraints

- **No write path anywhere.** The API opens SQLite with `mode=ro`. No route mutates the DB, the provider, or any file.
- **`cli.py` must never import `runner.web` at module level.** Lazy import inside the `serve` handler only; `ImportError` becomes an instruction to run `pip install -e '.[web]'`.
- **Every string that can carry subprocess output is passed through `redact.redact(text, secrets)` before serialization** — `tasks.last_error`, `events.detail`, every agent log line.
- **Row ids are query parameters, never path segments.** Ids are `{source}:{provider_task_id}#{repo_key}`; `#` is the URL fragment delimiter and a path segment is truncated client-side before the request is sent.
- **Day buckets follow `pipeline._midnight_epoch()` — local midnight, never `ts / 86400` (UTC).**
- **Tests are plain PASS/FAIL scripts, not pytest.** They print per case and `sys.exit(0 if all(results) else 1)`, matching `tests_smoke.py`.
- **Default bind is `127.0.0.1`.** A non-loopback `--host` is allowed but warns.
- **Vite builds into `runner/web/static/`, which is committed.** Never `web/dist` — `.gitignore` has an unanchored `dist/`.
- **Categorical hues (station kinds) and the reserved status palette (task states) must not overlap.** Palettes are validated with `scripts/validate_palette.js` for light and dark separately before any chart code is written.

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `runner/web/__init__.py` | Package marker. Empty. Must not import FastAPI. |
| `runner/web/db.py` | `Reader` — read-only SQLite queries returning plain dicts. No HTTP imports. |
| `runner/web/logs.py` | JSONL log discovery and byte-offset tail. No HTTP imports. |
| `runner/web/api.py` | FastAPI routes. Thin: parse, call `Reader`/`logs`, serialize. |
| `runner/web/static/` | Committed Vite build output. |
| `web/` | Vite + React + TS source. Node lives here and only here. |
| `tests_web.py` | Offline PASS/FAIL script for `db.py` and `logs.py`. |

**Modified:**

| Path | Change |
|---|---|
| `runner/state.py:64` | `PRAGMA journal_mode=WAL` in `Store.__init__`. |
| `runner/pipeline.py:892` | `state` ledger event in `_set()`. |
| `runner/pipeline.py:912` | `state` ledger event in `_escalate()`. |
| `runner/pipeline.py:452,514,564,709` | Per-repo agent log filenames (Task 3). |
| `runner/cli.py:188-203` | `serve` subcommand; `_doctor` web checks. |
| `pyproject.toml` | `web` optional extra, explicit `packages.find`, `package-data`. |
| `.gitignore` | `*.db-wal`, `*.db-shm`, `web/node_modules/`. |
| `tests_smoke.py` | Cases for `state` events and per-repo log paths. |
| `README.md`, `CLAUDE.md` | Dashboard section and invariants. |

---

## Task 1: State-change ledger events and WAL

The `events` ledger records agent runs only (`triage`, `plan`, `implement`, `review`, `fix`). Throughput over time needs transitions. `_set()` is the funnel for normal transitions; `_escalate()` writes `blocked` directly via `store.set_state` and bypasses it, so both need the line or escalations go uncounted.

**Files:**
- Modify: `runner/state.py:64-72` (`Store.__init__`)
- Modify: `runner/pipeline.py:892-897` (`_set`)
- Modify: `runner/pipeline.py:912-918` (`_escalate`)
- Modify: `.gitignore`
- Test: `tests_smoke.py` (append a new check block)

**Interfaces:**
- Consumes: nothing.
- Produces: `events` rows with `kind == "state"` and `detail == "<old>-><new>"`. Task 2's `throughput_by_day` and `cycle_times` parse exactly this format.

- [ ] **Step 1: Write the failing test**

Append to `tests_smoke.py`, immediately before the final `for label, ok in _c_checks:` loop:

```python
# --- state transitions reach the ledger ------------------------------------
# Throughput charts reconstruct history from these rows. _escalate() writes
# state directly rather than through _set(), so it needs its own line or every
# escalation is invisible to the dashboard.
with tempfile.TemporaryDirectory() as _tmp:
    _cfg = Config.load("config.example.yaml")
    _cfg.data["runner"]["db_path"] = f"{_tmp}/s.db"
    _cfg.data["runner"]["work_dir"] = f"{_tmp}/w"
    _cfg.data["runner"]["log_dir"] = f"{_tmp}/l"
    _src = _cfg.sources(only=["mycorp"])[0]
    _task = mk("T90", "Add pagination to /users", "add ?page and ?limit")
    _prov = FakeProvider([_task])
    _store = Store(f"{_tmp}/s.db")
    _pipe = Pipeline(_cfg, _src, _prov, FakeAgent(
        {"decision": "clarify", "confidence": 0.4, "scope": "s",
         "questions": ["Which endpoint?"]}), _store)
    _pipe.tick(dt.datetime(2026, 8, 10, 7, 0))

    _state_rows = _store.conn.execute(
        "SELECT detail FROM events WHERE kind = 'state'").fetchall()
    _c_checks.append(("_set() records a state transition",
                      any("->needs-input" in r["detail"] for r in _state_rows)))

    _rid = [r["task_id"] for r in _store.in_flight()][0]
    _pipe._escalate(_rid, "boom")
    _esc_rows = _store.conn.execute(
        "SELECT detail FROM events WHERE kind = 'state'").fetchall()
    _c_checks.append(("_escalate() records a state transition",
                      any("->blocked" in r["detail"] for r in _esc_rows)))

    _c_checks.append(("store is in WAL mode",
                      _store.conn.execute("PRAGMA journal_mode").fetchone()[0]
                      .lower() == "wal"))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tests_smoke.py`
Expected: three new `FAIL` lines — `_set() records a state transition`, `_escalate() records a state transition`, `store is in WAL mode` — and a non-zero exit.

- [ ] **Step 3: Add WAL to the store**

In `runner/state.py`, inside `Store.__init__`, after `self.conn.row_factory = sqlite3.Row` and before `executescript`:

```python
        # WAL lets the read-only dashboard query this file while a tick is
        # writing. Without it a reader hits SQLITE_BUSY against the writer's
        # lock. Persistent once set; harmless for the single-writer cron path.
        self.conn.execute("PRAGMA journal_mode=WAL")
```

- [ ] **Step 4: Record transitions in both write paths**

In `runner/pipeline.py`, `_set()`:

```python
    def _set(self, rid: str, state: AIState) -> None:
        row = self.store.get(rid)
        if row:
            graph.check(AIState(row["state"]), state)
            # The ledger otherwise holds agent runs only, so there is no
            # history of when a row reached pr-open or done. Throughput and
            # cycle time are reconstructed from these rows.
            self.store.record("state", rid, f"{row['state']}->{state.value}")
        self.store.set_state(rid, state)
        self._push_remote_state(rid)
```

In `_escalate()`, which bypasses `_set()`:

```python
    def _escalate(self, rid: str, reason: str) -> None:
        reason = self._clean(reason)
        row = self.store.get(rid)
        if row:
            self.store.record("state", rid,
                              f"{row['state']}->{AIState.BLOCKED.value}")
        self.store.set_state(rid, AIState.BLOCKED, last_error=reason[:1000])
        pid = self._pid(rid)
        self._push_remote_state(rid)
        self.provider.add_comment(
            pid, f"{self._repo_tag(rid)}**Escalated to a human.**\n\n{reason}")
```

- [ ] **Step 5: Ignore the WAL sidecar files**

In `.gitignore`, under the existing `*.db-journal` line:

```
*.db-wal
*.db-shm
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 tests_smoke.py`
Expected: all cases PASS, exit 0. The three new checks pass and no existing case regresses.

- [ ] **Step 7: Commit**

```bash
git add runner/state.py runner/pipeline.py .gitignore tests_smoke.py
git commit -m "feat: record state transitions in the events ledger

The ledger held agent runs only, so there was no history of when a row
reached pr-open or done -- only its current state. Throughput over time
could not be reconstructed.

_escalate() writes state directly rather than through _set(), so it gets
its own line; without it every escalation would be invisible.

WAL lets a read-only reader query the store while a tick is writing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: `runner/web/db.py` — the read layer

**Files:**
- Create: `runner/web/__init__.py`
- Create: `runner/web/db.py`
- Create: `tests_web.py`

**Interfaces:**
- Consumes: `events` rows with `kind == "state"` from Task 1.
- Produces: `Reader(db_path, *, secrets=(), limits=None)` with methods `overview(now) -> dict`, `tasks(*, source=None, state=None, include_terminal=False) -> list[dict]`, `task(row_id) -> dict | None`, `stuck(now_epoch) -> list[dict]`, `spend_by_day(days) -> list[dict]`, `throughput_by_day(days) -> list[dict]`, `cycle_times(days) -> list[dict]`, `review_cycles(days) -> list[dict]`, `cost_by_task(limit=10) -> list[dict]`, `close()`. Task 5 (`api.py`) consumes all of these — `cost_by_task` feeds `/api/spend` and `review_cycles` feeds `/api/throughput`.

- [ ] **Step 1: Write the failing tests**

Create `tests_web.py`:

```python
"""Offline tests for the dashboard read layer. No HTTP, no network.

Plain PASS/FAIL script in the style of tests_smoke.py -- not pytest.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from runner.state import Store
from runner.web.db import Reader

results: list[bool] = []


def check(label: str, ok: bool) -> None:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(bool(ok))


def seed(tmp: str) -> tuple[str, Store]:
    """A store with one healthy row and one of each stuck condition."""
    path = f"{tmp}/s.db"
    st = Store(path)
    now = int(time.time())
    st.upsert("acme:1#api", source="acme", provider_task_id="1",
              task_key="DEV-1", title="healthy", state="in-progress",
              repo_key="api", attempts=0, lease_until=now + 600)
    st.upsert("acme:2#api", source="acme", provider_task_id="2",
              task_key="DEV-2", title="waiting", state="needs-input",
              repo_key="api", attempts=0)
    st.upsert("acme:3#api", source="acme", provider_task_id="3",
              task_key="DEV-3", title="dead lease", state="in-progress",
              repo_key="api", attempts=1, lease_until=now - 60)
    st.upsert("acme:4#api", source="acme", provider_task_id="4",
              task_key="DEV-4", title="exhausted", state="queued",
              repo_key="api", attempts=3)
    st.upsert("acme:5#api", source="acme", provider_task_id="5",
              task_key="DEV-5", title="errored", state="queued",
              repo_key="api", attempts=0,
              last_error="push failed: token ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    return path, st


print("dashboard read layer")

with tempfile.TemporaryDirectory() as tmp:
    path, st = seed(tmp)
    rdr = Reader(path, secrets=[], limits={"max_fix_attempts": 3,
                                           "max_active_tasks": 3,
                                           "daily_budget_usd": 15,
                                           "daily_tokens": 0})
    now = int(time.time())
    stuck = {r["task_key"]: r["reason"] for r in rdr.stuck(now)}

    check("healthy row is not stuck", "DEV-1" not in stuck)
    check("needs-input is stuck", "waiting on a human" in stuck.get("DEV-2", ""))
    check("expired lease is stuck", "lease expired" in stuck.get("DEV-3", ""))
    check("exhausted attempts is stuck", "attempts" in stuck.get("DEV-4", ""))
    check("last_error is stuck", "error" in stuck.get("DEV-5", ""))

with tempfile.TemporaryDirectory() as tmp:
    path, st = seed(tmp)
    rdr = Reader(path, secrets=["ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
                 limits={"max_fix_attempts": 3})
    row = rdr.task("acme:5#api")
    check("last_error is redacted on egress",
          row is not None and "ghp_" not in (row["last_error"] or ""))
    check("row id containing '#' round-trips", row is not None
          and row["task_id"] == "acme:5#api")

with tempfile.TemporaryDirectory() as tmp:
    path, st = seed(tmp)
    rdr = Reader(path, limits={})
    wrote = True
    try:
        rdr.conn.execute("UPDATE tasks SET title = 'x' WHERE task_id = 'acme:1#api'")
        rdr.conn.commit()
    except sqlite3.OperationalError:
        wrote = False
    check("the reader's connection cannot write", wrote is False)

with tempfile.TemporaryDirectory() as tmp:
    path, st = seed(tmp)
    # 23:30 local yesterday and 00:30 local today must land in different
    # buckets; UTC bucketing (ts/86400) would merge or split them wrongly.
    today = dt.datetime.now().replace(hour=0, minute=30, second=0, microsecond=0)
    yesterday = today - dt.timedelta(hours=1)
    for when, tok in ((yesterday, 100), (today, 200)):
        st.conn.execute(
            "INSERT INTO events (ts, task_id, kind, detail, cost_usd, tokens)"
            " VALUES (?,?,?,?,?,?)",
            (int(when.timestamp()), "acme:1#api", "implement", "ok", 0.5, tok))
    st.conn.commit()
    rdr = Reader(path, limits={})
    days = {d["day"]: d for d in rdr.spend_by_day(7)}
    check("spend buckets on local midnight, not UTC", len(days) == 2)
    check("spend sums tokens per day",
          sum(d["tokens"] for d in days.values()) == 300)

with tempfile.TemporaryDirectory() as tmp:
    path, st = seed(tmp)
    base = int(dt.datetime.now().replace(hour=9, minute=0, second=0,
                                         microsecond=0).timestamp())
    for off, detail in ((0, "new->queued"), (60, "queued->in-progress"),
                        (120, "in-progress->pr-open"), (180, "pr-open->done")):
        st.conn.execute(
            "INSERT INTO events (ts, task_id, kind, detail) VALUES (?,?,?,?)",
            (base + off, "acme:1#api", "state", detail))
    st.conn.commit()
    rdr = Reader(path, limits={})
    tp = rdr.throughput_by_day(7)
    check("throughput counts transitions into done",
          sum(d["done"] for d in tp) == 1)
    check("throughput counts transitions into pr-open",
          sum(d["pr_open"] for d in tp) == 1)
    cycles = rdr.cycle_times(7)
    check("cycle time measured from first event to done",
          len(cycles) == 1 and cycles[0]["seconds"] == 180)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests_web.py`
Expected: `ModuleNotFoundError: No module named 'runner.web'`.

- [ ] **Step 3: Create the package marker**

Create `runner/web/__init__.py`:

```python
"""Read-only HTTP view over the runner's local state.

Nothing here writes. `db.Reader` opens SQLite with `mode=ro` so that is true
at the storage layer rather than by convention, and every string that can
carry subprocess output is passed through `redact.redact` on the way out --
a web endpoint is an outward path, the same as a ticket comment.

This package must never be imported from `cli.py` at module level: FastAPI is
an optional extra and the cron path must keep starting without it.
"""
```

- [ ] **Step 4: Write `db.py`**

Create `runner/web/db.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 tests_web.py`
Expected: `13/13 passed`, exit 0.

- [ ] **Step 6: Commit**

```bash
git add runner/web/__init__.py runner/web/db.py tests_web.py
git commit -m "feat: read-only query layer for the dashboard

Opens SQLite with mode=ro so writes fail at the storage layer rather than
by convention, and scrubs every field that can carry subprocess output.

Day buckets use date(ts,'unixepoch','localtime') to match the local
midnight the runner's daily gates measure from; UTC bucketing would make
the dashboard disagree with the cap it reports on.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: Per-repo agent log filenames (pre-existing defect)

**Found while designing the log viewer, not introduced by it.** Agent logs are named from the ticket key alone — `f"{task.key}-implement.log"` ([pipeline.py:514](runner/pipeline.py:514)) — and `_EventLog.__init__` opens with `path.open("w")`, which **truncates**. A ticket routed to two repos produces two rows that both write `DEV-123-implement.log`, so the second repo's run destroys the first repo's evidence. The dashboard would then show repo B's log under repo A's tab.

This is a correctness fix the log viewer depends on. If you would rather not touch `pipeline.py` further, cut this task — the cost is that cross-repo tickets show one shared, last-writer-wins log.

**Files:**
- Modify: `runner/models.py` (add `agent_log_name`)
- Modify: `runner/pipeline.py:452,514,564,709`
- Test: `tests_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `models.agent_log_name(task_key: str, repo_key: str, station: str) -> str`, returning `"{task_key}__{repo_key}__{station}.log"`. Task 4's `logs.py` imports it so the writer and the reader cannot disagree.

- [ ] **Step 1: Write the failing test**

Append to `tests_smoke.py`, before the final `for label, ok in _c_checks:` loop:

```python
# --- agent logs are per repo, not per ticket -------------------------------
# _EventLog opens with "w", so two rows of a cross-repo ticket writing the
# same filename means the second run truncates the first run's evidence.
from runner.models import agent_log_name as _aln

_c_checks.append(("log name includes the repo key",
                  _aln("DEV-1", "api", "implement")
                  == "DEV-1__api__implement.log"))
_c_checks.append(("two repos of one ticket get different log names",
                  _aln("DEV-1", "api", "implement")
                  != _aln("DEV-1", "web", "implement")))

import inspect as _inspect
_src = _inspect.getsource(Pipeline)
_c_checks.append(("pipeline builds log paths through agent_log_name",
                  "agent_log_name(" in _src
                  and "-implement.log" not in _src
                  and "-plan.log" not in _src
                  and "-review.log" not in _src))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tests_smoke.py`
Expected: `ImportError: cannot import name 'agent_log_name' from 'runner.models'`.

- [ ] **Step 3: Add the naming function**

Append to `runner/models.py`:

```python
def agent_log_name(task_key: str, repo_key: str, station: str) -> str:
    """Filename for one station's JSONL run log.

    The repo key is part of the name because a ticket routed to two repos has
    two independent rows, and `_EventLog` opens its file with "w". Without the
    repo key the second repo's run truncates the first repo's evidence.

    `__` separates the fields, matching `brain.py`'s `{source}__{repo}`, and
    because `-` appears inside both ticket keys (DEV-1234) and repo keys
    (pulse-backend) and so cannot be parsed back out.
    """
    safe = repo_key.replace("/", "_")
    return f"{task_key}__{safe}__{station}.log"
```

- [ ] **Step 4: Route all four call sites through it**

In `runner/pipeline.py`, add to the imports from `.models`:

```python
from .models import agent_log_name
```

Then replace each `log_path=` expression:

- Line ~452 (analyst): `log_path=self.log_root / agent_log_name(task.key, repo["key"], "plan"),`
- Line ~514 (implementer): `log_path=self.log_root / agent_log_name(task.key, repo["key"], "implement"))`
- Line ~564 (reviewer): `log_path=self.log_root / agent_log_name(task.key, repo["key"], "review"),`
- Line ~709 (CI fix): `log_path=self.log_root / agent_log_name(row["task_key"], repo["key"], f"fix-{attempts}"))`

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 tests_smoke.py`
Expected: all PASS, exit 0.

- [ ] **Step 6: Commit**

```bash
git add runner/models.py runner/pipeline.py tests_smoke.py
git commit -m "fix: name agent logs per repo, not per ticket

A ticket routed to two repos produced two rows that both wrote
DEV-123-implement.log, and _EventLog opens its file with 'w' -- so the
second repo's run truncated the first repo's evidence.

The naming lives in models.agent_log_name so the writer and the
dashboard's reader cannot drift apart.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: `runner/web/logs.py` — JSONL discovery and tail

**Files:**
- Create: `runner/web/logs.py`
- Test: `tests_web.py` (append)

**Interfaces:**
- Consumes: `models.agent_log_name` from Task 3.
- Produces: `stations(log_root: Path, task_key: str, repo_key: str) -> list[str]` and `tail(log_root, task_key, repo_key, station, offset: int = 0, *, secrets=(), max_bytes=262144) -> dict` returning `{"lines": list[dict], "next_offset": int, "eof": bool, "missing": bool}`. Task 5 serializes both.

- [ ] **Step 1: Write the failing tests**

Append to `tests_web.py`, before the final `print(f"\n{sum(results)}...")`:

```python
# --- agent log discovery and tail ------------------------------------------
import json as _json
from runner.web import logs as weblogs

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "DEV-1__api__implement.log").write_text(
        _json.dumps({"seq": 1, "type": "output", "stream": "stdout",
                     "text": "using token ghp_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"}) + "\n"
        + _json.dumps({"seq": 2, "type": "result", "num_turns": 3}) + "\n",
        encoding="utf-8")
    (root / "DEV-1__api__review.log").write_text(
        _json.dumps({"seq": 1, "type": "result"}) + "\n", encoding="utf-8")
    (root / "DEV-1__web__implement.log").write_text(
        _json.dumps({"seq": 1, "type": "result"}) + "\n", encoding="utf-8")

    found = weblogs.stations(root, "DEV-1", "api")
    check("stations lists only this repo's logs",
          sorted(found) == ["implement", "review"])

    got = weblogs.tail(root, "DEV-1", "api", "implement", 0,
                       secrets=["ghp_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"])
    check("tail returns parsed JSONL records", len(got["lines"]) == 2)
    check("tail redacts secrets in log text",
          "ghp_" not in _json.dumps(got["lines"]))
    check("tail reports eof at the end of the file", got["eof"] is True)

    more = weblogs.tail(root, "DEV-1", "api", "implement", got["next_offset"])
    check("tail from the returned offset yields nothing new",
          more["lines"] == [] and more["eof"] is True)

    check("a missing log is reported, not raised",
          weblogs.tail(root, "DEV-9", "api", "implement", 0)["missing"] is True)

    # A run written before Task 3 keeps its old name; an in-flight row should
    # still show something rather than an empty tab.
    (root / "DEV-2-implement.log").write_text(
        _json.dumps({"seq": 1, "type": "result"}) + "\n", encoding="utf-8")
    check("legacy per-ticket log names still resolve",
          weblogs.tail(root, "DEV-2", "api", "implement", 0)["missing"] is False)

    # A path traversal attempt must not escape the log root.
    check("station names cannot escape the log root",
          weblogs.tail(root, "DEV-1", "api", "../../etc/passwd", 0)["missing"] is True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests_web.py`
Expected: `ModuleNotFoundError: No module named 'runner.web.logs'`.

- [ ] **Step 3: Write `logs.py`**

Create `runner/web/logs.py`:

```python
"""Read the per-run JSONL agent logs under `runner.log_dir`.

`_EventLog` writes and flushes per event, so a run in progress is readable
while it runs. Reads are by byte offset: the UI polls with the offset the
previous poll returned and gets only what was appended since.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from ..models import agent_log_name
from ..redact import redact

# A station is a fixed vocabulary plus numbered fix passes. Anchored, because
# this value reaches a filesystem path and the alternative is path traversal.
_STATION = re.compile(r"^(plan|implement|review|fix-\d+)$")

DEFAULT_MAX_BYTES = 262144  # one poll's worth; the UI pages with next_offset


def _candidates(log_root: Path, task_key: str, repo_key: str,
                station: str) -> list[Path]:
    """Current name first, then the pre-per-repo name.

    A row that was mid-flight when per-repo names shipped still has its log
    under the old name; showing it beats an empty tab.
    """
    return [log_root / agent_log_name(task_key, repo_key, station),
            log_root / f"{task_key}-{station}.log"]


def stations(log_root: Path, task_key: str, repo_key: str) -> list[str]:
    """Which station logs exist for this row."""
    out: list[str] = []
    prefix = agent_log_name(task_key, repo_key, "")[:-len(".log")]
    for path in sorted(log_root.glob(f"{prefix}*.log")):
        name = path.name[len(prefix):-len(".log")]
        if _STATION.match(name):
            out.append(name)
    for station in ("plan", "implement", "review"):
        if station not in out and (log_root / f"{task_key}-{station}.log").exists():
            out.append(station)
    return out


def tail(log_root: Path, task_key: str, repo_key: str, station: str,
         offset: int = 0, *, secrets: Sequence[str] = (),
         max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Records appended after `offset`, redacted.

    Returns `next_offset` so the caller polls forward rather than re-reading,
    and `missing` rather than raising, because a station that has not run yet
    is the normal case, not an error.
    """
    if not _STATION.match(station):
        return {"lines": [], "next_offset": offset, "eof": True,
                "missing": True}
    path = next((p for p in _candidates(log_root, task_key, repo_key, station)
                 if p.exists()), None)
    if path is None:
        return {"lines": [], "next_offset": offset, "eof": True,
                "missing": True}

    size = path.stat().st_size
    start = min(max(int(offset), 0), size)
    with path.open("rb") as fh:
        fh.seek(start)
        chunk = fh.read(max_bytes)

    # Stop at the last newline: a partial record at the end of the chunk is a
    # record the writer has not finished, and re-reading it next poll is
    # cheaper than guessing at it.
    cut = chunk.rfind(b"\n")
    if cut == -1:
        return {"lines": [], "next_offset": start, "eof": start >= size,
                "missing": False}
    consumed = cut + 1
    text = redact(chunk[:consumed].decode("utf-8", "replace"), secrets)

    lines: list[dict[str, Any]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            lines.append(json.loads(raw))
        except ValueError:
            # Redaction can straddle a JSON escape, and a killed writer can
            # leave a half-record. Surface it rather than dropping it.
            lines.append({"type": "unparsed", "text": raw})

    next_offset = start + consumed
    return {"lines": lines, "next_offset": next_offset,
            "eof": next_offset >= size, "missing": False}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 tests_web.py`
Expected: `21/21 passed`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add runner/web/logs.py tests_web.py
git commit -m "feat: byte-offset reader for agent JSONL logs

Polls forward from the offset the previous read returned, stops at the
last complete record so a half-written line is re-read rather than
guessed at, and reports a missing log instead of raising -- a station
that has not run yet is the normal case.

Station names are matched against an anchored pattern before they reach
a path.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: `api.py`, the `serve` command, and packaging

**Files:**
- Create: `runner/web/api.py`
- Modify: `runner/cli.py:188-203` (subcommand), plus a `_serve` handler
- Modify: `pyproject.toml`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `Reader` (Task 2), `logs.stations`/`logs.tail` (Task 4).
- Produces: `build_app(cfg) -> fastapi.FastAPI`, and the HTTP contract the frontend consumes — the eight `GET /api/*` endpoints in the spec.

- [ ] **Step 1: Add the optional extra and package data**

In `pyproject.toml`, after `[project.scripts]`:

```toml
[project.optional-dependencies]
web = ["fastapi>=0.110", "uvicorn[standard]>=0.29"]

[tool.setuptools]
# Explicit, not flat-layout auto-discovery: the new top-level `web/` source
# directory must not be mistaken for a package.
packages = { find = { include = ["runner*"] } }

[tool.setuptools.package-data]
# Auto-discovery finds Python packages and carries no other files, so without
# this the built dashboard is dropped from the wheel and `serve` 404s every
# asset while importing cleanly.
"runner.web" = ["static/**/*"]
```

In `.gitignore`, add:

```
web/node_modules/
```

- [ ] **Step 2: Write `api.py`**

Create `runner/web/api.py`:

```python
"""FastAPI routes. Thin by design: parse, call db/logs, serialize.

Everything interesting is in `db.py` and `logs.py`, which are HTTP-free and
tested offline. If a question about correctness leads here, it is in the wrong
module.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import graph
from ..config import Config
from . import logs as weblogs
from .db import Reader

STATIC = Path(__file__).parent / "static"


def _secrets(cfg: Config) -> list[str]:
    """Exact token values to scrub, gathered as Pipeline._known_secrets does.

    Read by env var *name*, so a custom `token_env: GH_TOKEN_ACME` is caught
    the same as the default.
    """
    import os
    out: list[str] = []
    for profile in (cfg.get("vcs_profiles", {}) or {}).values():
        name = (profile or {}).get("token_env")
        if name and os.environ.get(name):
            out.append(os.environ[name])
    return out


def build_app(cfg: Config) -> FastAPI:
    db_path = cfg.get("runner.db_path", "~/.ai-runner/state.db")
    log_root = Path(str(cfg.get("runner.log_dir", "~/.ai-runner/logs"))).expanduser()
    limits = cfg.get("runner.limits", {}) or {}
    secrets = _secrets(cfg)

    app = FastAPI(title="ai-task-runner", docs_url=None, redoc_url=None)

    def reader() -> Reader:
        # One connection per request: sqlite3 objects are not safely shared
        # across threads, and a read-only open is cheap.
        return Reader(db_path, secrets=secrets, limits=limits)

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        r = reader()
        try:
            out = r.overview(dt.datetime.now())
            out["runner_id"] = cfg.get("runner.id", "runner")
            out["mode"] = cfg.get("runner.mode", "shadow")
            out["windows"] = cfg.get("runner.windows", {}) or {}
            return out
        finally:
            r.close()

    @app.get("/api/tasks")
    def tasks(source: str | None = None, state: str | None = None,
              include_terminal: bool = False) -> dict[str, Any]:
        r = reader()
        try:
            return {"tasks": r.tasks(source=source, state=state,
                                     include_terminal=include_terminal),
                    "stuck": r.stuck(int(dt.datetime.now().timestamp()))}
        finally:
            r.close()

    @app.get("/api/task")
    def task(id: str = Query(...)) -> dict[str, Any]:
        r = reader()
        try:
            row = r.task(id)
            if row is None:
                raise HTTPException(404, "no such row")
            row["stations"] = weblogs.stations(
                log_root, row["task_key"] or "", row["repo_key"] or "")
            return row
        finally:
            r.close()

    @app.get("/api/task/log")
    def task_log(id: str = Query(...), station: str = Query(...),
                 offset: int = 0) -> dict[str, Any]:
        r = reader()
        try:
            row = r.task(id)
            if row is None:
                raise HTTPException(404, "no such row")
            return weblogs.tail(log_root, row["task_key"] or "",
                                row["repo_key"] or "", station, offset,
                                secrets=secrets)
        finally:
            r.close()

    @app.get("/api/spend")
    def spend(days: int = 30) -> dict[str, Any]:
        r = reader()
        try:
            return {"days": r.spend_by_day(days),
                    "by_task": r.cost_by_task(10)}
        finally:
            r.close()

    @app.get("/api/throughput")
    def throughput(days: int = 30) -> dict[str, Any]:
        r = reader()
        try:
            return {"days": r.throughput_by_day(days),
                    "cycle_times": r.cycle_times(days),
                    "review_cycles": r.review_cycles(days)}
        finally:
            r.close()

    @app.get("/api/graph")
    def state_graph() -> dict[str, str]:
        return {"mermaid": graph.to_mermaid()}

    @app.get("/api/config")
    def config_summary() -> dict[str, Any]:
        # Hand-built. Never serialize the Config object: it holds expanded
        # ${VAR} secrets.
        return {
            "runner_id": cfg.get("runner.id", "runner"),
            "mode": cfg.get("runner.mode", "shadow"),
            "days": cfg.get("runner.days", []),
            "windows": cfg.get("runner.windows", {}) or {},
            "limits": cfg.get("runner.limits", {}) or {},
            "sources": [{"key": s.get("key"),
                         "repos": [r.get("key") for r in (s.get("repos") or [])]}
                        for s in (cfg.get("sources", []) or [])],
        }

    if STATIC.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"),
                  name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str) -> FileResponse:
            # Single-page app: every non-API path serves index.html so client
            # routing works on a hard refresh.
            index = STATIC / "index.html"
            if not index.exists():
                raise HTTPException(
                    500, "dashboard assets are missing; run `npm run build` "
                         "in web/")
            return FileResponse(index)

    return app
```

- [ ] **Step 3: Add the `serve` subcommand**

In `runner/cli.py`, inside `main()`, after the `graph` subparser line:

```python
    serve = sub.add_parser("serve", help="read-only dashboard on localhost")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--no-open", action="store_true")
```

And after the `status` handler block, before the `args.force` block:

```python
    if args.cmd == "serve":
        return _serve(cfg, args.host, args.port, not args.no_open)
```

Add the handler near `_doctor`:

```python
def _serve(cfg: Config, host: str, port: int, open_browser: bool) -> int:
    """Run the read-only dashboard.

    Imports are local on purpose: FastAPI is an optional extra and `run` --
    the cron entrypoint -- must keep starting without it.
    """
    try:
        import uvicorn
        from runner.web.api import build_app
    except ImportError:
        print("the dashboard needs its optional dependencies:\n"
              "    pip install -e '.[web]'", file=sys.stderr)
        return 1

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {host} exposes ticket titles, PR URLs and "
              f"error text to anything that can reach this machine.",
              file=sys.stderr)
    url = f"http://{host}:{port}/"
    print(f"dashboard on {url}  (read-only; ctrl-c to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    uvicorn.run(build_app(cfg), host=host, port=port, log_level="warning")
    return 0
```

- [ ] **Step 4: Verify the cron path did not gain a dependency**

Run: `python3 -c "import runner.cli; import sys; assert 'fastapi' not in sys.modules, 'cli imports fastapi eagerly'; print('cron path clean')"`
Expected: `cron path clean`.

- [ ] **Step 5: Verify the API answers**

```bash
pip install -e '.[web]'
ai-task-runner serve --no-open &
sleep 2
curl -s localhost:8787/api/overview | head -c 400; echo
curl -s localhost:8787/api/tasks | head -c 400; echo
curl -s 'localhost:8787/api/task?id=acme%3A1%23api' | head -c 200; echo
kill %1
```

Expected: JSON from each. The third confirms a `#` row id survives URL encoding — it returns either the row or a 404 body, never a 422 or a row id truncated at the `#`.

- [ ] **Step 6: Commit**

```bash
git add runner/web/api.py runner/cli.py pyproject.toml .gitignore
git commit -m "feat: read-only dashboard API and serve command

FastAPI and uvicorn are an optional extra and cli.py imports runner.web
lazily, so the cron path keeps starting on requests + pyyaml alone.

Row ids travel as query parameters: they contain '#', which a browser
strips from a path before the request is sent.

package-data is explicit because flat-layout discovery carries no
non-Python files, and the built dashboard would otherwise be missing from
the wheel while importing cleanly.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: Frontend scaffold, validated palette, and API client

**The palette is already validated — do not re-choose it.** Both modes were run through `scripts/validate_palette.js` while this plan was written:

```
light  (surface #fcfcfb): #2a78d6,#eb6834,#1baf7a,#eda100,#e87ba4
  PASS lightness band · PASS chroma floor
  PASS CVD separation  worst adjacent #eda100<->#1baf7a dE 9.1 (protan)
  PASS normal-vision floor  worst adjacent dE 19.6
  WARN contrast vs surface  #1baf7a 2.74, #eda100 2.11, #e87ba4 2.62

dark   (surface #1a1a19): #3987e5,#d95926,#199e70,#c98500,#d55181
  ALL CHECKS PASS, all 5 >= 3:1
```

**The light-mode contrast WARN is not dismissable.** It obligates relief: every chart ships a **table view toggle** (Task 8). That is a requirement, not a refinement.

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`, `web/index.html`, `web/src/main.tsx`, `web/src/App.tsx`, `web/src/index.css`, `web/src/lib/api.ts`, `web/src/lib/palette.ts`, `web/components.json`
- Create (generated): `runner/web/static/**`

**Interfaces:**
- Consumes: the eight `GET /api/*` endpoints from Task 5.
- Produces: `useOverview()`, `useTasks()`, `useTask(id)`, `useTaskLog(id, station, offset)`, `useSpend(days)`, `useThroughput(days)`, `useGraph()` from `lib/api.ts`; `SERIES`, `STATUS`, `STATIONS`, `stationColor()`, `STATE_STATUS`, `SURFACE`, `GRID`, `MUTED` from `lib/palette.ts`. Tasks 7-9 import only these.

- [ ] **Step 1: Scaffold the project**

```bash
cd web
npm create vite@latest . -- --template react-ts
npm install
npm install @tanstack/react-query @tanstack/react-table recharts mermaid
npm install -D tailwindcss @tailwindcss/vite
npx shadcn@latest init
npx shadcn@latest add card badge table tabs sheet button separator scroll-area tooltip
```

- [ ] **Step 2: Point the build at the Python package**

`web/vite.config.ts`:

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: {
    // NOT web/dist: .gitignore has an unanchored `dist/`, so a conventional
    // output directory would be silently uncommitted and every fresh clone
    // would get an assetless dashboard. This path is committed on purpose.
    outDir: path.resolve(__dirname, "../runner/web/static"),
    emptyOutDir: true,
  },
  server: { proxy: { "/api": "http://127.0.0.1:8787" } },
});
```

- [ ] **Step 3: Write the validated palette module**

`web/src/lib/palette.ts`:

```ts
// Validated with dataviz scripts/validate_palette.js. Light mode carries a
// contrast WARN on slots 3-5, which obligates the table-view toggle on every
// chart. Do not substitute hues without re-running the validator.
export const SERIES = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
} as const;

// Reserved. Never reused as a series color -- a status hue must not
// impersonate a station, and `fix` must never render in the red that means
// `blocked`.
export const STATUS = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
} as const;

// Station kinds, in fixed slot order. Never cycled: a sixth kind would take a
// named slot or fold into "Other", never a generated hue.
export const STATIONS = ["triage", "plan", "implement", "review", "fix"] as const;
export type Station = (typeof STATIONS)[number];

export function stationColor(s: Station, mode: "light" | "dark"): string {
  return SERIES[mode][STATIONS.indexOf(s)];
}

// Every state carries an icon and a word alongside this color -- color alone
// never conveys state.
export const STATE_STATUS: Record<string, keyof typeof STATUS | null> = {
  "done": "good",
  "pr-open": "good",
  "blocked": "critical",
  "human-only": "critical",
  "needs-input": "warning",
  "plan-review": "warning",
  "in-progress": null,
  "review": null,
  "planning": null,
  "queued": null,
  "new": null,
};

export const SURFACE = { light: "#fcfcfb", dark: "#1a1a19" } as const;
export const GRID = { light: "#e1e0d9", dark: "#2c2c2a" } as const;
export const MUTED = "#898781";
```

- [ ] **Step 4: Write the API client**

`web/src/lib/api.ts`:

```ts
import { useQuery } from "@tanstack/react-query";

export type TaskRow = {
  task_id: string; source: string; task_key: string; title: string | null;
  state: string; repo_key: string | null; branch: string | null;
  pr_number: number | null; pr_url: string | null; attempts: number;
  cost_usd: number; tokens: number; last_error: string | null;
  lease_owner: string | null; lease_until: number; updated_at: number;
  reason?: string; stations?: string[];
  events?: { ts: number; kind: string; detail: string; cost_usd: number; tokens: number }[];
};

export type Overview = {
  counts: Record<string, number>; active: number; max_active_tasks: number;
  today: { cost_usd: number; tokens: number };
  daily_budget_usd: number; daily_tokens: number;
  stuck: number; last_event_ts: number;
  runner_id: string; mode: string;
  windows: Record<string, string[]>;
};

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

// Row ids contain '#'. encodeURIComponent is not optional: an unencoded id is
// truncated at the '#' by the browser before the request leaves, and every
// routed row collapses onto its placeholder.
const qid = (id: string) => encodeURIComponent(id);

export const useOverview = () =>
  useQuery({ queryKey: ["overview"], queryFn: () => get<Overview>("/api/overview"),
             refetchInterval: 5000 });

export const useTasks = () =>
  useQuery({ queryKey: ["tasks"],
             queryFn: () => get<{ tasks: TaskRow[]; stuck: TaskRow[] }>("/api/tasks"),
             refetchInterval: 5000 });

export const useTask = (id: string | null) =>
  useQuery({ queryKey: ["task", id], enabled: !!id,
             queryFn: () => get<TaskRow>(`/api/task?id=${qid(id!)}`),
             refetchInterval: 5000 });

export const useTaskLog = (id: string | null, station: string | null, offset: number) =>
  useQuery({
    queryKey: ["log", id, station, offset], enabled: !!id && !!station,
    queryFn: () => get<{ lines: any[]; next_offset: number; eof: boolean; missing: boolean }>(
      `/api/task/log?id=${qid(id!)}&station=${encodeURIComponent(station!)}&offset=${offset}`),
    refetchInterval: 2000,
  });

export const useSpend = (days = 30) =>
  useQuery({ queryKey: ["spend", days], queryFn: () => get<any>(`/api/spend?days=${days}`),
             refetchInterval: 15000 });

export const useThroughput = (days = 30) =>
  useQuery({ queryKey: ["throughput", days],
             queryFn: () => get<any>(`/api/throughput?days=${days}`),
             refetchInterval: 15000 });

export const useGraph = () =>
  useQuery({ queryKey: ["graph"], queryFn: () => get<{ mermaid: string }>("/api/graph") });
```

- [ ] **Step 5: Wire the app shell**

`web/src/App.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

const qc = new QueryClient();

export default function App() {
  const [tab, setTab] = useState("overview");
  return (
    <QueryClientProvider client={qc}>
      <div className="min-h-screen bg-background text-foreground p-4 md:p-6">
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="board">Board</TabsTrigger>
            <TabsTrigger value="spend">Spend</TabsTrigger>
            <TabsTrigger value="throughput">Throughput</TabsTrigger>
            <TabsTrigger value="graph">Graph</TabsTrigger>
          </TabsList>
          <TabsContent value="overview">{/* Task 7 */}</TabsContent>
          <TabsContent value="board">{/* Task 7 */}</TabsContent>
          <TabsContent value="spend">{/* Task 8 */}</TabsContent>
          <TabsContent value="throughput">{/* Task 8 */}</TabsContent>
          <TabsContent value="graph">{/* Task 9 */}</TabsContent>
        </Tabs>
      </div>
    </QueryClientProvider>
  );
}
```

- [ ] **Step 6: Build and verify the assets land in the Python package**

```bash
cd web && npm run build
ls ../runner/web/static/index.html ../runner/web/static/assets/ | head
```
Expected: `index.html` and an `assets/` directory exist under `runner/web/static/`, and `git status` shows them as **untracked additions**, not ignored. If they are ignored, the Vite `outDir` is wrong.

- [ ] **Step 7: Commit**

```bash
git add web runner/web/static
git commit -m "feat: dashboard frontend scaffold with a validated palette

Both palettes were run through the dataviz validator. Light mode carries
a contrast WARN on three slots, so every chart owes a table view.

Vite builds into runner/web/static, not web/dist: .gitignore has an
unanchored dist/ and the build output would have been silently
uncommitted.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: Overview and Board

**Files:**
- Create: `web/src/views/Overview.tsx`, `web/src/views/Board.tsx`, `web/src/components/StateBadge.tsx`, `web/src/components/StatTile.tsx`
- Modify: `web/src/App.tsx` (fill the two TabsContent slots)

**Interfaces:**
- Consumes: `useOverview`, `useTasks`, `STATE_STATUS`, `STATUS` from Task 6.
- Produces: `<StateBadge state={string} />` and `<StatTile label value sub tone />`, both reused by Tasks 8 and 9.

- [ ] **Step 1: Write the state badge (icon + word, never color alone)**

`web/src/components/StateBadge.tsx`:

```tsx
import { Badge } from "@/components/ui/badge";
import { STATE_STATUS, STATUS } from "@/lib/palette";

const ICON: Record<string, string> = {
  good: "✓", warning: "⚠", serious: "▲", critical: "✕",
};

export function StateBadge({ state }: { state: string }) {
  const tone = STATE_STATUS[state] ?? null;
  // The word is always present. Color is a second channel, never the only one.
  return (
    <Badge variant="outline" className="gap-1 font-mono text-xs"
           style={tone ? { borderColor: STATUS[tone], color: STATUS[tone] } : undefined}>
      {tone && <span aria-hidden>{ICON[tone]}</span>}
      {state}
    </Badge>
  );
}
```

- [ ] **Step 2: Write the stat tile (no plot — a headline number is not a chart)**

`web/src/components/StatTile.tsx`:

```tsx
import { Card, CardContent } from "@/components/ui/card";

export function StatTile({ label, value, sub, tone }: {
  label: string; value: string; sub?: string; tone?: string;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
        <div className="mt-1 text-2xl font-semibold tabular-nums"
             style={tone ? { color: tone } : undefined}>{value}</div>
        {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 3: Write the Overview**

`web/src/views/Overview.tsx`:

```tsx
import { useOverview, useTasks } from "@/lib/api";
import { StatTile } from "@/components/StatTile";
import { StateBadge } from "@/components/StateBadge";
import { STATUS } from "@/lib/palette";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

function ago(ts: number): string {
  if (!ts) return "never";
  const s = Math.floor(Date.now() / 1000) - ts;
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export function Overview({ onOpen }: { onOpen: (id: string) => void }) {
  const ov = useOverview();
  const tasks = useTasks();
  if (!ov.data) return <div className="p-6 text-muted-foreground">loading…</div>;
  const o = ov.data;
  const stuck = tasks.data?.stuck ?? [];

  // A runner that has not ticked during an open window is the failure this
  // dashboard exists to catch, and nothing else on the page would show it.
  const silent = Math.floor(Date.now() / 1000) - o.last_event_ts > 3600;

  return (
    <div className="space-y-4 pt-4">
      <div className="grid gap-3 grid-cols-2 md:grid-cols-5">
        <StatTile label="Stuck" value={String(o.stuck)}
                  tone={o.stuck ? STATUS.critical : STATUS.good}
                  sub={o.stuck ? "needs a human" : "all clear"} />
        <StatTile label="Active" value={`${o.active} / ${o.max_active_tasks}`}
                  sub="slots in use" />
        <StatTile label="Tokens today" value={o.today.tokens.toLocaleString()}
                  sub={o.daily_tokens ? `cap ${o.daily_tokens.toLocaleString()}` : "no cap set"}
                  tone={o.daily_tokens && o.today.tokens >= o.daily_tokens ? STATUS.critical : undefined} />
        <StatTile label="Spend today" value={`$${o.today.cost_usd.toFixed(2)}`}
                  sub={`budget $${o.daily_budget_usd.toFixed(2)}`}
                  tone={o.today.cost_usd >= o.daily_budget_usd ? STATUS.critical : undefined} />
        <StatTile label="Last tick" value={ago(o.last_event_ts)}
                  sub={`${o.runner_id} · ${o.mode}`}
                  tone={silent ? STATUS.warning : undefined} />
      </div>

      <Card>
        <CardHeader className="pb-2"><CardTitle className="text-base">Needs a human</CardTitle></CardHeader>
        <CardContent>
          {stuck.length === 0 ? (
            <div className="py-6 text-sm text-muted-foreground">
              Nothing is stuck. Every in-flight row is progressing on its own.
            </div>
          ) : (
            <div className="divide-y">
              {stuck.map((r) => (
                <button key={r.task_id} onClick={() => onOpen(r.task_id)}
                        className="flex w-full items-start gap-3 py-2 text-left hover:bg-muted/50">
                  <StateBadge state={r.state} />
                  <div className="min-w-0 flex-1">
                    <div className="font-mono text-sm">{r.task_key}
                      <span className="ml-2 text-muted-foreground">{r.repo_key}</span></div>
                    <div className="truncate text-sm">{r.title}</div>
                    <div className="text-xs text-muted-foreground">{r.reason}</div>
                    {r.last_error && (
                      <div className="mt-1 truncate font-mono text-xs text-muted-foreground">
                        {r.last_error}</div>)}
                  </div>
                </button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
```

- [ ] **Step 4: Write the Board**

`web/src/views/Board.tsx`:

```tsx
import { useTasks, type TaskRow } from "@/lib/api";
import { StateBadge } from "@/components/StateBadge";
import { Card, CardContent } from "@/components/ui/card";

// Graph order, so the board reads left to right the way the pipeline runs.
const COLUMNS = ["new", "planning", "plan-review", "queued",
                 "in-progress", "review", "pr-open"] as const;

export function Board({ onOpen }: { onOpen: (id: string) => void }) {
  const { data } = useTasks();
  const rows = data?.tasks ?? [];
  // A repo with an active row is locked: graph.repo_is_free made visible
  // rather than inferred.
  const locked = new Set(rows.filter((r) => r.state === "in-progress")
                             .map((r) => r.repo_key));
  return (
    <div className="grid gap-3 pt-4" style={{ gridTemplateColumns: `repeat(${COLUMNS.length}, minmax(9rem, 1fr))` }}>
      {COLUMNS.map((col) => {
        const inCol = rows.filter((r) => r.state === col);
        return (
          <div key={col} className="space-y-2">
            <div className="flex items-center justify-between text-xs uppercase tracking-wide text-muted-foreground">
              <span>{col}</span><span className="tabular-nums">{inCol.length}</span>
            </div>
            {inCol.map((r: TaskRow) => (
              <Card key={r.task_id} onClick={() => onOpen(r.task_id)}
                    className="cursor-pointer hover:border-foreground/30">
                <CardContent className="space-y-1 p-3">
                  <div className="font-mono text-xs">{r.task_key}</div>
                  <div className="line-clamp-2 text-sm">{r.title}</div>
                  <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
                    <span>{r.repo_key}</span>
                    {locked.has(r.repo_key) && r.state === "in-progress" && <span>· locked</span>}
                    {r.attempts > 0 && <span>· {r.attempts} attempts</span>}
                    {r.tokens > 0 && <span>· {r.tokens.toLocaleString()} tok</span>}
                  </div>
                  {r.pr_url && (
                    <a href={r.pr_url} target="_blank" rel="noreferrer"
                       onClick={(e) => e.stopPropagation()}
                       className="text-xs underline">PR #{r.pr_number}</a>)}
                </CardContent>
              </Card>
            ))}
          </div>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 5: Wire both into `App.tsx`**

Replace the two placeholder `TabsContent` bodies, add the detail-sheet state that Task 9 fills:

```tsx
const [openId, setOpenId] = useState<string | null>(null);
// ...
<TabsContent value="overview"><Overview onOpen={setOpenId} /></TabsContent>
<TabsContent value="board"><Board onOpen={setOpenId} /></TabsContent>
```

- [ ] **Step 6: Verify against real data**

```bash
ai-task-runner serve --no-open &
cd web && npm run dev
```
Open the dev server. Expected: stat tiles show real numbers; the stuck list either lists rows with a `reason` string, or shows the empty-state sentence; the board renders in-flight rows in graph order. Confirm a stuck row's `reason` text is present — a colored badge with no words is the failure this check exists to catch.

- [ ] **Step 7: Build and commit**

```bash
cd web && npm run build && cd ..
git add web runner/web/static
git commit -m "feat: dashboard overview and board

Every state carries an icon and a word beside its color, so identity
never depends on color alone.

The last-tick tile is the point of the overview: a runner that has not
ticked during an open window is the failure nothing else on the page
would reveal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 8: Spend and Throughput charts

**Two rules this task exists to honour.** Tokens and dollars are different scales and go in **two separate charts sharing an x-range** — never one chart with two y-axes, which is the single most common charting error. And the light palette's contrast WARN obligates relief, so **every chart here ships a table-view toggle**.

**Files:**
- Create: `web/src/components/ChartFrame.tsx`, `web/src/views/Spend.tsx`, `web/src/views/Throughput.tsx`
- Modify: `web/src/App.tsx`

**Interfaces:**
- Consumes: `useSpend`, `useThroughput`, `SERIES`, `STATIONS`, `STATUS`, `GRID`, `MUTED` from Task 6.
- Produces: `<ChartFrame title rows columns>{chart}</ChartFrame>`, the table-view wrapper every chart in the app uses.

- [ ] **Step 1: Write the chart frame that carries the table view**

`web/src/components/ChartFrame.tsx`:

```tsx
import { useState, type ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow }
  from "@/components/ui/table";

/** Every chart owes a table view.
 *
 * The validated light palette carries a contrast WARN on three of five slots
 * (2.11-2.74:1 against #fcfcfb). That warning is not dismissable: it obligates
 * relief, and a table is the relief. It is also simply the fastest way to read
 * an exact number off a chart.
 */
export function ChartFrame({ title, columns, rows, children }: {
  title: string;
  columns: string[];
  rows: (string | number)[][];
  children: ReactNode;
}) {
  const [asTable, setAsTable] = useState(false);
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base">{title}</CardTitle>
        <Button variant="ghost" size="sm" onClick={() => setAsTable((v) => !v)}>
          {asTable ? "chart" : "table"}
        </Button>
      </CardHeader>
      <CardContent>
        {asTable ? (
          <Table>
            <TableHeader><TableRow>
              {columns.map((c) => <TableHead key={c}>{c}</TableHead>)}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((r, i) => (
                <TableRow key={i}>
                  {r.map((cell, j) => (
                    <TableCell key={j} className="tabular-nums">{cell}</TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : children}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Write the Spend view**

`web/src/views/Spend.tsx`:

```tsx
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip,
         XAxis, YAxis } from "recharts";
import { useSpend } from "@/lib/api";
import { ChartFrame } from "@/components/ChartFrame";
import { GRID, MUTED, SERIES, STATIONS } from "@/lib/palette";

const mode = () =>
  window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

export function Spend() {
  const { data } = useSpend(30);
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;
  const m = mode();

  // One row per day, one numeric field per station kind. Days with no work are
  // absent rather than zero-filled -- an absent bar reads as "nothing ran",
  // which is the truth.
  const rows = data.days.map((d: any) => {
    const out: any = { day: d.day.slice(5) };
    for (const s of STATIONS) {
      out[`${s}_tok`] = d.kinds?.[s]?.tokens ?? 0;
      out[`${s}_usd`] = d.kinds?.[s]?.cost_usd ?? 0;
    }
    return out;
  });

  const bars = (suffix: "tok" | "usd") =>
    STATIONS.map((s, i) => (
      <Bar key={s} dataKey={`${s}_${suffix}`} name={s} stackId="a"
           fill={SERIES[m][i]}
           // 2px surface gap between stacked segments.
           stroke={m === "dark" ? "#1a1a19" : "#fcfcfb"} strokeWidth={2}
           radius={i === STATIONS.length - 1 ? [4, 4, 0, 0] : 0} />
    ));

  const axis = { stroke: MUTED, fontSize: 11, tickLine: false, axisLine: false };

  return (
    <div className="space-y-4 pt-4">
      {/* Tokens and dollars are different scales. Two charts, one x-range --
          never one chart with two y-axes. */}
      <ChartFrame title="Tokens per day, by station"
        columns={["day", ...STATIONS]}
        rows={rows.map((r: any) => [r.day, ...STATIONS.map((s) => r[`${s}_tok`])])}>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={rows}>
            <CartesianGrid stroke={GRID[m]} vertical={false} />
            <XAxis dataKey="day" {...axis} />
            <YAxis {...axis} width={56} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Legend iconType="square" />
            {bars("tok")}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>

      <ChartFrame title="Spend per day, by station"
        columns={["day", ...STATIONS]}
        rows={rows.map((r: any) => [r.day, ...STATIONS.map((s) => r[`${s}_usd`].toFixed(2))])}>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={rows}>
            <CartesianGrid stroke={GRID[m]} vertical={false} />
            <XAxis dataKey="day" {...axis} />
            <YAxis {...axis} width={56} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Legend iconType="square" />
            {bars("usd")}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>

      {/* A ranking, not a composition: the spec's "Other" band would be
          meaningless here because the bars do not sum to a whole. Top 10 by
          tokens, and the table view carries the exact figures. */}
      <ChartFrame title="Most expensive rows"
        columns={["ticket", "repo", "tokens", "usd"]}
        rows={data.by_task.map((t: any) =>
          [t.task_key, t.repo_key ?? "", t.tokens.toLocaleString(), t.cost_usd.toFixed(2)])}>
        <ResponsiveContainer width="100%" height={Math.max(160, data.by_task.length * 28)}>
          <BarChart data={data.by_task} layout="vertical">
            <CartesianGrid stroke={GRID[m]} horizontal={false} />
            <XAxis type="number" {...axis} />
            <YAxis type="category" dataKey="task_key" {...axis} width={90} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Bar dataKey="tokens" fill={SERIES[m][0]} radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </div>
  );
}
```

- [ ] **Step 3: Write the Throughput view**

`web/src/views/Throughput.tsx`:

```tsx
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip,
         XAxis, YAxis } from "recharts";
import { useThroughput } from "@/lib/api";
import { ChartFrame } from "@/components/ChartFrame";
import { GRID, MUTED, STATUS } from "@/lib/palette";

const mode = () =>
  window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

/** Bucket cycle times into human-sized bands rather than raw seconds. */
function histogram(seconds: number[]): { band: string; n: number }[] {
  const bands = [
    ["< 1h", 3600], ["1-4h", 14400], ["4-12h", 43200],
    ["12-24h", 86400], ["1-3d", 259200], ["> 3d", Infinity],
  ] as const;
  return bands.map(([band, cap], i) => {
    const floor = i === 0 ? 0 : (bands[i - 1][1] as number);
    return { band, n: seconds.filter((s) => s > floor && s <= cap).length };
  });
}

export function Throughput() {
  const { data } = useThroughput(30);
  if (!data) return <div className="p-6 text-muted-foreground">loading…</div>;
  const m = mode();
  const axis = { stroke: MUTED, fontSize: 11, tickLine: false, axisLine: false };
  const days = data.days.map((d: any) => ({ ...d, day: d.day.slice(5) }));
  const hist = histogram(data.cycle_times.map((c: any) => c.seconds));

  const cycles: Record<number, number> = {};
  for (const r of data.review_cycles) cycles[r.cycles] = (cycles[r.cycles] ?? 0) + 1;
  const cycleRows = Object.entries(cycles)
    .map(([k, n]) => ({ cycles: `${k}x`, n })).sort((a, b) => a.cycles.localeCompare(b.cycles));

  return (
    <div className="space-y-4 pt-4">
      {/* These are states, not series, so they wear the reserved status
          palette -- with names in the legend, never color alone. */}
      <ChartFrame title="Outcomes per day"
        columns={["day", "pr-open", "done", "blocked"]}
        rows={days.map((d: any) => [d.day, d.pr_open, d.done, d.blocked])}>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={days}>
            <CartesianGrid stroke={GRID[m]} vertical={false} />
            <XAxis dataKey="day" {...axis} />
            <YAxis {...axis} width={40} allowDecimals={false} />
            <Tooltip cursor={{ fill: "transparent" }} />
            <Legend iconType="square" />
            <Bar dataKey="pr_open" name="pr-open" fill={STATUS.serious}
                 stroke={m === "dark" ? "#1a1a19" : "#fcfcfb"} strokeWidth={2}
                 radius={[4, 4, 0, 0]} />
            <Bar dataKey="done" name="done" fill={STATUS.good}
                 stroke={m === "dark" ? "#1a1a19" : "#fcfcfb"} strokeWidth={2}
                 radius={[4, 4, 0, 0]} />
            <Bar dataKey="blocked" name="blocked" fill={STATUS.critical}
                 stroke={m === "dark" ? "#1a1a19" : "#fcfcfb"} strokeWidth={2}
                 radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>

      <div className="grid gap-4 md:grid-cols-2">
        <ChartFrame title="Time from first touch to done"
          columns={["band", "rows"]} rows={hist.map((h) => [h.band, h.n])}>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={hist}>
              <CartesianGrid stroke={GRID[m]} vertical={false} />
              <XAxis dataKey="band" {...axis} />
              <YAxis {...axis} width={32} allowDecimals={false} />
              <Tooltip cursor={{ fill: "transparent" }} />
              <Bar dataKey="n" fill={STATUS.good} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartFrame>

        <ChartFrame title="Review cycles per row"
          columns={["cycles", "rows"]} rows={cycleRows.map((c) => [c.cycles, c.n])}>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={cycleRows}>
              <CartesianGrid stroke={GRID[m]} vertical={false} />
              <XAxis dataKey="cycles" {...axis} />
              <YAxis {...axis} width={32} allowDecimals={false} />
              <Tooltip cursor={{ fill: "transparent" }} />
              <Bar dataKey="n" fill={STATUS.warning} radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartFrame>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Wire both into `App.tsx`**

```tsx
<TabsContent value="spend"><Spend /></TabsContent>
<TabsContent value="throughput"><Throughput /></TabsContent>
```

- [ ] **Step 5: Render it and look at it**

The validator checks color, not layout. Run the dev server, open both tabs, and confirm by eye:

- No x-axis label collisions at 30 days; if labels overlap, reduce the default range or angle them.
- Every chart's **table toggle works** and shows the same numbers as the bars.
- Legends name every series; no chart has two y-axes.
- Switch the OS to dark mode and reload — bars must sit on the dark surface with a visible 2px gap, not a light-mode palette on a dark card.

- [ ] **Step 6: Build and commit**

```bash
cd web && npm run build && cd ..
git add web runner/web/static
git commit -m "feat: spend and throughput charts

Tokens and dollars are separate charts sharing an x-range, not one chart
with two y-axes.

Every chart carries a table view: the validated light palette warns on
contrast for three of five slots, and that warning obligates relief
rather than being dismissable.

Outcome bars use the reserved status palette because they encode states,
not series.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 9: Task detail sheet, log tail, and graph tab

**Files:**
- Create: `web/src/views/TaskSheet.tsx`, `web/src/views/GraphTab.tsx`
- Modify: `web/src/App.tsx`

**Interfaces:**
- Consumes: `useTask`, `useTaskLog`, `useGraph` from Task 6; `StateBadge` from Task 7.
- Produces: nothing downstream.

- [ ] **Step 1: Write the detail sheet with the log tail**

`web/src/views/TaskSheet.tsx`:

```tsx
import { useEffect, useState } from "react";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { StateBadge } from "@/components/StateBadge";
import { useTask, useTaskLog } from "@/lib/api";

function LogTail({ id, station }: { id: string; station: string }) {
  // Poll forward from the offset the last read returned rather than
  // re-fetching the file. A killed run still left evidence on disk, so a
  // finished station reads the same way a running one does.
  const [offset, setOffset] = useState(0);
  const [lines, setLines] = useState<any[]>([]);
  const { data } = useTaskLog(id, station, offset);

  useEffect(() => { setOffset(0); setLines([]); }, [id, station]);
  useEffect(() => {
    if (!data || data.missing || data.lines.length === 0) return;
    setLines((prev) => [...prev, ...data.lines]);
    setOffset(data.next_offset);
  }, [data]);

  if (data?.missing) {
    return <div className="p-4 text-sm text-muted-foreground">
      This station has not run for this row.</div>;
  }
  return (
    <ScrollArea className="h-[60vh] rounded border">
      <pre className="p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
        {lines.map((l, i) => (
          <div key={i}>
            <span className="text-muted-foreground">{l.type}</span>{" "}
            {l.text ?? JSON.stringify(l)}
          </div>
        ))}
      </pre>
    </ScrollArea>
  );
}

export function TaskSheet({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { data: row } = useTask(id);
  const [station, setStation] = useState<string | null>(null);
  useEffect(() => { setStation(row?.stations?.[0] ?? null); }, [row?.task_id]);

  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full sm:max-w-3xl">
        {row && (
          <>
            <SheetHeader>
              <SheetTitle className="flex items-center gap-2 font-mono text-sm">
                {row.task_key}<StateBadge state={row.state} />
                <span className="text-muted-foreground">{row.repo_key}</span>
              </SheetTitle>
            </SheetHeader>
            <div className="mt-2 text-sm">{row.title}</div>
            <div className="mt-1 flex flex-wrap gap-3 text-xs text-muted-foreground">
              {row.branch && <span className="font-mono">{row.branch}</span>}
              {row.pr_url && <a className="underline" href={row.pr_url}
                                target="_blank" rel="noreferrer">PR #{row.pr_number}</a>}
              <span>{row.attempts} attempts</span>
              <span>{row.tokens.toLocaleString()} tok · ${row.cost_usd.toFixed(2)}</span>
            </div>

            <Tabs defaultValue="timeline" className="mt-4">
              <TabsList>
                <TabsTrigger value="timeline">Timeline</TabsTrigger>
                <TabsTrigger value="logs">Logs</TabsTrigger>
                {row.last_error && <TabsTrigger value="error">Error</TabsTrigger>}
              </TabsList>

              <TabsContent value="timeline">
                <div className="divide-y text-sm">
                  {(row.events ?? []).map((e, i) => (
                    <div key={i} className="flex gap-3 py-1.5">
                      <span className="w-32 shrink-0 text-xs text-muted-foreground tabular-nums">
                        {new Date(e.ts * 1000).toLocaleString()}</span>
                      <span className="w-20 shrink-0 font-mono text-xs">{e.kind}</span>
                      <span className="min-w-0 flex-1 truncate">{e.detail}</span>
                      {e.tokens > 0 && (
                        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                          {e.tokens.toLocaleString()} tok</span>)}
                    </div>
                  ))}
                </div>
              </TabsContent>

              <TabsContent value="logs">
                <div className="mb-2 flex gap-1">
                  {(row.stations ?? []).map((s) => (
                    <button key={s} onClick={() => setStation(s)}
                            className={`rounded px-2 py-1 font-mono text-xs ${
                              s === station ? "bg-muted" : "text-muted-foreground"}`}>
                      {s}</button>
                  ))}
                </div>
                {id && station && <LogTail id={id} station={station} />}
                {(row.stations ?? []).length === 0 && (
                  <div className="p-4 text-sm text-muted-foreground">
                    No agent has run for this row yet.</div>)}
              </TabsContent>

              {row.last_error && (
                <TabsContent value="error">
                  <pre className="whitespace-pre-wrap rounded border p-3 font-mono text-xs">
                    {row.last_error}</pre>
                </TabsContent>
              )}
            </Tabs>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
```

- [ ] **Step 2: Write the graph tab**

`web/src/views/GraphTab.tsx`:

```tsx
import { useEffect, useRef } from "react";
import mermaid from "mermaid";
import { useGraph } from "@/lib/api";

/** The live transition table, rendered beside the board it governs.
 *
 * Served from graph.to_mermaid() rather than a checked-in copy, so it cannot
 * drift from TRANSITIONS.
 */
export function GraphTab() {
  const { data } = useGraph();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!data?.mermaid || !ref.current) return;
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    mermaid.initialize({ startOnLoad: false, theme: dark ? "dark" : "default" });
    mermaid.render("state-graph", data.mermaid)
      .then(({ svg }) => { if (ref.current) ref.current.innerHTML = svg; });
  }, [data?.mermaid]);
  return <div ref={ref} className="pt-4" />;
}
```

- [ ] **Step 3: Wire into `App.tsx`**

```tsx
<TabsContent value="graph"><GraphTab /></TabsContent>
{/* outside <Tabs>, so the sheet opens from any view */}
<TaskSheet id={openId} onClose={() => setOpenId(null)} />
```

- [ ] **Step 4: Verify the log tail against a real run**

```bash
ai-task-runner run --dry-run --force
ai-task-runner serve --no-open &
```
Open a row with a completed station. Expected: the Logs tab lists that row's stations, the tail renders JSONL records, and a row whose agent has not run shows the explanatory sentence rather than an empty box. For a cross-repo ticket, confirm each repo's tab shows **its own** log — that is what Task 3 bought.

- [ ] **Step 5: Build and commit**

```bash
cd web && npm run build && cd ..
git add web runner/web/static
git commit -m "feat: task detail sheet, log tail and state graph tab

The log tail polls forward from the offset the previous read returned,
so a running station streams and a finished one reads identically.

The graph tab renders graph.to_mermaid() rather than a checked-in copy,
so it cannot drift from TRANSITIONS.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 10: `doctor`, README, and CLAUDE.md

**Files:**
- Modify: `runner/cli.py` (`_doctor`)
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing downstream.

- [ ] **Step 1: Add the doctor checks**

In `runner/cli.py`, inside `_doctor`, alongside the existing checks:

```python
    # Dashboard: both halves can be absent independently -- the extra may be
    # uninstalled, or the wheel may have been built without the static assets.
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        notes.append("dashboard: web extra installed")
    except ImportError:
        notes.append("dashboard: not installed (pip install -e '.[web]')")
    index = Path(__file__).parent / "web" / "static" / "index.html"
    if index.exists():
        notes.append(f"dashboard: assets present ({index})")
    else:
        notes.append("dashboard: assets MISSING -- run `npm run build` in web/")
```

Neither is a `problem`: a runner with no dashboard is a valid, supported configuration, and `doctor` must not exit non-zero for it.

- [ ] **Step 2: Add the README section**

Append to `README.md`, after the commands section:

````markdown
## Dashboard

```bash
pip install -e '.[web]'
ai-task-runner serve            # http://127.0.0.1:8787
```

A read-only view of this laptop's runner: what is stuck, what is running,
what it cost, and whether the work landed. It opens the SQLite store with
`mode=ro`, so it cannot write to the database, the tickets or the repos —
acting on the runner stays in the task manager and the CLI.

It binds loopback by default. `--host` accepts anything, and warns, because
the page carries ticket titles, PR URLs and error text.

To change the UI you need Node:

```bash
cd web && npm install && npm run dev    # proxies /api to :8787
cd web && npm run build                 # writes runner/web/static/, which is committed
```
````

- [ ] **Step 3: Add the CLAUDE.md subsection**

Append to `CLAUDE.md`, after the "Security posture" section:

```markdown
### Dashboard

`runner/web/` is a read-only HTTP view over the same SQLite file and log
directory the cron tick writes. It is a separate process; nothing was added to
a tick, and the runner does not know it exists.

Five invariants, each easy to undo by accident:

- **`db.Reader` opens SQLite with `mode=ro`.** Read-only is enforced at the
  storage layer, not by convention, so a convenience helper cannot quietly gain
  a write path. `Store.__init__` sets `journal_mode=WAL` so those reads do not
  collide with a tick mid-write.
- **`cli.py` must never import `runner.web` at module level.** FastAPI is an
  optional extra; the cron entrypoint keeps starting on `requests` + `pyyaml`.
  The `serve` handler imports lazily and turns `ImportError` into an install
  hint.
- **Everything egressing goes through `redact()`** — `last_error`, event
  details, agent log lines. Logs are already scrubbed at write time; this is the
  second layer, because a web endpoint is an outward path exactly like a ticket
  comment.
- **Row ids travel as query parameters, never path segments.** They contain
  `#`, which a browser strips before the request is sent; a routed row id in a
  path collapses onto its placeholder.
- **Day buckets use local midnight** (`date(ts,'unixepoch','localtime')`),
  matching `_midnight_epoch()`. `ts / 86400` buckets on UTC and makes the
  dashboard disagree with the cap the runner actually enforced.

`events` now carries `kind = "state"` rows written by `_set()` **and**
`_escalate()` — the latter writes state directly and would otherwise leave
every escalation uncounted. Throughput and cycle time are reconstructed from
them; before they existed the ledger held agent runs only and no history of
transitions.

Agent log filenames come from `models.agent_log_name(task_key, repo_key,
station)`. The repo key is load-bearing: `_EventLog` opens with `"w"`, so a
cross-repo ticket without it had two rows truncating each other's evidence.

`runner/web/static/` is committed build output. Vite writes there rather than
`web/dist` because `.gitignore` has an unanchored `dist/`.
```

- [ ] **Step 4: Verify**

Run: `ai-task-runner doctor`
Expected: the two dashboard notes appear and the exit code is unchanged from before this task.

Run: `python3 tests_smoke.py && python3 tests_web.py`
Expected: both print all PASS and exit 0.

- [ ] **Step 5: Commit**

```bash
git add runner/cli.py README.md CLAUDE.md
git commit -m "docs: record the dashboard and its invariants

doctor reports both halves independently: the optional extra may be
uninstalled, or the wheel may have been built without static assets.
Neither is a problem -- a runner with no dashboard is supported.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification

The whole thing, from a clean checkout:

```bash
pip install -e '.[web]'
python3 tests_smoke.py
python3 tests_web.py
ai-task-runner doctor
ai-task-runner serve --no-open
```

Expected: both test scripts print all PASS and exit 0; `doctor` reports the
dashboard notes; `serve` answers on `127.0.0.1:8787` with the built UI, and
every tab renders against real data.
