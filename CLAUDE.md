# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ai-task-runner` — a resumable bridge between a task manager (ClickUp / Jira) and a
coding agent (Claude Code). It picks up tickets assigned to the developer, triages
them, plans, writes code in an isolated worktree, opens a draft PR, and follows the
PR through CI/review until merge.

## Layout

```
runner/
  __init__.py  cli.py  config.py  graph.py  models.py  pipeline.py  state.py  triage.py  vcs.py
  analyst.py  review.py  brain.py  jsonio.py  redact.py
  agents/    __init__.py  base.py (CodingAgent)   claude_code.py
  providers/ __init__.py  base.py (TaskProvider)  clickup.py  jira.py
  web/       __init__.py  api.py  db.py  logs.py  static/ (committed build output)
web/           frontend source (Vite + React); `npm run build` writes into runner/web/static/
tests_smoke.py  tests_web.py  README.md  config.example.yaml
```

Note the two unrelated `base.py` files: `providers/base.py` defines `TaskProvider`
(7 methods + optional `blocked_by`), `agents/base.py` defines `CodingAgent`
(`reason` for tool-less classification, `work` for agentic runs). Confusing one for
the other imports cleanly and only fails at the consumer, so check which you have open.

Packaging is `pyproject.toml` at the root. It does **not** use plain setuptools
flat-layout auto-discovery: `packages = { find = { include = ["runner*"] } }` is
explicit, because the top-level `web/` frontend source is a sibling directory at
the repo root and auto-discovery would try to treat it as a package too. A
`[tool.setuptools.package-data]` entry separately ships `runner/web/static/**/*`
— auto-discovery finds Python packages and carries no other files, so without
that entry the built dashboard assets would be silently absent from the wheel
while the package still imported cleanly. The console script is
`ai-task-runner = runner.cli:main`, so `pyproject.toml` must stay at the repo
root, not inside `runner/`.

## Commands

```bash
ai-task-runner doctor                     # validate graph, vcs profiles, provider creds, repo mapping
ai-task-runner run --dry-run --force      # one tick, ignore time windows, no writes
ai-task-runner run                        # one tick, respects windows/days; this is the cron entrypoint
ai-task-runner run --source acme          # limit to one source (repeatable)
ai-task-runner daemon --interval 900      # loop, for hosts where cron is awkward
ai-task-runner status                     # in-flight rows across all sources
ai-task-runner graph                      # print the live transition table as Mermaid
python3 tests_smoke.py                    # offline state-machine tests (fake provider + fake agent)
```

`tests_smoke.py` is a plain script, not pytest — it prints `PASS`/`FAIL` per case and
exits non-zero on failure. There is no single-test flag; comment out cases or add a new
`run_case(...)` block. It loads `config.example.yaml` directly, so changing that file's
`mycorp` source, windows, or policy will change test outcomes.

## Architecture

### Tick-and-exit

`Pipeline.tick()` advances **each task by at most one step, then returns**. Nothing
blocks on CI or a human. A closed laptop or crashed run costs one step, not a ticket.
Any change that introduces waiting, polling loops, or long-lived processes inside a
tick breaks the core property.

Tick order (`pipeline.py`): day/window/budget gates → advance in-flight rows (cheap,
no LLM) → triage new tickets (LLM, capped per run).

### The state graph is data

`graph.py::TRANSITIONS` is the single source of truth. `graph.check()` raises
`IllegalTransition` rather than letting a bad write through; `graph.to_mermaid()` and
`validate_graph()` (run in `doctor` and in tests) read the same object, so the README
diagram cannot drift. Add states by editing `TRANSITIONS` and `AIState` — never by
adding `if` branches in `pipeline.py`.

States: `new → needs-input | human-only | planning → plan-review (AWAITING_PLAN_OK) | queued | blocked`,
then `queued → in-progress → review → pr-open → done`. `human-only`, `blocked`, `done` are terminal.

### Layered indirection

`pipeline.py` knows nothing about ClickUp, Jira or Claude Code. Adding a provider means
implementing the 7 abstract methods of `providers/base.py::TaskProvider` (+ optional
`blocked_by`) and registering it in `providers/__init__.py`. Adding an agent means the
2-method `agents/base.py::CodingAgent` (`reason`, `work`). Keep provider-specific
knowledge inside the provider.

