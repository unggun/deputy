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

# --- stuck() must be emptiable ---------------------------------------------
# `last_error` is never cleared anywhere in the codebase (pipeline.py writes
# it, nothing wipes it), so without a terminal filter the "Needs a human"
# list and the red KPI tile degrade into a permanent record of every error
# the runner ever hit -- "Nothing is stuck" becomes unreachable after the
# first escalation, and the dashboard disagrees with `deputy status`,
# which does filter terminals.
with tempfile.TemporaryDirectory() as tmp:
    path, st = seed(tmp)
    now = int(time.time())
    st.upsert("acme:6#api", source="acme", provider_task_id="6",
              task_key="DEV-6", title="finished, but hit a blip on the way",
              state="done", repo_key="api", attempts=2,
              last_error="gh: timed out waiting for checks")
    st.upsert("acme:7#api", source="acme", provider_task_id="7",
              task_key="DEV-7", title="escalated", state="blocked",
              repo_key="api", attempts=3, last_error="boom")
    st.upsert("acme:8#api", source="acme", provider_task_id="8",
              task_key="DEV-8", title="denylisted", state="human-only",
              repo_key="api", attempts=0)
    rdr = Reader(path, secrets=[], limits={"max_fix_attempts": 3})
    stuck = {r["task_key"]: r["reason"] for r in rdr.stuck(now)}

    check("a done row with a stale last_error is NOT stuck",
          "DEV-6" not in stuck)
    check("a blocked row is still stuck -- that is what blocked means",
          "waiting on a human (blocked)" in stuck.get("DEV-7", ""))
    check("a human-only row is still stuck",
          "waiting on a human (human-only)" in stuck.get("DEV-8", ""))
    check("a non-terminal row with last_error is still stuck",
          "last run recorded an error" in stuck.get("DEV-5", ""))
    check("a terminal row is not flagged for a stale last_error",
          "last run recorded an error" not in stuck.get("DEV-7", ""))

    # The headline claim: with nothing in flight and nothing waiting, the
    # banner can reach "Nothing is stuck" again.
    st.conn.execute("DELETE FROM tasks")
    st.conn.commit()
    st.upsert("acme:9#api", source="acme", provider_task_id="9",
              task_key="DEV-9", title="shipped", state="done", repo_key="api",
              attempts=1, last_error="a transient failure, long since past")
    rdr2 = Reader(path, secrets=[], limits={"max_fix_attempts": 3})
    check("the stuck list empties once the work is done",
          rdr2.stuck(now) == []
          and rdr2.overview(dt.datetime.now())["stuck"] == 0)

# --- the two _midnight_epoch definitions must agree -------------------------
# db.py duplicates pipeline's definition rather than importing it (importing
# `pipeline` would pull the providers, the agent adapter and `requests` into
# a process whose only job is to read a file). Its docstring says "a test
# pins it"; this is that test.
from runner.pipeline import _midnight_epoch as _pipeline_midnight
from runner.web.db import _midnight_epoch as _web_midnight

_agree = all(
    _web_midnight(when) == _pipeline_midnight(when)
    for when in (
        dt.datetime(2026, 1, 1, 0, 0, 0),
        dt.datetime(2026, 3, 29, 2, 30, 0),    # spring DST boundary (EU)
        dt.datetime(2026, 6, 15, 12, 0, 1),
        dt.datetime(2026, 10, 25, 2, 30, 0),   # autumn DST boundary (EU)
        dt.datetime(2026, 12, 31, 23, 59, 59),
        dt.datetime.now(),
    ))
check("db._midnight_epoch agrees with pipeline._midnight_epoch", _agree)

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

