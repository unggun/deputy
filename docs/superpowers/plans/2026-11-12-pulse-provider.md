# Pulse Task Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deputy can take tickets from Pulse Task Spaces exactly as it does from ClickUp and Jira: tasks assigned to me in the ready status, triaged, planned, built and shipped, with Deputy's state shown on the task.

**Architecture:** A new `TaskProvider`, `runner/providers/pulse.py`, talks to Pulse's existing MCP server (`POST /mcp`) with plain JSON-RPC over `requests`.
- No LLM is involved and there is no MCP SDK. Each call is one `tools/call` POST, and Pulse answers with plain JSON (the server is stateless, `enableJsonResponse: true`).
- Authentication is a bearer token in an env var.
- **Container = Pulse Space.** A Space can hold work for several repos; `repos_for_container` returning several repos already makes triage route each ticket.

**Tech Stack:** Python 3.10+, `requests` (already a dependency), stdlib `html.parser`.

**Pulse side:** `Virtual-Office/docs/tickets/personal-access-tokens/task.md` adds:
- personal access tokens (`pls_pat_…`)
- the `get_me` tool
- `tags` and `url` on task results

**This plan does not wait for that ticket.** Tasks 1–6 run against an OAuth access token copied from an existing MCP connection (it lasts an hour, which is enough for development). Task 7 is the cut-over once the ticket ships.

## Decisions (from the 12 Nov 2026 design review)

| # | Decision |
|---|---|
| D1 | Transport is the existing MCP server, not a new REST API. |
| D2 | The credential is a Pulse **personal access token** in `${PULSE_TOKEN}`. |
| D3 | Deputy acts **as the token's owner**. Its comments and changes show the owner's name with Pulse's "via AI" chip. |
| D4 | Deputy's state is a **global dropdown custom field** in Pulse (Settings → Spaces), e.g. "AI State". Field id and option ids live in config, the same shape as the ClickUp source. |
| D5 | Container = **Space**, matched by `spaceId` (`containers`) or Space name (`container_names`). |
| D6 | Comments are markdown with a **visible footer** as the bot marker, never `<!-- … -->`. Pulse renders a non-HTML body with its markdown renderer; an HTML comment would push it onto the HTML path. |
| D7 | `branch_key` = the Pulse short id (`PLS-1234`). Pulse's GitHub App links branches, commits and PRs by that id, so the default `task.key` is correct and no override is needed. |
| D8 | A **401** from Pulse fails the whole source for that tick. Nothing is retried, no row is touched, and "Pulse token rejected" shows in `deputy status`. |
| D9 | Rate limit: Pulse allows 100 MCP calls/min per user, shared with the owner's own AI assistants. A tick must stay well under it. |

## Global Constraints

- **The token never leaves memory.** It is not logged, written to the SQLite store, or put in a ticket or agent prompt. `redact.py` must mask `pls_` tokens (Task 5).
- **`pipeline.py` does not change** except where Task 5 says so. Nothing outside `providers/pulse.py` knows it is Pulse.
- **`dry_run` writes nothing to Pulse.** Each write logs what it would have sent, as `clickup.py` does.
- Tests follow `tests_smoke.py`'s existing style: `check(label, ok)` and a stubbed `_call`, no network.

## MCP facts the provider relies on

- **Request:** `POST {base_url}/mcp` with headers:
  - `Authorization: Bearer <token>`
  - `Content-Type: application/json`
  - `Accept: application/json, text/event-stream`

  Body: `{"jsonrpc":"2.0","id":N,"method":"tools/call","params":{"name":…,"arguments":{…}}}`. **Batches are refused.**
- **Success:** `result.content[0].text` is a JSON string of the payload.
- **Tool failure** (not found, permission, validation): HTTP 200 with `result.isError: true` and `result.content[0].text` holding a sentence. An unreadable task is reported as *not found*, never "forbidden".
- **Auth failure:** HTTP 401 with a JSON body that carries a `message`. Messages include "Unknown access token.", "This connection was revoked in Pulse.", "…expired on {date}…" and "…no longer active."
- **Rate limit:** HTTP 429.
- **Tools used:**

  | Tool | Arguments | Payload |
  |---|---|---|
  | `get_me` | — | `{id, name, email}` (ticket item 6) |
  | `list_spaces` | — | Spaces with `id`, `name` |
  | `search_tasks` | `assigneeId, status, spaceId?, page, limit≤100` | `{tasks, total, page, limit}` |
  | `get_task` | `id` (short id or UUID) | adds `description`, `updatedAt`, `blockedBy[]`, `links[]`, `customFields[]` |
  | `list_comments` | `taskId` | comments with `id, body, author{id,name}, createdAt, via` |
  | `add_comment` | `taskId, body` | the comment |
  | `update_task` | `id, status?, customFields?{fieldId: optionId}` | the task |

  Task summaries carry `id, shortId, title, status{name,isCompleted}, listId, spaceId, assignees[]`, plus `tags[]` and `url` once ticket item 6b ships.