### Sources and vcs profiles

`sources` is the top-level unit: one workspace, its own credentials, statuses and repos.
Each source is ticked independently and `cli.py::_tick_all` swallows per-source
exceptions so one broken workspace never stops the others. Repos reference a named
`vcs_profile` for git/GitHub identity (`token_env` preferred; `gh_config_dir` as the
alternative). Never introduce `gh auth switch` — it mutates global state and races
between parallel runs.

### Local state vs. remote state

SQLite (`Store`) is per-laptop; the AI State field/property on the ticket is the only
shared state. Row ids are namespaced so two workspaces cannot collide in one DB —
`_row_id()` builds them, `_pid()` unwraps them. Two shapes:

- `"{source.key}:{provider_task_id}"` — the pre-triage **placeholder**. It records
  that the ticket was seen (and any triage crash, which has no repo to blame) before
  routing has happened. `clarify` and `human_only` decisions end here too.
- `"{source.key}:{provider_task_id}#{repo_key}"` — a **routed** row, one per repo.
  `_route()` creates them and retires the placeholder.

Because the ids are unknowable until triage has routed the ticket, "do I already know
this ticket?" is `store.rows_for_task(source, pid)`, never `store.get(row_id)`. Rows
written before per-repo fan-out existed carry the two-segment id *with* a `repo_key`
and no `#`; they keep advancing unchanged, and `_route()` reuses rather than re-keys
them.

**Never adopt an orphan.** A ticket marked in-flight remotely with no local row means
another laptop or a reassignment; `_orphan()` marks it `blocked` with an explanatory
comment and stops. The branch, worktree and PR live elsewhere.

### One ticket, several repos

A Jira project routinely covers a backend and a frontend, and nothing in the ticket
metadata tells them apart. So **triage routes**: `Source.repos_for_container()` returns
every candidate, `_build_prompt` shows the model each one's `key`, `language` and
`notes`, and it returns `repos: [...]`. `_pick_repos()` drops anything not offered, and
with a single candidate the model is not consulted at all — a hallucinated key must not
turn a routable ticket into a spurious `clarify`. Routing quality is entirely a function
of how accurate `language` and `notes` are in config; they are not decoration.

A ticket naming two repos gets **two independent rows** — own branch, worktree, PR,
review cycle, attempt counter and brain file. What they share is the one remote AI State
field, so writes to it are aggregated by `_remote_state()`: **least advanced wins**
(a ticket is only `done` when every repo is), except that `blocked`/`human-only` on any
row outranks progress on the others. Never call `provider.set_ai_state` directly from a
station — go through `_set()` / `_push_remote_state()`, or one repo's progress will
overwrite the other's.

Provider *workflow status* writes are gated the same way through `_set_status_when()`:
the first repo to merge must not flip the ticket to `done_status` while the second PR is
still open.

Re-triage (a human answered, or rejected a plan) **keeps the existing repo set** —
`_candidate_repos()` offers only the repos already assigned. Re-routing would strand
whatever branch and worktree the dropped row already owned.

### Concurrency and safety invariants

- **One agent per repo** (`graph.repo_is_free`). Two agents branching off the same base
  produce conflicting PRs. File-overlap detection is a tempting refinement but you don't
  know which files a ticket touches until after the agent has run. A cross-repo ticket
  does not violate this — its rows are in *different* repos — but it does consume one
  `max_active_tasks` slot per repo, which is correct: that is two agent runs.
- **Leases** (`store.acquire_lease` / `release_lease`) guard execution; an expired lease
  on an `in-progress` row means a dead run and requeues with an incremented attempt.
- **Idempotent comments** — every comment goes through `_comment(rid, key, md)`, which
  claims `"{rid}:{key}"` in the store first. Reruns of the same tick must not duplicate
  comments (there is a smoke test for this). Always pass a stable, meaningful key.
- **Bot detection** is marker-based: `runner.bot_marker` (default `<!-- ai-runner -->`)
  is prepended to every comment; providers set `Comment.is_bot` by searching for it.
  Human-reply detection depends on this, so never post via a raw provider call.
