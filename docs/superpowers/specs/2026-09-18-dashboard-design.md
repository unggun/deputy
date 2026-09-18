# A local read-only dashboard for ai-task-runner

Date: 2026-09-18
Status: approved, ready for implementation planning

## Why

The runner already records everything an operator needs, and none of it is
visible without SQL. `tasks` holds per-row state, repo, branch, PR, attempts,
accrued cost and tokens; `events` is an append-only ledger indexed on `ts`; the
per-run JSONL logs under `runner.log_dir` are written as output arrives, so a
live run is readable while it runs. Today the only readers are
`ai-task-runner status`, which prints in-flight rows, and `sqlite3`.

Four questions go unanswered:

1. **Is anything stuck?** Blocked rows, expired leases, exhausted attempts.
2. **What is it doing right now?** In-flight rows by state, repo locks, slots.
3. **What is it costing?** Today against the limits, and the trend behind it.
4. **Did the work land?** Throughput, cycle time, review cycles.

This builds a localhost, read-only web dashboard that answers all four.

## Constraints that shape the design

- **The runner is tick-and-exit.** Nothing may introduce a long-lived process
  into a tick. The dashboard is a *separate* process that reads the same files;
  the runner is unaware of it.
- **SQLite is per-laptop.** The dashboard is inherently single-machine. Any
  design that needs cross-machine aggregation is a different project.
- **Nothing reaches a ticket unredacted** — and a web endpoint is an outward
  path, so it inherits the same rule.
- **`pip install` must keep working without Node**, and the cron path must not
  grow a web framework dependency.

## Scope

**In:** a read-only HTTP API over the existing SQLite DB and log directory; a
static single-page frontend; an `ai-task-runner serve` command; a `state` ledger
line in both `pipeline._set()` and `pipeline._escalate()`; one `PRAGMA` in
`Store.__init__`; an offline test script; doctor and docs updates.

**Out, decided explicitly:**

- **Any write path.** No retry, no lease release, no plan approval, no pause.
  Acting on the runner stays in ClickUp/Jira and the CLI. This is what keeps
  the dashboard a pure consumer that cannot corrupt a tick.
- **Remote or hosted access.** Localhost only. No auth, no TLS, because there
  is no exposure to authenticate against.
- **Websockets / streaming.** The runner advances one step per tick; polling
  is the honest model.
- **Replacing `ai-task-runner status`.** It stays; it is what works over SSH.

## Finding: the ledger records agent runs, not state changes

`store.record(...)` is called from exactly five sites in `pipeline.py`, all of
them agent runs: `triage` (242), `plan` (455), `implement` (515), `review`
(567), `fix` (710). Nothing records a state transition.

So questions 1-3 are derivable from the current schema, and question 4 is not:
there is no history of when a row reached `pr-open` or `done`, only its current
`state` and `updated_at`. Throughput over time cannot be reconstructed.

The fix is two lines in `_set()` (pipeline.py:892), already the single funnel
for state writes and already reading the old state for `graph.check`:

```python
def _set(self, rid: str, state: AIState) -> None:
    row = self.store.get(rid)
    if row:
        graph.check(AIState(row["state"]), state)
        self.store.record("state", rid, f"{row['state']}->{state.value}")
    self.store.set_state(rid, state)
    self._push_remote_state(rid)
```

No LLM, no cost, no new table, no new state. It makes cycle time, transitions
over time and review-cycle counts fall out of the existing `idx_events_ts`.

`_escalate()` calls `store.set_state` directly rather than `_set()`, so a
`blocked` transition from escalation is not captured by the line above. The
implementation must record the transition in `_escalate()` too, or throughput
will under-count escalations. Both sites, one `kind`: `"state"`.

## Shape of the work

This is one coherent deliverable but not one sitting. The natural phases, each
independently verifiable: (1) the `state` ledger lines plus WAL, with the
`tests_smoke.py` case; (2) `db.py` and `logs.py` plus `tests_web.py`, no HTTP;
(3) `api.py` and `serve`, verifiable with `curl`; (4) the frontend; (5) docs,
`doctor`, packaging. Phases 1-3 deliver value on their own — `curl` against a
read-only API already answers all four questions.

## Architecture

```
runner/web/
  __init__.py
  db.py       read-only SQLite queries -> plain dicts. No HTTP imports.
  logs.py     JSONL log discovery + byte-offset tail. No HTTP imports.
  api.py      FastAPI routes. Thin: parse, call db/logs, serialize.
  static/     committed Vite build output, served at /

web/          Vite + React + TS source. Node lives here and only here.
```

`db.py` and `logs.py` are HTTP-free so they are testable offline against a temp
SQLite file, matching how `tests_smoke.py` tests the state machine with a fake
provider and fake agent. The API layer stays thin enough that "does the
dashboard show the truth?" is a question about `db.py`.

### Read-only, enforced not promised

`db.py` opens the database with:

```python
sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
```