---

### Task 1: Client core, `_call` and error mapping

**Files:** create `runner/providers/pulse.py`.

- [x] Constructor `PulseProvider(cfg, bot_marker, dry_run=False)`, matching the signature `providers.build` calls. Read from `cfg`:
  - `base_url`: API origin, no trailing slash
  - `web_url`: frontend origin, used only until `url` ships on task results
  - `token`
  - `spaces`: optional allowlist of Space ids
  - `ready_status`, `in_progress_status`, `review_status`, `done_status`
  - `skip_tags`
  - `state_field_id`, `state_field_options`

  Raise `RuntimeError` at construction if `token`, `base_url`, `state_field_id` or any `AIState` value in `state_field_options` is missing. The message names the key; it never includes the value.
- [x] `requests.Session` with the three headers, timeout 30s.
- [x] Define the two exceptions at module level:

```python
class PulseAuthError(RuntimeError):
    """The token was refused. Fail the whole source; never retry (D8)."""

class PulseToolError(RuntimeError):
    """A tool ran and said no (isError). The message is Pulse's own sentence."""
```

- [x] `_call(self, tool: str, **arguments) -> Any`:
  - Drop `None` arguments.
  - POST.
  - Status 401 → `PulseAuthError(f"Pulse token rejected: {message}")`, with `message` read from the JSON body if it has one, else the first 200 chars.
  - Status 429 → `RuntimeError("Pulse rate limit (100/min) hit")`.
  - Any other status ≥400 → `RuntimeError(f"Pulse {tool} -> {status}: {text[:400]}")`.
  - A JSON-RPC `error` object → `RuntimeError`.
  - `result.isError` → `PulseToolError(text)`.
  - Otherwise `json.loads(result["content"][0]["text"])`.
- [x] A monotonically increasing `id` per call.
- [x] Never put the token in an exception, a log line or `repr`. Give the class a `__repr__` that omits it.

### Task 2: Reads

- [x] `whoami()`: `_call("get_me")["id"]`, cached on the instance.
  - **Before ticket item 6 ships**, fall back to an optional `cfg["user_id"]` with a `log.warning`.
  - Remove the fallback in Task 7.
- [x] `_space_names()`: `list_spaces` once per provider instance (one call per tick), returning a `{id: name}` dict.
- [x] `fetch_candidates()`:
  - Page `search_tasks(assigneeId=whoami(), status=ready_status, spaceId=<each allowlisted space, or omitted>, limit=100, page=n)` until `page*limit >= total`.
  - Keep rows where `status.isCompleted` is false and no `skip_tags` tag is present (tags compared case-insensitively; an empty list until 6b ships).
  - Map each row with `_to_task`.
- [x] `get_task(task_id)`: `_call("get_task", id=task_id)` → `_to_task`. A `PulseToolError` "not found" propagates; the pipeline already treats a vanished ticket as orphaned.
- [x] `_to_task(t)`:

  | `Task` field | Source |
  |---|---|
  | `id` | `t["id"]` (UUID) |
  | `key` | `t["shortId"] or t["id"]` |
  | `title` | `t["title"]` |
  | `description` | `_html_to_text(t.get("description") or "")` |
  | `status` | `(t["status"] or {}).get("name","").lower()` |
  | `url` | `t.get("url")`, or `f"{web_url}/operational/workspace/{spaceId}/lists/{listId}/tasks/{id}"` |
  | `list_id` | `t["spaceId"]` (D5: the container is the Space) |
  | `list_name` | `_space_names().get(spaceId, spaceId)` |
  | `tags` | `t.get("tags", [])` |
  | `assignee_ids` | `[a["id"] for a in t["assignees"]]` |
  | `updated_at` | epoch ms of `t.get("updatedAt")`, or 0 on a summary row |
  | `ai_state` | `_read_state(t)` |
  | `raw` | `t` |

  Search rows carry no `customFields` and no `updatedAt`. Check whether `pipeline.py` reads `ai_state` or `updated_at` from `fetch_candidates()` results:
  - If it does, call `get_task` per candidate instead of mapping the summary. That costs one call per new candidate per tick (D9).
  - If it does not, the summary is enough.

  Record which one you found in the module docstring.
- [x] `_read_state(t)`: find `customFields[*]` with `id == state_field_id`. Its `value` is an option id (or `{id,label}`; handle both), reverse-mapped through `state_field_options`. Missing or unknown → `AIState.NEW`.
- [x] `list_comments(task_id)`: page through `list_comments`.
  - Skip `isDeleted`.
  - `text = _html_to_text(body)` when the body looks like HTML (starts with `<`), else the raw body. Pulse UI comments are HTML; Deputy's own are markdown.
  - `is_bot = self.footer in text or self.marker in text`. Do **not** use `via == "mcp"`: the owner's own Claude sessions set it too (D6).
  - Sort by `createdAt`.