- **Budget** — daily spend is checked against `limits.daily_budget_usd` before any work;
  every LLM step records cost via `store.record(...)`.
- **`REVIEW` counts toward `max_active_tasks` but does not hold its repo**, matching
  `PR_OPEN`. The repo lock exists to stop two agents branching off the same base; a
  ticket already in review or already PR'd is no longer racing anyone for that base.

### Four model seams

`CodingAgent` has two methods, `reason` and `work`, but `work()` takes `model` and
`allowed_tools` overrides — so the analyst and reviewer stations reuse it with their
own model and tool set instead of adding new methods. That gives four effectively
independent seams:

- `reason(prompt, *, max_turns=1)` — triage. Runs `claude -p` with `--allowedTools ""`
  and one turn, so **triage never reads the repo**; it judges `scope` *and routes the
  ticket to one or more repos* from ticket text plus each candidate repo's `language`
  and `notes` in config. Uses `agent.triage_model`. `--allowedTools ""` removes the
  ability to *act* but not the ambient context, so isolation is a separate mechanism —
  see below.
- `work(prompt, cwd, *, log_path=None)` — implementation and fix passes. 60 turns, full
  allowlist, cwd is the worktree. Uses `agent.model`.
- `work(prompt, cwd, model=agent.model, allowed_tools=agent.planner_tools)` — the
  analyst station. `planner_tools` grants no write capability (`Read`, `Glob`,
  `Grep`, read-only `Bash(git log:*)`/`Bash(git diff:*)`), which is what makes planning
  safe to run even in `mode: shadow`.
- `work(prompt, cwd, model=agent.reviewer_model, allowed_tools=agent.reviewer_tools)`
  — the review station. `reviewer_model` falls back to `agent.model`; `reviewer_tools`
  is read-only plus the repo's own `test` command, appended at call time.

`triage_model` falls back to `model` when unset. `allowed_tools` includes `Agent`, which
is what permits `work()` to dispatch subagents — the tool is named `Agent`, not `Task`.
Subagent definitions are read from `.claude/agents/*.md` in the **target repo**, since
that is the cwd; the runner does not define them (no `--agents` plumbing yet).

### Billing: quota vs API credits

`claude -p` bills exactly like an interactive session — auth decides, not `-p`. With a
claude.ai login the runner spends the developer's **5-hourly/weekly subscription quota**,
competing with their own Claude Code use. The `total_cost_usd` the CLI reports is then an
API-*equivalent* number, which means `limits.daily_budget_usd` is gating on money nobody
pays while the real constraint goes untracked.

To put the runner on separate pay-as-you-go credits, set `ANTHROPIC_API_KEY` in the cron
environment only (`_run()` passes the parent env through `_sanitized_env()`, which strips
only `GIT_*`, so this scopes cleanly). Add `CLAUDE_CODE_SIMPLE=1` so a missing key fails
loudly instead of silently falling back to the subscription.

So **both** currencies are recorded. Every agent run returns a `TokenUsage`
(input + output + cache-create + cache-read); `Spend` carries it alongside `cost_usd`
through the stations and `StationError`, and `store.record(...)` writes both to the
`events` ledger and accrues both onto the task row. `limits.daily_tokens` gates on the
token sum exactly as `daily_budget_usd` gates on the dollar sum — `_limits_exceeded()`
checks both before every agent run and reports whichever tripped. `daily_tokens: 0`
(the default) disables the token gate. For scale: a trivial one-turn `work()` run
measured 40k tokens and $0.03, so on a subscription the dollar limit will essentially
never fire on its own.

### Triage isolation

`reason()` used to pass `cwd=None`, so triage inherited the directory cron launched the
runner from — and `claude` loads that directory's `CLAUDE.md`, settings, skills, MCP tool
definitions and git state into the system prompt *before* reading the prompt. Triage did
not "never read the repo": it read whichever repo it was standing in, and answered about
it. Asked what project it was in, unisolated triage recited this repository's `CLAUDE.md`;
isolated triage answers `NO PROJECT CONTEXT`.