with tempfile.TemporaryDirectory() as tmp:
    path, st = seed(tmp)
    # First event is well outside the 30-day reporting window; only the
    # ->done transition falls inside it. A window-bounded first-event scan
    # would understate this to (done_ts - window_start) instead of the true
    # (done_ts - old_first_ts).
    old_first = dt.datetime.now() - dt.timedelta(days=40)
    recent_done = dt.datetime.now() - dt.timedelta(days=3)
    old_first_ts = int(old_first.timestamp())
    done_ts = int(recent_done.timestamp())
    st.conn.execute(
        "INSERT INTO events (ts, task_id, kind, detail) VALUES (?,?,?,?)",
        (old_first_ts, "acme:1#api", "triage", "ok"))
    st.conn.execute(
        "INSERT INTO events (ts, task_id, kind, detail) VALUES (?,?,?,?)",
        (done_ts, "acme:1#api", "state", "in-progress->done"))
    st.conn.commit()
    rdr = Reader(path, limits={})
    cycles = {c["task_id"]: c["seconds"] for c in rdr.cycle_times(30)}
    check("cycle time uses the row's true first event, not the window edge",
          cycles.get("acme:1#api") == done_ts - old_first_ts)
    check("cycle time is not clamped to the reporting window",
          cycles.get("acme:1#api", 0) > 35 * 86400)

# --- overview: dashboard vs. pipeline agreement -----------------------------
# `Reader.overview()`'s default for an unset `daily_budget_usd` must match
# `Pipeline._limits_exceeded`'s -- a dashboard that reported $0 while the
# pipeline was actually enforcing $20 would disagree with the gate it exists
# to report on. Exercised against the real `Pipeline._limits_exceeded`
# (duck-typed `self`, since building a full `Pipeline` needs a provider and
# an agent) rather than a literal `20` copied into the test, so a future
# change to the pipeline's default fails this test instead of silently
# diverging from the dashboard.
from runner.pipeline import Pipeline


class _FakeStore:
    def __init__(self, spend: float = 0.0) -> None:
        self._spend = spend

    def spend_since(self, epoch: int) -> float:
        return self._spend

    def tokens_since(self, epoch: int) -> int:
        return 0


class _FakeSelf:
    def __init__(self, limits: dict) -> None:
        self.limits = limits
        self.store = _FakeStore()


with tempfile.TemporaryDirectory() as tmp:
    path, st = seed(tmp)
    rdr = Reader(path, limits={})
    reported_budget = rdr.overview(dt.datetime.now())["daily_budget_usd"]

    fake = _FakeSelf({})
    fake.store = _FakeStore(spend=reported_budget - 0.01)
    under = Pipeline._limits_exceeded(fake, dt.datetime.now())
    fake.store = _FakeStore(spend=reported_budget + 0.01)
    over = Pipeline._limits_exceeded(fake, dt.datetime.now())

    check("dashboard's default daily_budget_usd matches what the pipeline "
          "actually enforces",
          under is None and over is not None
          and "daily budget exhausted" in over)

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

    # Legacy fix-N logs must be discoverable via stations(), not just
    # readable via tail() -- pipeline.py writes fix-N stations too, and a
    # fixed ("plan", "implement", "review") legacy scan would leave one
    # readable but never listed for the UI to ask for.
    (root / "DEV-2-fix-1.log").write_text(
        _json.dumps({"seq": 1, "type": "result"}) + "\n", encoding="utf-8")
    check("legacy fix-N logs are discoverable via stations()",
          "fix-1" in weblogs.stations(root, "DEV-2", "api"))

with tempfile.TemporaryDirectory() as tmp:
    # A ClickUp Custom Task ID is whatever the workspace typed. Glob
    # metacharacters in it must not be read as wildcards: unescaped, "CU-[1]"
    # is a character class matching "CU-1", so this row would list the wrong
    # row's stations (or, more often, none of its own).
    root = Path(tmp)
    (root / "CU-[1]__api__implement.log").write_text("{}\n", encoding="utf-8")
    (root / "CU-1__api__review.log").write_text("{}\n", encoding="utf-8")
    check("glob metacharacters in a task key are matched literally",
          weblogs.stations(root, "CU-[1]", "api") == ["implement"])
    check("a literal task key does not pick up another row's logs",
          weblogs.stations(root, "CU-1", "api") == ["review"])

with tempfile.TemporaryDirectory() as tmp:
    # A record larger than a whole read window must not livelock: the reader
    # has to advance past it, not spin forever returning the same offset.
    root = Path(tmp)
    big_record = _json.dumps({"seq": 1, "type": "output",
                               "text": "x" * 170}) + "\n"
    (root / "DEV-3__api__implement.log").write_text(
        big_record, encoding="utf-8")

    first = weblogs.tail(root, "DEV-3", "api", "implement", 0, max_bytes=64)
    second = weblogs.tail(root, "DEV-3", "api", "implement",
                          first["next_offset"], max_bytes=64)
    check("oversized record advances next_offset instead of stalling",
          second["next_offset"] > first["next_offset"])
    check("oversized record's second poll is not identical to the first",
          second != first)

