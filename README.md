# ai-task-runner

A resumable bridge between a task manager and a coding agent. Ships with
ClickUp + Claude Code; swap either by implementing one small interface.

## Why it is built this way

Every tick advances each ticket **one step and exits**. Nothing blocks waiting
for CI or a human. That means a laptop closing, a VPN dropping or a crashed run
costs you at most one step, not a whole ticket.

## State machine

The transition table lives in `runner/graph.py` as data, not as `if` branches.
Illegal transitions raise instead of corrupting state, and this diagram is
generated from the same object the runtime uses, so it cannot drift:

```bash
ai-task-runner graph        # paste into Confluence / ClickUp docs
```

```mermaid
stateDiagram-v2
    [*] --> new: TO DO + assigned to me
    NEW --> NEEDS_INPUT: triage: ambiguous
    NEW --> HUMAN_ONLY: triage: unsafe or needs a human
    NEW --> PLANNING: triage: executable, scope needs sign-off
    NEW --> QUEUED: triage: executable, below approval gate
    NEW --> BLOCKED: triage failed
    NEEDS_INPUT --> NEW: human answered — re-triage
    NEEDS_INPUT --> BLOCKED: orphaned or errored
    AWAITING_PLAN_OK --> QUEUED: plan approved
    AWAITING_PLAN_OK --> NEW: plan rejected — re-triage
    AWAITING_PLAN_OK --> BLOCKED: orphaned or errored
    PLANNING --> AWAITING_PLAN_OK: grounded plan posted
    PLANNING --> NEEDS_INPUT: analyst read the code and found it ambiguous
    PLANNING --> PLANNING: analyst run died — retry
    PLANNING --> BLOCKED: analyst failed repeatedly
    QUEUED --> IN_PROGRESS: slot free, in execute window
    QUEUED --> BLOCKED: no repo mapped
    IN_PROGRESS --> REVIEW: branch pushed, awaiting review
    IN_PROGRESS --> QUEUED: run crashed — retry
    IN_PROGRESS --> BLOCKED: agent failed or produced nothing
    REVIEW --> PR_OPEN: reviewer approved
    REVIEW --> QUEUED: reviewer requested changes
    REVIEW --> REVIEW: reviewer run died — retry
    REVIEW --> BLOCKED: rejected, cycles exhausted, or reviewer failed
    PR_OPEN --> PR_OPEN: pushed fixes for CI or review
    PR_OPEN --> DONE: PR merged
    PR_OPEN --> BLOCKED: PR closed, or out of fix attempts
    DONE --> [*]
    note right of NEEDS_INPUT: waiting on a human
    note right of BLOCKED: waiting on a human
    note right of HUMAN_ONLY: waiting on a human
    note right of AWAITING_PLAN_OK: waiting on a human
```

| State | Meaning |
|---|---|
| `new` | never triaged |
| `needs-input` | questions posted, waiting on a human answer |
| `human-only` | off-limits for the agent; a human does this one |
| `planning` | the analyst is reading the repo to ground a plan |
| `plan-review` | plan posted, waiting for a human OK |
| `queued` | approved, waiting for a free execution slot |
| `in-progress` | the agent is (or was) actively working in a worktree |
| `review` | branch pushed, an independent reviewer pass is judging it |
| `pr-open` | PR exists, polling CI and human review |
| `blocked` | escalated; needs a human |
| `done` | PR merged |

`planning` and `review` are the two stations added on top of the original
triage → execute → PR loop: `planning` runs a read-only analyst against the
repo before a human signs off on the plan, and `review` runs an independent
pass over the pushed branch before a PR ever opens.

### Scheduling constraints

* **One agent per repo.** Two agents branching from the same base produce
  conflicting PRs. File-overlap detection sounds better but you don't know which
  files a ticket touches until after the agent has run, so the coarse rule is the
  useful one.
* **Blockers are honoured.** ClickUp task dependencies and Jira "is blocked by"
  links are read natively; a ticket with open upstream work is deferred with a
  comment rather than started.

## Repo brain

Every repo gets a durable notes file — plain markdown at
`{runner.brain_dir}/{source}__{repo}.md` — holding the things a run learns
that would otherwise be relearned every tick: which env var the tests need,
which review finding keeps recurring. It is injected into the planner,
implementer and reviewer prompts as background context.

It is human-editable by design: open it, correct it, delete it. Nothing
treats it as anything other than a file. It is appended to only when the
independent review pass **approves** the change, before the PR is even
opened — a rejected, changes-requested, or crashed run writes nothing — and
appends are capped at `limits.brain_max_bytes`. At the cap, appends are
refused and logged rather than silently evicting older notes; prune it by
hand when that happens.

## Config keys added by the station release

| Key | Meaning |
|---|---|
| `runner.brain_dir` | where per-repo brain files live (see above) |
| `limits.brain_max_bytes` | per-repo brain file size cap |
| `limits.max_review_cycles` | implementer ↔ reviewer round trips before giving up and asking a human; separate from `max_fix_attempts`, which covers crashes and CI |
| `agent.reviewer_model` | model for the independent review pass; falls back to `agent.model` |
| `agent.planner_tools` | tools available to the analyst station; read-only by construction, which is what makes planning safe in `mode: shadow` |
| `agent.reviewer_tools` | tools available to the reviewer station; the repo's own `test` command is appended at call time |

## Multiple workspaces, providers and GitHub accounts