So `reason()` runs in `agent.triage_dir` (a fixed empty directory, default
`~/.ai-runner/triage`) with `TRIAGE_ISOLATION`: `--strict-mcp-config`,
`--disable-slash-commands`, `--setting-sources ""`. Fixed and not a temp dir because the
cwd is part of the system prompt — a path that changes per call misses the prompt cache
per call. An uncreatable directory logs and falls back to the old behaviour rather than
stranding every new ticket. `agent.isolate_triage: false` opts out.

That fallback is silent by design and therefore easy to be wrong about, so `doctor`
**checks it rather than trusting it**: it runs one real `reason()` call with
`cli.py::CANARY_PROMPT` and asserts the reply contains `CANARY_SENTINEL`
(`NO PROJECT CONTEXT`, kept verbatim in both places so the doc and the check cannot
drift). `_isolation_status()` grades the reply `ok` / `skipped` / `broken` /
`unverified`, and **`unverified` is not a pass** — a canary that timed out or came back
empty proves nothing, so it lands in `problems` and `doctor` exits non-zero. Only
`agent.isolate_triage: false` is graded `skipped`, since that is a config choice rather
than a fault. The check costs one small agent run per `doctor`; `doctor` is operator-run,
not the cron entrypoint, so that is affordable — do not move it into `run`.

`work()` is deliberately **not** isolated: the target repo's own `CLAUDE.md` and settings
are exactly the context the implementer, analyst and reviewer are supposed to have. Do not
"make this consistent" by applying the triage flags to `work()`.

### Agent process supervision

`work()` runs `claude -p --output-format stream-json --verbose` and consumes NDJSON as it
arrives; the final `type: "result"` event carries `result`, `total_cost_usd`, `num_turns`,
`duration_ms` and `usage`. `reason()` stays on `--output-format json` — one turn, nothing
to stream. Three invariants live in `agents/claude_code.py` and are easy to undo by
accident:

- **The child leads its own process group** (`start_new_session=True`) and timeouts go
  through `_kill_tree()`, which signals the *group* (SIGTERM, then SIGKILL after
  `TERM_GRACE_SECONDS`). `subprocess.run(timeout=...)` kills only the direct child, and
  Claude Code's bash invocations, MCP servers and subagents outlive it — still holding the
  worktree, still spending quota. `_kill_tree()` also runs on the normal exit path, for the
  same reason. Never replace this with a plain `run(timeout=...)`.
- **stderr is drained on its own thread.** Not optional: a child that fills its stderr pipe
  blocks on write forever while the main thread is reading stdout. The same reasoning
  applies at the `_EventLog` cap — it stops *recording*, but the pipes keep being read.
- **stdin is `DEVNULL`.** `claude -p` reads a non-TTY stdin and *appends it to the
  prompt*. An inherited stdin therefore feeds the agent whatever happens to be on it — a
  heredoc, a pipe, or a terminal it then blocks on forever. The prompt goes in argv; this
  is not a spare channel.
- **The environment is sanitised.** `_sanitized_env()` strips `GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_INDEX_FILE` and friends, which otherwise override the worktree and point the agent's
  `git` at a different repository, and sets `AI_TASK_RUNNER_RUN_ID` so a nested agent can
  tell it is already inside a managed run.
- **The prompt has a ceiling.** It travels in argv, so past the kernel's limit the exec
  fails as a bare `OSError` — at the one moment an operator most needs a readable
  sentence. `_run()` refuses first, returning `ok=False` with both byte counts, which the
  stations already turn into a `StationError` and an escalation. The check lives in
  `_run()`, the single funnel `reason()` and `work()` both pass through, so a fifth seam
  cannot be added that skips it; and it counts `len(prompt.encode())`, not `len(prompt)`,
  or a multibyte prompt slips through at three times the ceiling.
  `agent.max_prompt_bytes` defaults to 400000 on macOS and 120000 on Linux, which caps a
  single argument at 128 KiB. **Never truncate instead** — a shortened prompt buys a
  confident answer about the wrong ticket.