with tempfile.TemporaryDirectory() as tmp:
    # A missing db must fail with a sentence naming the path, not sqlite's
    # bare "unable to open database file" -- that's what `serve` and any
    # other caller that bypasses it are left to show an operator.
    missing = Path(tmp) / "no-such.db"
    try:
        Reader(str(missing))
        check("Reader on a missing db raises and names the path", False)
    except FileNotFoundError as exc:
        check("Reader on a missing db raises and names the path",
              str(missing) in str(exc))

# --- api.py --------------------------------------------------------------
# Nothing above exercises HTTP at all -- that's how the SPA catch-all
# swallowing /api/* got past review once. `fastapi` (and its TestClient)
# only ship with the optional `web` extra, so this whole block is guarded:
# a host without the extra must still get a clean run, not an ImportError,
# and a skip must never silently read as a pass (it is not appended to
# `results`).
try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

if TestClient is None:
    print("\napi.py checks skipped -- fastapi (the `web` extra) is not "
          "installed")
else:
    print("\ndashboard api layer")
    from runner.config import Config
    from runner.web import api as web_api

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = str(tmp_path / "api.db")
        st = Store(db_path)
        st.upsert("acme:1#api", source="acme", provider_task_id="1",
                  task_key="DEV-1", title="healthy", state="in-progress",
                  repo_key="api", attempts=0)

        # A synthetic static/assets dir so the SPA catch-all is actually
        # mounted (build_app only registers it when `STATIC / "assets"`
        # exists) -- otherwise the /api/does-not-exist check below would
        # pass for the wrong reason: no catch-all route to swallow it.
        static_dir = tmp_path / "static"
        (static_dir / "assets").mkdir(parents=True)
        (static_dir / "index.html").write_text("<html></html>",
                                               encoding="utf-8")

        cfg = Config(data={"runner": {"db_path": db_path,
                                      "log_dir": str(tmp_path / "logs"),
                                      "limits": {}},
                           "vcs_profiles": {}, "sources": []},
                    source_path=tmp_path / "config.yaml")

        # build_app reads the module-level STATIC constant at call time, so
        # pointing it at our synthetic dir before calling build_app is
        # enough -- no need to touch the real runner/web/static tree.
        original_static = web_api.STATIC
        web_api.STATIC = static_dir
        try:
            client = TestClient(web_api.build_app(cfg))

            r = client.get("/api/does-not-exist")
            check("unknown /api path 404s even with the SPA catch-all mounted",
                  r.status_code == 404)

            r = client.get("/api/task", params={"id": "acme:1#api"})
            check("task route resolves a row id containing '#'",
                  r.status_code == 200 and r.json().get("task_key") == "DEV-1")
            check("task route response has no meta key",
                  r.status_code == 200 and "meta" not in r.json())

            r = client.get("/api/task", params={"id": "nope"})
            check("task route 404s for an unknown id", r.status_code == 404)

            r = client.get("/api/tasks")
            check("tasks route responses have no meta key",
                  r.status_code == 200
                  and all("meta" not in t for t in r.json()["tasks"]))

            # The last-tick indicator has to reproduce Pipeline.tick's gate,
            # which is day-of-week AND advance window. Both have to be on the
            # wire or the tile falls back to a flat threshold that warns all
            # night and stays quiet through a real outage.
            r = client.get("/api/overview")
            check("overview carries the windows and days the tile gates on",
                  r.status_code == 200
                  and isinstance(r.json().get("windows"), dict)
                  and isinstance(r.json().get("days"), list))

            # Route-level provenance. The traversal test above calls
            # weblogs.tail directly, so it would stay green if the route
            # started taking task_key/repo_key off the query string. What
            # actually keeps a caller from naming its own log file is that
            # both come from a store lookup on the row id -- assert that, at
            # the route, with hostile query params present and ignored.
            _seen: list[tuple] = []
            _real_tail = weblogs.tail

            def _spy(log_root, task_key, repo_key, station, offset=0, **kw):
                _seen.append((task_key, repo_key, station))
                return _real_tail(log_root, task_key, repo_key, station,
                                  offset, **kw)

            weblogs.tail = _spy
            try:
                r = client.get("/api/task/log", params={
                    "id": "acme:1#api", "station": "implement",
                    "task_key": "escaped", "repo_key": "escaped"})
                ok = (r.status_code == 200
                      and _seen == [("DEV-1", "api", "implement")])
            finally:
                weblogs.tail = _real_tail
            check("log route takes task_key/repo_key from the store, never "
                  "from the query string", ok)

            # A routed row's history starts on its placeholder: triage and
            # any clarification round were recorded before routing existed.
            st.record("triage", "acme:1", "execute s 0.9", 0.01, 900,
                      duration_ms=7000, turns=1, model="claude-haiku-5")
            st.record("implement", "acme:1#api", "ok", 0.4, 40000,
                      duration_ms=540000, turns=30, log="implement")
            r = client.get("/api/task", params={"id": "acme:1#api"})
            kinds = [e["kind"] for e in r.json().get("events", [])]
            check("task history includes the placeholder's triage",
                  r.status_code == 200 and kinds == ["triage", "implement"])
            check("task events carry run telemetry",
                  r.json()["events"][1].get("duration_ms") == 540000
                  and r.json()["events"][1].get("log") == "implement")

            r = client.get("/api/runs", params={"days": 7})
            runs = r.json().get("runs", [])
            check("runs route lists both runs, attributed to the ticket",
                  r.status_code == 200 and len(runs) == 2
                  and all(x["task_key"] == "DEV-1" for x in runs))
            r = client.get("/api/runs", params={"station": "triage"})
            check("runs route filters by station",
                  [x["kind"] for x in r.json()["runs"]] == ["triage"])
            r = client.get("/api/runs", params={"limit": 100000})
            check("runs route refuses an unbounded limit", r.status_code == 422)

            r = client.get("/api/flow", params={"days": 7})
            check("flow route answers with rows and a summary before anything merged",
                  r.status_code == 200 and r.json()["rows"] == []
                  and r.json()["summary"]["merged"] == 0)
        finally:
            web_api.STATIC = original_static

