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
  analyst.py  review.py  brain.py  jsonio.py
  agents/    __init__.py  base.py (CodingAgent)   claude_code.py
  providers/ __init__.py  base.py (TaskProvider)  clickup.py  jira.py
tests_smoke.py  README.md  config.example.yaml
```

Note the two unrelated `base.py` files: `providers/base.py` defines `TaskProvider`
(7 methods + optional `blocked_by`), `agents/base.py` defines `CodingAgent`
(`reason` for tool-less classification, `work` for agentic runs). Confusing one for
the other imports cleanly and only fails at the consumer, so check which you have open.

Packaging is `pyproject.toml` at the root with setuptools flat-layout auto-discovery —
it picks up `runner` and its two sub-packages and excludes `tests_smoke.py` and
`config.example.yaml`. The console script is `ai-task-runner = runner.cli:main`, so
`pyproject.toml` must stay at the repo root, not inside `runner/`.

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
shared state. Row ids are namespaced `"{source.key}:{provider_task_id}"` so two
workspaces cannot collide in one DB — `_row_id()` builds them, `_pid()` unwraps them.

**Never adopt an orphan.** A ticket marked in-flight remotely with no local row means
another laptop or a reassignment; `_orphan()` marks it `blocked` with an explanatory
comment and stops. The branch, worktree and PR live elsewhere.

### Concurrency and safety invariants

- **One agent per repo** (`graph.repo_is_free`). Two agents branching off the same base
  produce conflicting PRs. File-overlap detection is a tempting refinement but you don't
  know which files a ticket touches until after the agent has run.
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
  and one turn, so **triage never reads the repo**; it judges `scope` from ticket text
  plus the `notes:` string in config. Uses `agent.triage_model`.
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
environment only (`_run()` inherits the parent env and passes no `env=`, so this scopes
cleanly). Add `CLAUDE_CODE_SIMPLE=1` so a missing key fails loudly instead of silently
falling back to the subscription.

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
- **Jira comments** use `/rest/api/2` (plain text); reads use `/rest/api/3` +
  `_flatten_adf` since v3 comments demand Atlassian Document Format for no benefit.
  Search uses `/rest/api/3/search/jql` with `nextPageToken`, not the deprecated
  `/search` with `startAt`.
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

Agent runs use `permission_mode: dontAsk` with an explicit `allowed_tools` allowlist;
`bypassPermissions` is only acceptable inside a disposable container. Worktrees live
under `runner.work_dir`, never in the developer's own checkout. Secrets come from env
vars referenced as `${VAR}` in `config.yaml` — never inline, and `config.yaml` itself is
not committed (`config.example.yaml` is the template).