- **The model is verified, not assumed.** `--model` is a request; the result payload
  reports what answered in `modelUsage`. `_check_model()` warns when the requested model
  is absent from a non-empty `modelUsage` and does nothing else. Warn-only is deliberate
  twice over: a `work()` run legitimately reports several models (`allowed_tools`
  includes `Agent`, and subagents bill separately), so the test is *presence*, not
  exclusivity; and an absent or unrecognised `modelUsage` stays silent so an upstream
  schema change cannot warn on every run. Matching tolerates alias-vs-canonical
  (`sonnet` → `claude-sonnet-5`).

The per-run JSONL event log under `runner.log_dir` is written as output arrives and flushed
per event, so a killed run still leaves evidence — the previous `log_path` write only
happened after the process returned, which is exactly when it was least likely to.
`agent.max_log_bytes` bounds it.

### Gating: mode, windows, policy

`runner.mode: shadow` triages and comments but never writes code — the kill switch,
along with the per-ticket `no-ai` tag. Windows are three separate ranges (`advance`,
`triage`, `execute`), enforced in-process, not in cron; `--force` widens them to 24/7.
The plan-approval gate is `policy.require_plan_approval_from_scope` compared against
the triage `scope` (`xs < s < m < l`); shadow mode always gates.

`triage.py`'s denylist **overrides the model** — prod data, migrations, auth, payments,
secrets, infra go to `human-only` regardless of what the LLM decided. Keep it that way;
a smoke test asserts it.

### Brain

`brain.py` holds durable per-repo notes at `runner.brain_dir`, one markdown file per
`{source}__{repo}`. It is appended to **only on an approving review**, before the PR is
even opened — a rejected plan, a rejected/changes-requested review, or a crashed run
writes nothing, so the brain only accumulates notes from changes an independent review
actually approved. It refuses to grow past `limits.brain_max_bytes`: at the
cap it logs and skips the append rather than evicting older notes, because a brain that
quietly forgets is worse than one that tells you to prune it. It is plain markdown in a
known directory — editing or deleting it by hand is a supported operation, not
corruption.

## Provider gotchas worth preserving

- **Jira state** lives in an issue *entity property* (`ai-runner-state`), not a custom
  field — no Jira admin, atomic writes. Only the four human-actionable states are
  mirrored to `ai:*` labels so boards stay readable.
- **Jira JQL**: `labels NOT IN ("no-ai")` silently drops issues with *no labels at all*.
  The `(labels IS EMPTY OR ...)` branch is required, not defensive.
- **Jira `require_labels`** is the opt-*in* counterpart to `skip_labels`: an AND of
  `labels = "x"` clauses, so more entries narrow the funnel. It only shapes
  `fetch_candidates()`, i.e. new triage — `_advance` re-reads in-flight rows via
  `get_task`, so pulling the label off a ticket mid-flight does not abandon its
  branch or PR. ClickUp has no equivalent yet (`skip_tags` only).
- **Jira comments** use `/rest/api/2` (plain text); reads use `/rest/api/3` +
  `_flatten_adf` since v3 comments demand Atlassian Document Format for no benefit.
  Search uses `/rest/api/3/search/jql` with `nextPageToken`, not the deprecated
  `/search` with `startAt`.
- **Branch names** come from `vcs.branch_name(provider.branch_key(task), title)`.
  `TaskProvider.branch_key()` defaults to `task.key`; ClickUp overrides it to
  `CU-<native id>` because the integration only links a branch to a task when the
  id carries that prefix (a Custom Task ID is matched verbatim, so it is used
  as-is). `branch_name` slugs the key with `keep_case=True` — lowercasing it would
  break that matching, so `slug()`'s default lowercasing must not creep back in
  there.
- **ClickUp dependencies**: the API returns mirror rows for both directions. Disambiguate
  by `dep["task_id"] == this task` (meaning it waits on `depends_on`), not by `type`.
- **ClickUp state backend** is configurable: `custom_field` (atomic, filterable —
  recommended) or `tag` (global namespace). On `custom_field`, `set_ai_state` raises
  `RuntimeError` for any `AIState` with no entry in `state_field_options:` — new states
  (e.g. `planning`, `review`) need a ClickUp dropdown migration and a config update
  *before* they ship, not after; see the "Upgrading" section in `README.md`.