# --- serve must not demand provider credentials -----------------------------
# The dashboard reads the local SQLite file and log directory and talks to no
# provider, so requiring CLICKUP_TOKEN/JIRA_TOKEN to be exported before it will
# render a read-only page is a credential demand with nothing behind it. It also
# breaks precisely where it matters: cron has those vars, an interactive shell
# does not. `run` and `doctor` must still refuse, since they really do call out.
import os as _os
from runner.config import Config as _Config, _expand as _cfg_expand

_saved = {k: _os.environ.pop(k, None)
          for k in ("CLICKUP_TOKEN", "JIRA_TOKEN", "GH_TOKEN_WORK")}
try:
    _raw = {"provider": {"token": "${CLICKUP_TOKEN}"}, "db": "~/x.db"}

    _strict_raised = False
    try:
        _cfg_expand(_raw)
    except RuntimeError:
        _strict_raised = True
    check("strict expansion still refuses an unset env var", _strict_raised)

    _lenient = _cfg_expand(_raw, require_env=False)
    check("lenient expansion leaves the reference as a literal",
          _lenient["provider"]["token"] == "${CLICKUP_TOKEN}")
    check("lenient expansion leaves other values untouched",
          _lenient["db"] == "~/x.db")

    _cfg = _Config.load("config.example.yaml", require_env=False)
    check("a config full of unset vars still loads for serve",
          _cfg.get("runner.db_path") is not None)
finally:
    for _k, _v in _saved.items():
        if _v is not None:
            _os.environ[_k] = _v

# --- flow: where time and human attention went --------------------------------
# One ticket, clarified before routing (on its placeholder id), then routed,
# planned, approved, implemented, reviewed by a human with feedback, merged.
def _ev(st, ts, tid, kind, detail="", **kw):
    st.record(kind, tid, detail, ts=ts, **kw)