`mode=ro` makes writes fail at the SQLite layer, so a future convenience helper
cannot quietly gain a write path. Paired with `PRAGMA journal_mode=WAL` added
to `Store.__init__` (state.py:64) so a dashboard read does not collide with a
cron tick mid-write. WAL is persistent once set and is behaviour-preserving for
the existing single-writer usage.

### Redaction on egress

Every string that can carry subprocess output is passed through
`redact.redact()` before serialization: `tasks.last_error`, `events.detail`,
and every line of an agent log. Known secrets are gathered the way
`Pipeline._known_secrets()` (pipeline.py:931) gathers them, from the vcs
profiles in config. Logs are already scrubbed at write time; this is the second
layer, because the failure mode is permanent and silent.

### Row ids travel as query parameters

Row ids are `{source}:{provider_task_id}#{repo_key}`. `#` is the URL fragment
delimiter; a row id in a path segment is truncated at the `#` by the browser
before the request is sent, and every routed row would collapse onto its
placeholder. Ids are therefore always query parameters, URL-encoded.

## API

All endpoints are `GET`. All return JSON except `/` and static assets.

| Endpoint | Returns |
|---|---|
| `GET /api/overview` | `mode`; per-window open/closed; counts per `AIState`; active slots vs `max_active_tasks`; today's tokens and USD vs `daily_tokens` and `daily_budget_usd`; stuck count; most recent `events.ts` |
| `GET /api/tasks` | rows. Filters: `source`, `state`, `include_terminal` (default false) |
| `GET /api/task?id=<urlencoded>` | one row, its events, and which log files exist |
| `GET /api/task/log?id=&station=&offset=` | redacted JSONL lines from a byte offset; returns `next_offset` and `eof` |
| `GET /api/spend?days=30` | per-day `SUM(cost_usd)` and `SUM(tokens)`, grouped by event `kind` |
| `GET /api/throughput?days=30` | per-day transitions into `pr-open`/`done`/`blocked`; cycle times; review-cycle counts |
| `GET /api/graph` | `graph.to_mermaid()` |
| `GET /api/config` | non-secret summary: source keys, repo keys, limits, windows, mode |

`/api/config` returns a hand-built dict of named fields. It must never
serialize the `Config` object, which holds expanded `${VAR}` secrets.

### Day boundaries follow the runner, not UTC

`_limits_exceeded()` measures the daily gates from `_midnight_epoch(now)` —
**local** midnight. `/api/overview` must reuse that same helper, or the tokens
the dashboard shows will disagree with the cap the runner actually enforced,
which is the one number an operator must be able to trust.

The same applies to `/api/spend` and `/api/throughput`: bucket by local day
(`date(ts, 'unixepoch', 'localtime')` in SQL, or local-midnight boundaries
computed in Python), never by `ts / 86400`, which buckets by UTC and will split
an evening's work across two bars for anyone west of Greenwich.

### "Stuck" is defined once

In `db.py`, not in the frontend. A row is stuck if any holds:

- `state` in (`blocked`, `human-only`, `needs-input`, `plan-review`) — waiting
  on a human;
- `state == in-progress` and `lease_until < now` — a dead run;
- `attempts >= limits.max_fix_attempts`;
- `last_error` is non-empty.

Each stuck row carries a `reason` string naming which condition fired, so the
UI renders an explanation rather than a color.

## Frontend

**Stack:** Vite + React + TypeScript + Tailwind + shadcn/ui, with TanStack
Query (polling), TanStack Table (task tables), Recharts (shadcn's chart
wrapper), and mermaid for the graph tab.

Chosen over Next.js because the data is SQLite on this machine and the code
that interprets it — `AIState`, the `#`-namespaced row ids, `redact()` — is
Python, so the API is Python regardless; Next's server half would be dead
weight and a second runtime. Chosen over Svelte because shadcn/ui is
React-first and this UI is mostly tables, badges, sheets and a few charts.

### Views

Per the dataviz form heuristic, two of the four views are not charts: "is
anything stuck" and "what is it doing" are identity/state questions whose
honest form is a list and a board.

**Overview (landing).** Stuck banner (status palette, each row with an icon and
a word, never color alone; empty state states the good outcome). KPI stat tiles
with no plots: active slots vs cap, today's tokens vs `daily_tokens`, today's
spend vs `daily_budget_usd`, mode, window open/closed. A **last-tick
indicator** comparing the most recent `events.ts` to the current window — a
runner that has not ticked during an open `advance` window is the failure this
dashboard exists to catch, and nothing else on the page would reveal it. Mini
board linking into the Board tab.

**Board.** Columns in graph order (`new` -> `planning` -> `plan-review` ->
`queued` -> `in-progress` -> `review` -> `pr-open`), one card per routed row:
ticket key, repo, branch, attempts, accrued cost, PR link. Repo-lock badges so
`graph.repo_is_free` is visible rather than inferred. A cross-repo ticket shows
as two cards, which is the truth.