`sources` is the top-level unit: one task-manager workspace, its own credentials
and statuses, and the repos that belong to it. Mix ClickUp and Jira freely; each
source is ticked independently and a broken one never stops the others.

Repos reference a named `vcs_profile` for git/GitHub identity. Two mechanisms,
both parallel-safe:

| Mechanism | Config | When |
|---|---|---|
| PAT per account | `token_env: GH_TOKEN_ACME` | Simplest. Preferred. |
| Separate `gh` tree | `gh_config_dir: ~/.config/gh-acme` | You want `gh` interactively too |

Set up a second `gh` tree once:

```bash
GH_CONFIG_DIR=~/.config/gh-acme gh auth login
```

Do **not** use `gh auth switch` — it mutates global state and races between runs.
The profile also sets `GIT_SSH_COMMAND` and `GIT_AUTHOR_*`, so the right key is
used for push and commits are attributed to the right identity, regardless of
your global git config.

## Reassignment and multiple laptops

Each developer's SQLite is local, so the AI State field is the only shared state.
If a runner sees a ticket marked in-flight that it has no local record of — a
reassignment, or a second laptop — it does **not** adopt it. The branch, worktree
and PR live elsewhere. It marks the ticket `Blocked`, explains the handover in a
comment, and stops. Clearing the field re-triages from scratch.

## Install

```bash
pipx install -e .          # or: pip install -e .
cp config.example.yaml config.yaml
export CLICKUP_TOKEN=pk_xxx GH_TOKEN_WORK=ghp_xxx
gh auth login              # GitHub CLI, used for all PR operations
claude auth status         # each dev uses their own Claude login
ai-task-runner doctor      # checks every source, profile and repo
```

## Run

```bash
ai-task-runner run --dry-run --force        # see what it would do, right now
ai-task-runner run                          # one tick, respects time windows
ai-task-runner run --source acme            # limit to one workspace
ai-task-runner status                       # what is in flight, across sources
python3 tests_smoke.py                      # offline state-machine tests
```

## Schedule

One job every 15 minutes. Phase gating lives in `config.yaml`, not in cron.

**macOS / Linux**
```cron
*/15 * * * * cd /opt/ai-task-runner && /usr/local/bin/ai-task-runner run >> /var/log/ai-runner.log 2>&1
```

**Windows**
```powershell
schtasks /create /tn "ai-task-runner" /sc minute /mo 15 ^
  /tr "C:\Python312\Scripts\ai-task-runner.exe run -c C:\ai-runner\config.yaml"
```

**Anywhere** — if cron is awkward (laptop sleeps, Windows quirks):
```bash
ai-task-runner daemon --interval 900
```

## Rollout

1. **Weeks 1–2** — `mode: shadow`. Triage and comments only, no code. Measure how
   often engineers agree with the triage decision. If agreement is under ~80%,
   fix the prompt and the `ready` definition before going further.
2. **Week 3** — `mode: execute`, `max_active_tasks: 1`, `require_plan_approval_from_scope: xs`
   (everything needs a plan OK). One volunteer repo.
3. **Week 4+** — relax the approval gate to `m`, raise concurrency.

## Kill switch

Set `runner.mode: shadow`, or add the `no-ai` tag to any individual ticket.

## Upgrading

The station release (analyst + reviewer) added two `AIState` values, `planning`
and `review`. Auditing the ClickUp mapping for them also turned up a third,
pre-existing gap: `new` was never in `state_field_options:` either, even
though a rejected plan sends a ticket back to `new` via `set_ai_state`. On the
`custom_field` backend, `set_ai_state` raises the moment a ticket reaches a
state with no mapped dropdown option, so all three are hard failures, not
cosmetic gaps.

Before upgrading a ClickUp source on `custom_field`:

1. Create ClickUp dropdown options for `new`, `planning` and `review` on the
   AI State custom field.
2. Add all three to that source's `state_field_options:` in `config.yaml`.
3. Run `ai-task-runner doctor` and confirm no `RuntimeError` surfaces from
   the ClickUp provider.

Jira is unaffected — state lives in an issue entity property that accepts
any value, and only the four human-actionable states are mirrored to labels.

## Adding a provider or agent

Implement `runner/providers/base.py::TaskProvider` (7 methods) and register it in
`runner/providers/__init__.py`, or implement `runner/agents/base.py::CodingAgent`
(2 methods). Nothing in `pipeline.py` knows about ClickUp, Jira or Claude Code.

### Jira specifics

State lives in an **issue entity property** (`ai-runner-state`) — no custom field,
no Jira admin, no screen configuration, atomic writes. Only the four states a
human must act on are mirrored to labels (`ai:needs-input`, `ai:human-only`,
`ai:plan-review`, `ai:blocked`), so boards stay readable.

One JQL trap the provider handles for you: `labels NOT IN ("no-ai")` silently
drops issues that have **no labels at all**. The `IS EMPTY` branch is required.

## Security notes

- `permission_mode: dontAsk` with an explicit `allowed_tools` allowlist, never
  `bypassPermissions` outside a disposable container.
- Worktrees live under `work_dir`, never in the developer's own checkout.
- Keep `.env` files out of the worktree; inject test config through the `setup`
  command instead.
- The denylist in `runner/triage.py` overrides the model. Nothing touching prod
  data, migrations, auth, payments, secrets or infra reaches the agent.