- [x] `blocked_by(task_id)`:
  - From `get_task`: `links` where `relationship == "blocked_by"`, giving `{"key": shortId or taskId, "closed": <bool>}`.
  - `closed` needs each blocker's status: call `get_task` per blocker. Treat a `restricted` link or a not-found blocker as **open**, since it can't be shown to be done.
  - Cap at 10 blockers per task, with a warning beyond that.
- [x] `_html_to_text(html)`: stdlib `HTMLParser` that keeps text, turns `<br>` / `</p>` / `</li>` into newlines, prefixes `<li>` with `- `, and unescapes entities. No new dependency.

### Task 3: Writes

- [x] `self.footer`: same rule as `clickup.py`. Use `FOOTER_DEFAULT` when `bot_marker` starts with `<!--`, else the marker itself. Import `FOOTER_DEFAULT` from `providers/clickup_format.py` (or wherever it lives) rather than copying the string.
- [x] `add_comment(task_id, markdown)`:
  - `body = f"{markdown.rstrip()}\n\n_{self.footer}_"`.
  - In dry run, log and return `"dry-run"`.
  - Otherwise return `_call("add_comment", taskId=task_id, body=body)["id"]`.
  - Never send `<!-- -->` (D6). Strip any HTML comment from `markdown` and log a warning if one is found.
- [x] `set_ai_state(task_id, state)`:
  - `option = state_field_options[state.value]`. A missing option → `RuntimeError`, as ClickUp does.
  - Then `_call("update_task", id=task_id, customFields={state_field_id: option})`.
- [x] `set_status(task_id, status)`: `_call("update_task", id=task_id, status=<configured name>)`.
  - Pulse matches status names per Space, case-insensitively.
  - A Space with no status by that name gives `PulseToolError`. Let it propagate, so the pipeline escalates.
- [x] **Do not catch `PulseAuthError` anywhere in the provider.**

### Task 4: Register and doctor

**Files:** `runner/providers/__init__.py`, `runner/cli.py`, `config.example.yaml`.

- [x] Add `"pulse": PulseProvider` to `REGISTRY` and `__all__`.
- [x] Generalise `_clickup_state_gaps` in `cli.py:59` into a check that also runs for `provider_type == "pulse"`: every `AIState` value has a `state_field_options` entry.
- [x] Add a Pulse doctor check: one `get_me` call, printing "Pulse: acting as {name} <{email}>".
  - A 401 prints the refusal message and fails the doctor.
  - The token is never printed.
- [x] Add a commented Pulse source to `config.example.yaml`:

```yaml
  # Pulse Task Spaces, over Pulse's MCP server. Create the token in
  # Pulse → User Settings → AI Clients → Personal access tokens (Read and write).
  # Create a global dropdown "AI State" in Settings → Spaces with one option per
  # state below, then paste the field and option ids.
  - key: pulse
    provider:
      type: pulse
      base_url: https://api.pulse.example.com    # API origin; /mcp is appended
      web_url: https://pulse.example.com          # only used until task results carry `url`
      token: ${PULSE_TOKEN}
      # spaces: ["<space-uuid>"]                  # optional; omit to search every Space you can see
      ready_status: "To Do"
      in_progress_status: "In Progress"
      review_status: "Code Review"
      done_status: "Done"
      skip_tags: ["no-ai"]
      state_field_id: "<custom-field-uuid>"
      state_field_options:
        new: "<option-id>"
        needs-input: "<option-id>"
        human-only: "<option-id>"
        plan-review: "<option-id>"
        planning: "<option-id>"
        queued: "<option-id>"
        in-progress: "<option-id>"
        review: "<option-id>"
        pr-open: "<option-id>"
        blocked: "<option-id>"
        done: "<option-id>"
    repos:
      - key: virtual-office
        containers: ["<space-uuid>"]        # Pulse Space ids (D5); or container_names: ["Pulse"]
        vcs_profile: work
        path: ~/code/Virtual-Office
        remote: git@github.com:Digital-Envision/Virtual-Office.git
        base_branch: main
```

### Task 5: Auth failure surfaces in `deputy status` (D8)

**Files:** `runner/cli.py`, `runner/state.py`, `runner/redact.py`.

- [x] In `_tick_all`, catch `PulseAuthError` **before** the generic `except Exception`:
  - log it at ERROR without a traceback;
  - record it for the source;
  - `continue`. Every other source still runs.

  Import the exception lazily, or define a provider-agnostic `ProviderAuthError` in `providers/base.py` that `PulseAuthError` subclasses. **Prefer the base class**, so `cli.py` does not import a specific provider.