- `blocked_by()` failures must never stop work — `_advance` catches and treats the read
  as clear.

## Security posture

**Nothing reaches a ticket unredacted.** `redact()` (`runner/redact.py`) scrubs credential
material, and every path that can carry subprocess output outward goes through it:
`Pipeline._comment` / `_escalate` (the one funnel for provider comments), the `last_error`
writes, `vcs.run`'s `CommandError`, and the agent event log. It combines shape patterns
(`ghp_`, `github_pat_`, `sk-ant-`, `ATATT`, `pk_N_`, bearer headers, `user:pass@host` in a
remote URL) with exact-value matching against the tokens this runner actually injects —
`Pipeline._known_secrets()` gathers them from every vcs profile, and the agent adapter picks
them up by env var *name*, which is what catches a custom `token_env: GH_TOKEN_ACME`. Keep
the patterns narrow: a greedy rule eats branch names and SHAs, and an unreadable escalation
comment just sends the human off to run the failing command by hand.

Agent runs use `permission_mode: dontAsk` with an explicit `allowed_tools` allowlist;
`bypassPermissions` is only acceptable inside a disposable container. Worktrees live
under `runner.work_dir`, never in the developer's own checkout. Secrets come from env
vars referenced as `${VAR}` in `config.yaml` — never inline, and `config.yaml` itself is
not committed (`config.example.yaml` is the template).

### Dashboard

`runner/web/` is a read-only HTTP view over the same SQLite file and log
directory the cron tick writes. It is a separate process, started by
`ai-task-runner serve`; nothing was added to a tick, and the runner does not
know the dashboard exists.

Invariants, each easy to undo by accident:

- **`db.Reader` opens SQLite with `mode=ro`.** Read-only is enforced at the
  storage layer, not by convention, so a convenience helper cannot quietly gain
  a write path. `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` also means
  a missing database surfaces as a generic `OperationalError`; `Reader.__init__`
  turns that into `FileNotFoundError` when the path truly doesn't exist, which
  is what lets `_serve` tell "no database yet" apart from "database is broken".
- **`Store.__init__` sets `PRAGMA journal_mode=WAL`.** This is what makes the
  read-only reader and the writing tick safe to run at once: without WAL, a
  dashboard query issued while a tick holds the write lock hits `SQLITE_BUSY`
  instead of an answer. It has nothing to do with the dashboard's own code —
  it is set on the *writer* side, in `state.py`, so it is the invariant most
  likely to be deleted by someone tidying `Store.__init__` who has never had
  reason to open `runner/web/`.
- **`_serve` probes before it binds a port.** It opens a `Reader` and runs
  `SELECT 1 FROM tasks`, and refuses to start on `FileNotFoundError` (missing
  db) or `sqlite3.Error` (a directory, a corrupt file, the wrong schema).
  `db.exists()` was not enough — true for a directory too — and a dashboard
  that bound the port anyway would render an all-zeros board indistinguishable
  from a runner that ran and genuinely found nothing to do, which is exactly
  the ambiguity this dashboard exists to remove.
- **`cli.py` must never import `runner.web` at module level.** FastAPI and
  uvicorn are the optional `.[web]` extra; the cron entrypoint (`run`,
  `daemon`) keeps starting on `requests` + `pyyaml` alone. `_serve` imports
  them lazily and turns `ImportError` into an install hint instead of crashing
  every other subcommand on an unrelated missing dependency.
- **Everything egressing goes through `redact()`.** `db.Reader._row` scrubs
  `last_error`, `detail` and `title` on every row; `web/logs.py::tail` scrubs
  agent log lines the same way. Both take the exact secret values gathered
  from `vcs_profiles` by env var name (`web/api.py::_secrets`), matching how
  `Pipeline._known_secrets` does it for provider comments — a web endpoint is
  an outward path exactly like a ticket comment, so it gets the same treatment
  as a second layer on top of the redaction already done at log-write time.
- **Row ids travel as query parameters, never path segments.** `/api/task` and
  `/api/task/log` take `id` as a query param because ids contain `#`
  (`"{source}:{provider_task_id}#{repo_key}"`); a browser would otherwise
  strip everything from `#` onward before the request left the tab, and a
  routed row's id would collapse onto its pre-triage placeholder.