with tempfile.TemporaryDirectory() as tmp:
    path = f"{tmp}/s.db"
    st = Store(path)
    t0 = int(dt.datetime.now().replace(hour=8, minute=0, second=0,
                                       microsecond=0).timestamp()) - 86400
    ph, rid = "acme:11", "acme:11#api"
    st.upsert(rid, source="acme", provider_task_id="11", task_key="DEV-11",
              title="flow", state="done", repo_key="api", pr_number=4,
              pr_url="http://pr/4")
    _ev(st, t0, ph, "triage", "clarify (s)", cost_usd=0.01)
    _ev(st, t0 + 1, ph, "state", "new->needs-input")
    _ev(st, t0 + 3601, ph, "state", "needs-input->new")      # 1h waiting on a human
    _ev(st, t0 + 3602, ph, "triage", "execute (m)")
    _ev(st, t0 + 3603, rid, "state", "new->planning")
    _ev(st, t0 + 3700, rid, "plan", "ready")
    _ev(st, t0 + 3701, rid, "state", "planning->plan-review")
    _ev(st, t0 + 5501, rid, "state", "plan-review->queued")  # 30m approving the plan
    _ev(st, t0 + 5502, rid, "state", "queued->in-progress")
    _ev(st, t0 + 6000, rid, "implement", "ok", duration_ms=400_000, turns=12,
        model="claude-sonnet-5", model_requested="sonnet", log="implement")
    _ev(st, t0 + 6001, rid, "state", "in-progress->review")
    _ev(st, t0 + 6100, rid, "review", "approve", log="review")
    _ev(st, t0 + 6101, rid, "state", "review->pr-open")
    _ev(st, t0 + 6700, rid, "pr_ready")                      # CI took 10m
    _ev(st, t0 + 9700, rid, "pr_review", "dana")             # 50m to first review
    _ev(st, t0 + 9800, rid, "fix", "feedback: ok", log="fix-1")
    _ev(st, t0 + 9900, rid, "fix", "ci: ok", log="fix-2")
    _ev(st, t0 + 10900, rid, "pr_merged", "human_commits=0")
    _ev(st, t0 + 10901, rid, "state", "pr-open->done")
    rdr = Reader(path, limits={})
    fl = rdr.flow(7)
    row = fl["rows"][0] if fl["rows"] else {}
    check("flow finds the merged row", len(fl["rows"]) == 1)
    check("flow counts from the placeholder's first event",
          row.get("total") == 10901)
    check("time to PR runs from first touch to pr-open",
          row.get("to_pr") == 6101)
    check("PR to first review is measured from ready-for-review",
          row.get("pr_to_review") == 3000)
    check("first review to merge", row.get("review_to_merge") == 10901 - 9700)
    check("waiting counts clarification, plan approval and the review wait",
          row.get("waiting") == 3600 + 1800 + 3000)
    check("a clarification round before routing is a touch",
          row.get("clarifications") == 1)
    check("fix passes are split by cause",
          row.get("fix_feedback") == 1 and row.get("fix_ci") == 1)
    check("a human-feedback fix makes the PR not autonomous",
          row.get("autonomous") is False)
    check("summary carries the waiting share",
          abs(fl["summary"]["waiting_share"] - 8400 / 10901) < 1e-9)
    check("time by state never includes done",
          "done" not in fl["summary"]["time_by_state"])

    # Same ticket shape, but nobody had to step in: autonomous.
    rid2 = "acme:12#api"
    st.upsert(rid2, source="acme", provider_task_id="12", task_key="DEV-12",
              title="clean", state="done", repo_key="api")
    _ev(st, t0, rid2, "state", "new->queued")
    _ev(st, t0 + 10, rid2, "state", "queued->in-progress")
    _ev(st, t0 + 20, rid2, "state", "review->pr-open")
    _ev(st, t0 + 30, rid2, "fix", "ci: ok")
    _ev(st, t0 + 40, rid2, "pr_merged", "human_commits=0")
    _ev(st, t0 + 50, rid2, "state", "pr-open->done")
    # And one whose own pushes predate SHA tracking: unknown, not a guess.
    rid3 = "acme:13#api"
    st.upsert(rid3, source="acme", provider_task_id="13", task_key="DEV-13",
              title="old", state="done", repo_key="api")
    _ev(st, t0, rid3, "state", "review->pr-open")
    _ev(st, t0 + 5, rid3, "fix", "ok")
    _ev(st, t0 + 9, rid3, "pr_merged", "human_commits=?")
    _ev(st, t0 + 10, rid3, "state", "pr-open->done")
    rdr = Reader(path, limits={})
    fl = rdr.flow(7)
    by = {r["task_id"]: r for r in fl["rows"]}
    check("no human fix and no human commit is autonomous",
          by[rid2]["autonomous"] is True)
    check("an unknown merge is neither autonomous nor not",
          by[rid3]["autonomous"] is None)
    check("the autonomy rate is over the rows that can say",
          fl["summary"]["autonomy"] == {"known": 2, "autonomous": 1, "rate": 0.5})
    check("a merge with no human review has no review-stage time",
          by[rid2]["pr_to_review"] is None and fl["summary"]["n"]["pr_to_review"] == 1)

    # A person reviewed the draft before the runner marked it ready.
    rid4 = "acme:14#api"
    st.upsert(rid4, source="acme", provider_task_id="14", task_key="DEV-14",
              title="early review", state="done", repo_key="api")
    _ev(st, t0, rid4, "state", "review->pr-open")
    _ev(st, t0 + 5, rid4, "pr_review", "dana")
    _ev(st, t0 + 9, rid4, "pr_ready", "")
    _ev(st, t0 + 20, rid4, "pr_merged", "human_commits=0")
    _ev(st, t0 + 21, rid4, "state", "pr-open->done")
    early = {r["task_id"]: r for r in Reader(path, limits={}).flow(7)["rows"]}[rid4]
    check("a review before the PR was marked ready waited zero, never negative",
          early["pr_to_review"] == 0 and early["review_to_merge"] == 16)

    # Marker lines carry no money and must not reach the spend chart.
    kinds = set()
    for d in rdr.spend_by_day(7):
        kinds |= set(d["kinds"])
    check("PR milestone markers never appear as spend kinds",
          not kinds & {"state", "pr_ready", "pr_review", "pr_merged"})

    # --- runs ------------------------------------------------------------
    runs = rdr.runs(7)
    check("runs lists agent runs only", {r["kind"] for r in runs}
          <= {"triage", "plan", "implement", "review", "fix"})
    impl = next(r for r in runs if r["kind"] == "implement")
    check("a run's start is derived from its duration",
          impl["started"] == impl["ts"] - 400)
    check("an alias request answered by its model is not a mismatch",
          impl["model_mismatch"] is False and impl["model"] == "claude-sonnet-5")
    tri = next(r for r in runs if r["kind"] == "triage")
    check("a placeholder's triage run is attributed to its ticket",
          tri["task_key"] == "DEV-11" and tri["open_id"] == rid
          and tri["repo_key"] is None)
    check("fix outcomes read through the cause prefix",
          {r["outcome"] for r in runs if r["kind"] == "fix"} == {"ok"})
    check("runs filter by station",
          {r["kind"] for r in rdr.runs(7, station="fix")} == {"fix"})