**Spend.** Daily tokens as a stacked bar segmented by station kind (`triage`,
`plan`, `implement`, `review`, `fix` — five fixed categorical hues, assigned in
order, never cycled, 2px surface gap between segments). Daily USD as a
**separate** stacked bar with the same x-range, stacked vertically to read as a
pair — explicitly not one chart with two y-axes. Cost per ticket as a
horizontal bar, top 10 with the remainder folded into "Other".

**Throughput.** Outcomes per day (transitions into `pr-open`, `done`,
`blocked`) — these are states, so they use the reserved status palette with
icons and labels. Cycle-time histogram (`new` -> `done` per row). Review-cycle
histogram against `max_review_cycles`.

**Graph tab.** `graph.to_mermaid()` rendered, so the live transition table sits
beside the board it governs.

**Task detail.** A shadcn Sheet openable from any row. Tabs: Timeline (that
row's events), Logs (one per `plan` / `implement` / `review` / `fix-N`,
tail-polled by byte offset, monospace), Error, Links (ticket, branch, PR).

### Color

Station kinds use the categorical palette; task states use the reserved status
palette. They must not overlap — `fix` must never render in the red that means
`blocked`. Both light and dark palettes are stepped from the same ramps and
**validated with `scripts/validate_palette.js` against their own surface**
before any chart code is written; dark mode is selected, not an inversion. Any
FAIL is fixed by re-stepping, not by judgement.

Legends are present for every chart with two or more series. Values, labels and
legends wear text tokens, never the series color.

### Polling

TanStack Query: 5s on overview and tasks, 2s on an open log tail. A visible
"updated Ns ago" and a manual refresh. No websockets.

## Packaging

### Optional dependency

```toml
[project.optional-dependencies]
web = ["fastapi>=0.110", "uvicorn[standard]>=0.29"]
```

**`cli.py` must never import `runner.web` at module level.** The `serve`
handler imports lazily and converts `ImportError` into an instruction to run
`pip install -e '.[web]'`. A cron host that never opens the dashboard never
installs a web framework, and `run` keeps starting on `requests` + `pyyaml`.

### Package data

Flat-layout auto-discovery finds Python packages and does not carry non-Python
files into the wheel. Without the following, `runner/web/static/**` is dropped
from the distribution and `serve` 404s every asset while importing cleanly:

```toml
[tool.setuptools]
packages = { find = { include = ["runner*"] } }

[tool.setuptools.package-data]
"runner.web" = ["static/**/*"]
```

The explicit `find.include` also keeps the new top-level `web/` source
directory out of flat-layout discovery.

### Build output is committed

`.gitignore` contains an unanchored `dist/`, which matches at any depth — a
conventional `web/dist` would be silently uncommitted and every fresh clone
would get an assetless dashboard. Vite therefore builds directly into
`runner/web/static/`, which is committed on purpose: `pip install` alone yields
a working dashboard and Node is needed only to change the UI. `web/node_modules/`
gets a new ignore line. The cost is build artifacts in diffs, accepted
deliberately for a single-developer tool.

### The command

```
ai-task-runner serve [--port 8787] [--host 127.0.0.1] [--no-open]
```

Defaults to loopback and opens a browser tab. A non-loopback `--host` is
allowed but prints a warning naming what is exposed — ticket titles, PR URLs,
error text — because localhost-only was a deliberate choice and this is the one
way to leave it by accident.

## Testing

`tests_web.py`, a plain PASS/FAIL script in the style of `tests_smoke.py` — not
pytest, prints per case, exits non-zero on failure. It seeds a temp SQLite DB
and asserts, with no HTTP and no network:

- each of the four stuck conditions is detected, and a healthy row is not;
- spend and throughput aggregate correctly across a day boundary;
- a planted fake token in `last_error` and in a log line comes back
  `[REDACTED]`;
- **a write through the API's connection raises** — proving `mode=ro` rather
  than trusting it;
- a row id containing `#` round-trips through the query-parameter encoding.

`tests_smoke.py` gains one case asserting that `_set()` and `_escalate()` write
a `state` event, since throughput depends on it.

## Docs

- `doctor` reports whether the `web` extra is importable and whether
  `runner/web/static/index.html` is present.
- README gains a Dashboard section.
- CLAUDE.md gains a short subsection recording the invariants: read-only
  connection, redaction on egress, localhost default, no writes to provider
  state, `cli.py` must not import `runner.web` eagerly.

## Known consequences

- **Build artifacts in git.** Accepted; see above.
- **WAL adds `-wal` and `-shm` files** beside the database. `.gitignore` covers
  `*.db` and `*.db-journal` but not these; it needs `*.db-wal` and `*.db-shm`.
- **The `state` event kind increases `events` growth** by roughly one row per
  transition — trivial next to the agent-run rows already written, and there is
  no retention policy today for either.
- **The dashboard shows one laptop.** If the runner ever runs on two machines,
  this shows whichever one it is served from, and that is correct but easy to
  misread. The overview names `runner.id`.