- [x] Record it: check whether `events.row_id` is a foreign key to `tasks`.
  - If it is not, write an `events` row with `row_id = f"{source}:*"` and kind `source_auth_failed`.
  - If it is, add a `source_health(source TEXT PRIMARY KEY, last_error TEXT, at INTEGER)` table, following the `CREATE TABLE IF NOT EXISTS` note at `state.py:181`.

  Clear the record on the next successful tick for that source.
- [x] `deputy status` prints a line per source with a current auth failure: `pulse: Pulse token rejected: This personal access token expired on 9 February 2027. Create a new one in Settings → AI Clients.`
- [x] `redact.py`: add a pattern for `pls_(at|rt|pat)_[A-Za-z0-9_-]{20,}` with a smoke test, next to the existing ClickUp `pk_` handling.

### Task 6: Smoke tests

**File:** `tests_smoke.py`, in a new `# --- pulse provider ---` block, using the file's own `check()` and the `_cu._req = lambda …` stubbing style from line ~2718. Stub `PulseProvider._call` (or `session.post` for the error-mapping cases).

- [x] `_call` maps:
  - 401 → `PulseAuthError`, with the server message and without the token;
  - 429 → `RuntimeError`;
  - `isError` → `PulseToolError`;
  - success → the parsed payload.
- [x] `fetch_candidates` pages until `total`, drops completed and `skip_tags` rows, and passes `assigneeId = whoami()` and the ready status.
- [x] `_to_task` maps `shortId` → `key`, `spaceId` → `list_id`, the Space name → `list_name`, and lowercases the status.
- [x] `_read_state` round-trips every `AIState` through `state_field_options`; an unknown option gives `NEW`.
- [x] `add_comment` appends the visible footer, never sends `<!--`, and sends nothing in dry run.
- [x] `list_comments` flags Deputy's own comment as bot by its footer, and does NOT flag a human comment with `via: "mcp"`.
- [x] `_html_to_text` handles `<p>`, `<br>`, `<li>` and `&amp;`.
- [x] `blocked_by` treats restricted and not-found blockers as open.
- [x] `branch_key` returns `PLS-1234` (D7), and `vcs.branch_name` builds a name Pulse's `TASK_ID_PATTERN` would match.
- [x] `_tick_all` with one source raising `PulseAuthError` and a second healthy source: the second still ticks, and the failure is recorded.
- [x] Constructor refuses a config missing an `AIState` option, and the error names the key only.
- [x] `redact` masks `pls_pat_…`.

Run with `python tests_smoke.py`. The suite counts call sites against `Pipeline`'s source (README line ~568), so check that number still holds.

### Task 7: Cut over to a personal access token (after the Pulse ticket ships)

- [ ] In Pulse, create a token named "Deputy" with Read and write access, expiring in 90 days. Put it in the secrets file the scheduler reads as `PULSE_TOKEN` (README → Deploy on a VPS §7).
- [ ] Remove the `user_id` fallback from `whoami()`, and the `web_url` URL fallback if `url` now arrives on task results. Keep `web_url` optional in config for older Pulse deployments only if one exists; otherwise delete it.
- [ ] Run `deputy doctor` and check it shows "acting as" you. Then run `deputy run --source pulse --dry-run`, then a real run.
- [ ] README: add a "Pulse specifics" section under "Adding a provider or agent", covering:
  - D4 and D5
  - the 100/min shared limit
  - the token's 90-day expiry and Pulse's 7-day warning email
  - that rotating a token means create the new one, swap the env var, then revoke the old one
- [x] Update this repo's `CLAUDE.md` provider list (line ~20) to include `pulse.py`.

## Open items to settle while building

- **Task 2, resolved (12 Nov):** yes. `_triage_new` branches on `task.ai_state` and triage reads `task.description`, so `fetch_candidates` costs 1 + N (`search_tasks`, then `get_task` per candidate).
- **Task 5, resolved (12 Nov):** `events.task_id` has no FK, but a refused token is a fact about the source that should disappear once fixed, not ticket history. So it went into a new `source_health` table, cleared only after Pulse accepts the token again (`provider.authenticated`). A tick outside the working window makes no calls, so it does not clear it. Also: `pipeline.py`'s three per-row `except Exception` handlers (advance, triage, the `blocked_by` gate) now re-raise `ProviderAuthError`; otherwise a 401 would be written onto every row as `last_error`.
- **Task 4, simplified:** the Pulse constructor refuses a config missing any `AIState` option, and `doctor` already reports constructor errors per source. `_clickup_state_gaps` was not generalised.
- **Reassignment:** Pulse has multiple assignees per task, as ClickUp does. Check that README "Reassignment and multiple laptops" holds unchanged: a candidate is "assigned to me" if I am *among* the assignees.