- **Day buckets use local midnight**, `date(ts,'unixepoch','localtime')` in
  `db.py`, matching `pipeline._midnight_epoch`. The two are duplicated rather
  than shared by import — importing `pipeline` would pull providers, the agent
  adapter and `requests` into a process whose only job is to read a file — so
  a test pins them to the same definition. `ts / 86400` would bucket on UTC
  and disagree with the cap the runner actually enforced. `db.py`'s
  `daily_budget_usd` default (20) is kept equal to
  `pipeline._limits_exceeded`'s own default for the same reason: with the key
  unset in config, the pipeline still enforces a $20 cap, and a dashboard
  showing $0 there would disagree with the gate it exists to report on.

`_record_transition()` — not `_set()` — is the single funnel for the `events`
ledger's `kind = "state"` rows. `grep -n "self\.store\.set_state" runner/pipeline.py`
finds five call sites (a plain `grep -n "store.set_state"` also matches
`_record_transition`'s own docstring, which mentions the string — a sixth,
spurious hit), and `_record_transition` runs immediately before
`store.set_state` at every one of them: one is inside `_set()` (the general
case, which also does `graph.check()` and pushes remote state), one is inside
`_escalate()` (the transition to `blocked`), and three write state directly,
bypassing both `_set()` and `_escalate()` — the crash-retry back to `queued`,
the start of `in-progress` execution, and the transition to `pr-open`. Without
`_record_transition` at those three sites, the ledger would be blind to them.
Before this ledger existed, `events` held agent runs only, with no record of
when a row actually changed state; `db.py::throughput_by_day` and
`cycle_times` read these rows to reconstruct throughput and cycle time,
splitting each `detail` string on `->`.

The graph tab does not render a diagram. `/api/graph` returns
`{"transitions": [...], "mermaid": "..."}`, both derived from the same
`graph.TRANSITIONS` object that backs `ai-task-runner graph`, so neither the
table nor the pasted-out text can drift from the runtime's own rules. The
frontend (`GraphTab.tsx`) renders `transitions` as a plain table and tucks the
`mermaid` string in a `<details>` for copy-out elsewhere. A real
diagram-layout engine was tried and dropped: `mermaid` pulled in `elk`,
`cytoscape` and `katex` plus chunks for a dozen diagram types this dashboard
never renders, taking the committed bundle to 5.9 MB across roughly 100 files
for an 11-state diagram, and brought `mermaid`'s own `chevrotain`/`lodash-es`
`npm audit` advisories along with it. The bundle is back to 896 KB across 10
files with 0 vulnerabilities. Do not reintroduce a diagram library for this —
an 11-state table earns its keep as a table.

Agent log filenames come from `models.agent_log_name(task_key, repo_key,
station)`. The repo key is load-bearing: `_EventLog` opens with `"w"`, so a
cross-repo ticket without it had two rows truncating each other's evidence.
`web/logs.py` looks for the current name first and falls back to the
pre-per-repo `{task_key}-{station}.log` name, so a row that was mid-flight
when per-repo names shipped still has a readable log instead of an empty tab.

`runner/web/static/` is committed build output, not source. `web/vite.config.ts`
points `build.outDir` at it directly rather than the default `web/dist`,
because `.gitignore` has an unanchored `dist/` that would otherwise swallow
the built assets the wheel needs (`pyproject.toml`'s
`[tool.setuptools.package-data]` entry for `runner.web` is what actually ships
them). `runner/web/static/assets` only exists after `npm run build` has run at
least once, and `api.py` guards on that directory rather than on `STATIC`
itself (which always exists in the source tree): mounting `StaticFiles`
against a directory that isn't there yet raises at startup. A fresh checkout
with no frontend build is a supported state — `serve` still answers the JSON
routes, and with no `assets/` directory it never registers the SPA catch-all
at all, so an unbuilt dashboard's non-API paths get a plain 404, not a 500.
The 500 is for the narrower, unlikely case where `assets/` exists but
`index.html` was removed out from under it — the catch-all route notices that
specifically and names the missing build rather than serving a blank page.