with tempfile.TemporaryDirectory() as tmp:
    path = f"{tmp}/s.db"
    st = Store(path)
    now = int(time.time())
    st.record("review", "acme:1#api", "approve", model="claude-opus-5",
              model_requested="sonnet", ts=now)
    st.record("implement", "acme:1#api", "agent timed out after 60s", ts=now)
    st.record("fix", "acme:1#api", "ci+feedback: boom: no", ts=now)
    rdr = Reader(path, limits={})
    by_kind = {r["kind"]: r for r in rdr.runs(1)}
    check("a different model answering is flagged",
          by_kind["review"]["model_mismatch"] is True)
    check("an implement error is a failed run",
          by_kind["implement"]["failed"] is True)
    check("a fix error after its cause is a failed run",
          by_kind["fix"]["failed"] is True)

# --- numbered station logs ----------------------------------------------------
from runner.models import run_station
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    for name in ("implement", "implement-2", "implement-10", "review", "plan",
                 "fix-1", "review-2"):
        (root / f"DEV-1__api__{name}.log").write_text("{}\n", encoding="utf-8")
    check("numbered station logs list in pipeline then run order",
          weblogs.stations(root, "DEV-1", "api")
          == ["plan", "implement", "implement-2", "implement-10", "review",
              "review-2", "fix-1"])
    check("a numbered station log is readable",
          weblogs.tail(root, "DEV-1", "api", "implement-2", 0)["missing"] is False)
    check("the first run keeps the bare name; later runs are numbered",
          (run_station("implement", 1), run_station("implement", 2),
           run_station("fix", 1)) == ("implement", "implement-2", "fix-1"))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
