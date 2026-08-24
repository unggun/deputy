"""Offline smoke test: fake provider + fake agent, exercises the state machine."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

for k in ["CLICKUP_TOKEN", "CLICKUP_TOKEN_SIDE", "JIRA_EMAIL", "JIRA_TOKEN", "GH_TOKEN_WORK"]:
    os.environ.setdefault(k, "x")
sys.path.insert(0, str(Path(__file__).parent))

from runner.config import Config
from runner.models import AgentResult, AIState, Comment, Task
from runner.pipeline import Pipeline
from runner.providers.base import TaskProvider
from runner.state import Store


class FakeProvider(TaskProvider):
    def __init__(self, tasks):
        self.tasks = {t.id: t for t in tasks}
        self.comments = {t.id: [] for t in tasks}
        self.states = {}
        self.statuses = {}

    def whoami(self): return "me"
    def fetch_candidates(self): return list(self.tasks.values())
    def get_task(self, tid): return self.tasks[tid]
    def list_comments(self, tid): return self.comments[tid]

    def add_comment(self, tid, md):
        c = Comment(id=str(len(self.comments[tid]) + 1), author="ai", author_id="me",
                    text=md, created_at=len(self.comments[tid]), is_bot=True)
        self.comments[tid].append(c)
        return c.id

    def set_ai_state(self, tid, state):
        self.states[tid] = state
        self.tasks[tid].ai_state = state

    def set_status(self, tid, status): self.statuses[tid] = status

    def human_reply(self, tid, text):
        self.comments[tid].append(Comment(
            id=str(len(self.comments[tid]) + 1), author="dev", author_id="u1",
            text=text, created_at=len(self.comments[tid]), is_bot=False))


class FakeAgent:
    def __init__(self, decision, work_replies=None):
        self.decision = decision
        self.work_replies = list(work_replies or [])
    def reason(self, prompt, *, max_turns=1):
        import json
        return AgentResult(ok=True, cost_usd=0.01, text=json.dumps(self.decision))
    def work(self, prompt, cwd, *, log_path=None, model=None, allowed_tools=None):
        text = self.work_replies.pop(0) if self.work_replies else "did the thing"
        return AgentResult(ok=True, text=text, cost_usd=0.5)


def mk(tid, title, desc, list_id="901100000001"):
    return Task(id=tid, key=tid, title=title, description=desc, status="to do",
                url=f"http://x/{tid}", list_id=list_id, list_name="Backend",
                tags=[], assignee_ids=["me"], updated_at=1)


def run_case(name, task, decision, expect):
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config.load("config.example.yaml")
        cfg.data["runner"]["db_path"] = f"{tmp}/s.db"
        cfg.data["runner"]["work_dir"] = f"{tmp}/w"
        cfg.data["runner"]["log_dir"] = f"{tmp}/l"
        src = cfg.sources(only=["mycorp"])[0]
        prov = FakeProvider([task])
        pipe = Pipeline(cfg, src, prov, FakeAgent(decision), Store(f"{tmp}/s.db"))
        pipe.tick(dt.datetime(2026, 8, 10, 7, 0))
        got = prov.states.get(task.id)
        ok = got == expect
        print(f"  {'PASS' if ok else 'FAIL':<5} {name:<38} -> {got.value if got else None}")
        return ok, pipe, prov, cfg, tmp


print("state machine smoke tests")
results = []

results.append(run_case(
    "denylist beats the model",
    mk("T1", "Fix prod database migration", "run the migration on production data"),
    {"decision": "execute", "confidence": 0.99, "scope": "s", "plan": ["do it"]},
    AIState.HUMAN_ONLY)[0])

results.append(run_case(
    "vague ticket -> questions",
    mk("T2", "Improve the dashboard", "make it better"),
    {"decision": "clarify", "confidence": 0.4, "scope": "m",
     "questions": ["Which metrics?"]},
    AIState.NEEDS_INPUT)[0])

results.append(run_case(
    "low confidence downgraded to clarify",
    mk("T3", "Add pagination to /users", "add ?page and ?limit"),
    {"decision": "execute", "confidence": 0.5, "scope": "s", "plan": ["x"]},
    AIState.NEEDS_INPUT)[0])

results.append(run_case(
    "large scope downgraded to clarify",
    mk("T4", "Rewrite auth module", "all of it"),
    {"decision": "execute", "confidence": 0.9, "scope": "l", "plan": ["x"]},
    AIState.NEEDS_INPUT)[0])

results.append(run_case(
    "medium scope -> plan approval gate",
    mk("T5", "Add CSV export to reports", "button downloads a CSV of the current filter"),
    {"decision": "execute", "confidence": 0.9, "scope": "m", "plan": ["a", "b"]},
    AIState.PLANNING)[0])

results.append(run_case(
    "small scope, shadow mode still gates",
    mk("T6", "Fix typo in footer", "'Copyrigth' -> 'Copyright'"),
    {"decision": "execute", "confidence": 0.95, "scope": "xs", "plan": ["fix typo"]},
    AIState.PLANNING)[0])

# --- approval round trip + idempotency -------------------------------------
print("\napproval round trip")
with tempfile.TemporaryDirectory() as tmp:
    from runner import vcs as _vcs

    cfg = Config.load("config.example.yaml")
    cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w", log_dir=f"{tmp}/l",
                              brain_dir=f"{tmp}/b")
    src = cfg.sources(only=["mycorp"])[0]
    task = mk("T7", "Add CSV export", "button downloads a CSV")
    prov = FakeProvider([task])
    store = Store(f"{tmp}/s.db")
    agent = FakeAgent(
        {"decision": "execute", "confidence": 0.9, "scope": "m",
         "plan": ["a", "b"], "acceptance_criteria": ["csv downloads"]},
        work_replies=[json.dumps({"verdict": "ready", "plan": ["a", "b"],
                                  "acceptance_criteria": ["csv downloads"]})])
    pipe = Pipeline(cfg, src, prov, agent, store)
    when = dt.datetime(2026, 8, 10, 7, 0)

    # _plan checks out a real worktree for the analyst to read. This suite is
    # offline (fake provider, fake agent) — stub the checkout instead of
    # touching git. The worktree machinery itself is covered by the dedicated
    # "worktree re-creation" section below.
    _checkout = Path(tmp) / "checkout"
    _checkout.mkdir()
    _real_ensure_worktree = _vcs.ensure_worktree
    _vcs.ensure_worktree = lambda *a, **k: _checkout
    try:
        pipe.tick(when)   # triage: routes into planning
        pipe.tick(when)   # planning: analyst grounds the plan
        assert prov.states["T7"] == AIState.AWAITING_PLAN_OK
        n1 = len(prov.comments["T7"])

        pipe.tick(when)   # re-run: must not duplicate the plan comment
        dup_ok = len(prov.comments["T7"]) == n1
        print(f"  {'PASS' if dup_ok else 'FAIL':<5} re-run posts no duplicate comment")
        results.append(dup_ok)

        prov.human_reply("T7", "approved, go ahead")
        pipe.tick(when)
        q_ok = store.get("mycorp:T7")["state"] == AIState.QUEUED.value
        print(f"  {'PASS' if q_ok else 'FAIL':<5} 'approved' moves plan-review -> queued")
        results.append(q_ok)

        # shadow mode must never execute
        pipe.tick(when)
        ex_ok = store.get("mycorp:T7")["state"] == AIState.QUEUED.value
        print(f"  {'PASS' if ex_ok else 'FAIL':<5} shadow mode never executes")
        results.append(ex_ok)
    finally:
        _vcs.ensure_worktree = _real_ensure_worktree

# --- orphan detection -------------------------------------------------------
print("\nreassignment / orphan handling")
with tempfile.TemporaryDirectory() as tmp:
    cfg = Config.load("config.example.yaml")
    cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w", log_dir=f"{tmp}/l")
    src = cfg.sources(only=["mycorp"])[0]
    task = mk("T8", "Half-done ticket", "started on another laptop")
    task.ai_state = AIState.PR_OPEN
    prov = FakeProvider([task])
    pipe = Pipeline(cfg, src, prov, FakeAgent({}), Store(f"{tmp}/s.db"))
    pipe.tick(dt.datetime(2026, 8, 10, 7, 0))
    o_ok = prov.states["T8"] == AIState.BLOCKED
    print(f"  {'PASS' if o_ok else 'FAIL':<5} unknown in-flight ticket -> blocked, not adopted")
    results.append(o_ok)

# --- time windows -----------------------------------------------------------
print("\ntime windows")
with tempfile.TemporaryDirectory() as tmp:
    cfg = Config.load("config.example.yaml")
    cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w", log_dir=f"{tmp}/l")
    src = cfg.sources(only=["mycorp"])[0]
    prov = FakeProvider([mk("T9", "Anything", "x")])
    pipe = Pipeline(cfg, src, prov, FakeAgent(
        {"decision": "clarify", "confidence": 0.3, "scope": "s", "questions": ["?"]}),
        Store(f"{tmp}/s.db"))
    pipe.tick(dt.datetime(2026, 8, 10, 22, 0))       # Monday night
    w1 = "T9" not in prov.states
    pipe.tick(dt.datetime(2026, 8, 9, 7, 0))         # Sunday morning
    w2 = "T9" not in prov.states
    pipe.tick(dt.datetime(2026, 8, 10, 7, 0))        # Monday triage window
    w3 = prov.states.get("T9") == AIState.NEEDS_INPUT
    for label, ok in [("outside advance window: no-op", w1),
                      ("weekend: no-op", w2),
                      ("inside triage window: acts", w3)]:
        print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
        results.append(ok)

# --- graph structure --------------------------------------------------------
print("\nworkflow graph")
from runner import graph as G
g_ok = not G.validate_graph()
print(f"  {'PASS' if g_ok else 'FAIL':<5} transition table is structurally sound")
results.append(g_ok)

try:
    G.check(AIState.DONE, AIState.IN_PROGRESS)
    i_ok = False
except G.IllegalTransition:
    i_ok = True
print(f"  {'PASS' if i_ok else 'FAIL':<5} illegal transition raises")
results.append(i_ok)

rows = [{"task_id": "s:A", "task_key": "A", "repo_key": "pulse-backend",
         "state": AIState.IN_PROGRESS.value}]
r1 = not G.repo_is_free("pulse-backend", rows).ok
r2 = G.repo_is_free("pulse-frontend", rows).ok
print(f"  {'PASS' if r1 else 'FAIL':<5} busy repo blocks a second agent")
print(f"  {'PASS' if r2 else 'FAIL':<5} different repo is free")
results += [r1, r2]

# New states: planning is pre-approval, review is post-push.
n1 = AIState.PLANNING in G.TRANSITIONS[AIState.NEW]
n2 = AIState.AWAITING_PLAN_OK in G.TRANSITIONS[AIState.PLANNING]
n3 = AIState.NEEDS_INPUT in G.TRANSITIONS[AIState.PLANNING]
n4 = AIState.REVIEW in G.TRANSITIONS[AIState.IN_PROGRESS]
n5 = AIState.PR_OPEN in G.TRANSITIONS[AIState.REVIEW]
n6 = AIState.QUEUED in G.TRANSITIONS[AIState.REVIEW]
n7 = AIState.PR_OPEN not in G.TRANSITIONS[AIState.IN_PROGRESS]
n8 = not AIState.PLANNING.is_terminal and not AIState.REVIEW.is_terminal
n9 = AIState.AWAITING_PLAN_OK not in G.TRANSITIONS[AIState.NEW]
for label, ok in [("new -> planning exists", n1),
                  ("planning -> plan-review exists", n2),
                  ("planning -> needs-input exists", n3),
                  ("in-progress -> review exists", n4),
                  ("review -> pr-open exists", n5),
                  ("review -> queued exists (request_changes)", n6),
                  ("in-progress -> pr-open is gone", n7),
                  ("new states are not terminal", n8),
                  ("new -> plan-review is retired (planning owns the gate)", n9)]:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(ok)

# A task in review holds a slot but does not block its repo.
rev_rows = [{"task_id": "s:B", "task_key": "B", "repo_key": "pulse-backend",
             "state": AIState.REVIEW.value}]
r3 = G.repo_is_free("pulse-backend", rev_rows).ok
print(f"  {'PASS' if r3 else 'FAIL':<5} a task in review does not block its repo")
results.append(r3)

with tempfile.TemporaryDirectory() as _tmp:
    _st = Store(f"{_tmp}/a.db")
    _st.upsert("s:R", state=AIState.REVIEW.value)
    _st.upsert("s:P", state=AIState.PLANNING.value)
    a1 = _st.active_count() == 1
print(f"  {'PASS' if a1 else 'FAIL':<5} review counts as active, planning does not")
results.append(a1)

# A partial upsert (no explicit state=) must never resurrect or reset state —
# this is what silently broke the PLANNING -> AWAITING_PLAN_OK path before it
# was fixed: fields.setdefault("state", ...) ran even on updates.
with tempfile.TemporaryDirectory() as _tmp:
    _st2 = Store(f"{_tmp}/b.db")
    _st2.upsert("s:X", state=AIState.PR_OPEN.value)
    _st2.upsert("s:X", attempts=1)   # partial update: no state= passed
    u1 = _st2.get("s:X")["state"] == AIState.PR_OPEN.value
    _st2.upsert("s:Y", attempts=0)   # first-ever insert: no state= passed
    u2 = _st2.get("s:Y")["state"] == AIState.NEW.value
print(f"  {'PASS' if u1 else 'FAIL':<5} a partial upsert on an existing row leaves state untouched")
print(f"  {'PASS' if u2 else 'FAIL':<5} a first insert with no state still defaults to new")
results += [u1, u2]

b1 = not G.blockers_clear([{"key": "DEV-1", "closed": False}]).ok
b2 = G.blockers_clear([{"key": "DEV-1", "closed": True}]).ok
print(f"  {'PASS' if b1 else 'FAIL':<5} open blocker defers the ticket")
print(f"  {'PASS' if b2 else 'FAIL':<5} closed blocker does not")
results += [b1, b2]

# --- cli: graph needs no config ---------------------------------------------
print("\ncli")
import io, contextlib, shutil, subprocess
from runner.cli import main as cli_main

with tempfile.TemporaryDirectory() as tmp:
    cwd = os.getcwd()
    os.chdir(tmp)                      # no config.yaml anywhere here
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli_main(["graph"])
        g_ok = rc == 0 and "stateDiagram-v2" in buf.getvalue()
    except Exception as exc:
        g_ok = False
        print(f"        raised: {exc}")
    finally:
        os.chdir(cwd)
print(f"  {'PASS' if g_ok else 'FAIL':<5} `graph` works with no config file present")
results.append(g_ok)

# --- vcs: worktree re-creation over an existing branch ----------------------
print("\nworktree re-creation")
from runner import vcs

if shutil.which("git") is None:
    print("  SKIP  git not on PATH")
else:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        origin, main_co, work = root / "origin.git", root / "main", root / "work"
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        vcs.run(["git", "init", "--bare", "-b", "main", str(origin)])
        vcs.run(["git", "clone", str(origin), str(main_co)], env=env)
        (main_co / "f.txt").write_text("hello")
        vcs.run(["git", "add", "-A"], cwd=main_co, env=env)
        vcs.run(["git", "commit", "-m", "init"], cwd=main_co, env=env)
        vcs.run(["git", "push", "-u", "origin", "main"], cwd=main_co, env=env)

        repo = {"key": "r", "path": str(main_co), "remote": str(origin),
                "base_branch": "main"}

        wt1 = vcs.ensure_worktree(repo, "ai/test-branch", work, env=env)
        (wt1 / "g.txt").write_text("agent work")
        vcs.commit_all(wt1, "agent commit", env=env)
        sha = vcs.run(["git", "rev-parse", "HEAD"], cwd=wt1).stdout.strip()

        # Worktree cleaned up (merged, or work_dir wiped) but branch survives.
        vcs.remove_worktree(repo, wt1, env=env)
        alive = vcs.branch_exists(main_co, "ai/test-branch", env=env)

        try:
            wt2 = vcs.ensure_worktree(repo, "ai/test-branch", work, env=env)
            sha2 = vcs.run(["git", "rev-parse", "HEAD"], cwd=wt2).stdout.strip()
            w_ok, keeps = True, sha2 == sha
        except Exception as exc:
            w_ok, keeps = False, False
            print(f"        raised: {exc}")

        for label, ok in [("branch survives worktree removal", alive),
                          ("re-creating worktree on existing branch works", w_ok),
                          ("re-created worktree keeps the PR's commits", keeps)]:
            print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
            results.append(ok)

        # The reviewer gets its own detached checkout of the pushed branch.
        vcs.push(wt2, "ai/test-branch", env=env)
        rv = vcs.ensure_review_worktree(repo, "ai/test-branch", work, env=env)
        rv_sha = vcs.run(["git", "rev-parse", "HEAD"], cwd=rv).stdout.strip()
        rv_head = vcs.run(["git", "symbolic-ref", "-q", "HEAD"],
                          cwd=rv, check=False).returncode != 0
        rv_apart = rv.resolve() != wt2.resolve()
        rv_again = vcs.ensure_review_worktree(repo, "ai/test-branch", work,
                                              env=env).resolve() == rv.resolve()

        bn = vcs.branch_name("DEV-12", "Add CSV export to reports")
        bn_ok = bn.startswith("ai/dev-12-") and " " not in bn

        for label, ok in [("review worktree holds the pushed commit", rv_sha == sha),
                          ("review worktree is detached", rv_head),
                          ("review worktree is separate from the impl worktree", rv_apart),
                          ("re-running review checkout is idempotent", rv_again),
                          ("branch_name slugs key and title", bn_ok)]:
            print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
            results.append(ok)

        # A second review cycle must see the newly pushed commits, not just
        # reuse the old checkout path - prove the re-point, not just reuse.
        (wt2 / "h.txt").write_text("second agent commit")
        vcs.commit_all(wt2, "second agent commit", env=env)
        sha2 = vcs.run(["git", "rev-parse", "HEAD"], cwd=wt2).stdout.strip()
        vcs.push(wt2, "ai/test-branch", env=env)

        rv2 = vcs.ensure_review_worktree(repo, "ai/test-branch", work, env=env)
        rv2_sha = vcs.run(["git", "rev-parse", "HEAD"], cwd=rv2).stdout.strip()
        rv2_head = vcs.run(["git", "symbolic-ref", "-q", "HEAD"],
                           cwd=rv2, check=False).returncode != 0
        rv2_apart = rv2.resolve() == rv.resolve()

        for label, ok in [("review worktree re-points to the newly pushed commit",
                           rv2_sha == sha2 and sha2 != sha),
                          ("re-pointed review worktree is still detached", rv2_head),
                          ("re-pointed review worktree is still the same path", rv2_apart)]:
            print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
            results.append(ok)

# --- agent: the two seams build different command lines --------------------
print("\nagent seams")
import runner.agents.claude_code as CC

class _Proc:
    returncode = 0
    stderr = ""
    stdout = json.dumps({"result": "{}", "total_cost_usd": 0.0,
                         "num_turns": 1, "duration_ms": 1})

def _capture(cfg, **work_kwargs):
    """Run reason() and work() with subprocess stubbed; return both argv lists."""
    seen = []
    agent = CC.ClaudeCodeAgent({"binary": sys.executable, **cfg})
    real = CC.subprocess.run
    CC.subprocess.run = lambda cmd, **kw: (seen.append(cmd), _Proc())[1]
    try:
        agent.reason("classify this")
        agent.work("implement this", Path("."), **work_kwargs)
    finally:
        CC.subprocess.run = real
    return seen[0], seen[1]

def _flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None

r_cmd, w_cmd = _capture({"model": "sonnet", "triage_model": "opus"})
checks = [
    ("reason() uses triage_model", _flag(r_cmd, "--model") == "opus"),
    ("work() uses model",          _flag(w_cmd, "--model") == "sonnet"),
    ("reason() still has no tools", _flag(r_cmd, "--allowedTools") == ""),
    ("reason() is one turn",        _flag(r_cmd, "--max-turns") == "1"),
    ("work() may dispatch subagents", "Agent" in (_flag(w_cmd, "--allowedTools") or "")),
]

r2, w2 = _capture({"model": "sonnet"})          # triage_model omitted
checks.append(("triage_model falls back to model",
               _flag(r2, "--model") == "sonnet" and _flag(w2, "--model") == "sonnet"))

r3, w3 = _capture({})                            # neither set
checks.append(("no model configured -> no --model flag",
               "--model" not in r3 and "--model" not in w3))

r4, w4 = _capture({"model": "sonnet", "allowed_tools": ["Read", "Edit"]})
checks.append(("explicit allowed_tools is not overridden",
               _flag(w4, "--allowedTools") == "Read,Edit"))

_, w5 = _capture({"model": "sonnet"}, model="opus")
checks.append(("work(model=...) overrides the configured model",
               _flag(w5, "--model") == "opus"))

_, w6 = _capture({"model": "sonnet", "allowed_tools": ["Read", "Edit"]},
                 allowed_tools=["Read", "Grep"])
checks.append(("work(allowed_tools=...) overrides the configured tools",
               _flag(w6, "--allowedTools") == "Read,Grep"))

_, w7 = _capture({"model": "sonnet", "allowed_tools": ["Read", "Edit"]})
checks.append(("no override leaves configured tools alone",
               _flag(w7, "--allowedTools") == "Read,Edit"))

_, w8 = _capture({"model": "sonnet"}, allowed_tools=[])
checks.append(("empty allowed_tools means no tools, not 'unset'",
               _flag(w8, "--allowedTools") == ""))

for label, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(ok)

# --- json extraction --------------------------------------------------------
print("\njson extraction")
from runner.jsonio import parse_json_object

_j_checks = []
_j_checks.append(("bare object", parse_json_object('{"a": 1}') == {"a": 1}))
_j_checks.append(("fenced object",
                  parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}))
_j_checks.append(("prose before and after",
                  parse_json_object('Sure!\n{"a": 1}\nHope that helps.') == {"a": 1}))
_j_checks.append(("nested braces survive",
                  parse_json_object('{"a": {"b": 2}}') == {"a": {"b": 2}}))

try:
    parse_json_object("no object here")
    _j_checks.append(("missing object raises ValueError", False))
except ValueError:
    _j_checks.append(("missing object raises ValueError", True))

for label, ok in _j_checks:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(ok)

# --- brain ------------------------------------------------------------------
print("\nrepo brain")
import datetime as _dt
from runner import brain as B

with tempfile.TemporaryDirectory() as tmp:
    bp = B.brain_path(Path(tmp), "mycorp", "pulse-backend")
    day = _dt.date(2026, 8, 24)

    b_missing = B.read(bp) == "" and B.as_prompt_block(bp) == ""

    w1 = B.append(bp, ["Tests need PGHOST=localhost."], today=day)
    b_write = w1 == ["Tests need PGHOST=localhost."] and bp.exists()
    b_dated = bp.read_text().startswith("- 2026-08-24: Tests need PGHOST")

    # Same note, different whitespace and case: still a duplicate.
    w2 = B.append(bp, ["tests   need  PGHOST=localhost."], today=day)
    b_dedupe = w2 == [] and bp.read_text().count("PGHOST") == 1

    w3 = B.append(bp, ["The lint step needs node 20."], today=day)
    b_second = w3 == ["The lint step needs node 20."]

    # At the cap: refuse, do not evict.
    before = bp.read_text()
    w4 = B.append(bp, ["x" * 200], max_bytes=len(before.encode()) + 10, today=day)
    b_cap = w4 == [] and bp.read_text() == before

    b_block = "=== REPO NOTES" in B.as_prompt_block(bp)

    # Two sources mapping the same repo key must not share a file.
    b_ns = B.brain_path(Path(tmp), "acme", "pulse-backend") != bp

for label, ok in [("missing brain reads as empty", b_missing),
                  ("append writes the note", b_write),
                  ("notes are dated", b_dated),
                  ("dedupe ignores case and whitespace", b_dedupe),
                  ("a genuinely new note is appended", b_second),
                  ("at the cap it refuses rather than evicting", b_cap),
                  ("as_prompt_block wraps the notes", b_block),
                  ("sources are namespaced apart", b_ns)]:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(ok)

# --- analyst station --------------------------------------------------------
print("\nanalyst station")
from runner import analyst as AN

class _ScriptedAgent:
    """Returns canned text per work() call, in order. Records the argv-ish kwargs."""
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    def reason(self, prompt, *, max_turns=1):
        return AgentResult(ok=True, text=self.replies.pop(0), cost_usd=0.01)
    def work(self, prompt, cwd, *, log_path=None, model=None, allowed_tools=None):
        self.calls.append({"prompt": prompt, "model": model,
                           "allowed_tools": allowed_tools})
        return AgentResult(ok=True, text=self.replies.pop(0), cost_usd=0.2)

_ready = json.dumps({
    "verdict": "ready", "problem": "CSV export is missing",
    "approach": "add a serializer and a route",
    "plan": ["add serializer", "wire route", "test"],
    "acceptance_criteria": ["GET /reports.csv returns text/csv"],
    "risks": ["large exports may time out"],
    "files_of_interest": ["api/reports.py — the route lives here"],
})
_ag = _ScriptedAgent([_ready])
_res, _cost = AN.analyse(
    _ag, mk("T20", "Add CSV export", "button downloads a CSV"), [],
    {"key": "pulse-backend", "test": "pytest"},
    "=== REPO NOTES ===\nTests need PGHOST.\n", ["csv downloads"],
    model="opus", cwd=Path("."))

a1 = _res.verdict == "ready"
a2 = _res.acceptance_criteria == ["GET /reports.csv returns text/csv"]
a3 = _cost == 0.2
a4 = _ag.calls[0]["model"] == "opus"
a5 = _ag.calls[0]["allowed_tools"] == AN.PLANNER_TOOLS
a6 = "Write" not in AN.PLANNER_TOOLS and "Edit" not in AN.PLANNER_TOOLS
a7 = "Tests need PGHOST." in _ag.calls[0]["prompt"]
a8 = "csv downloads" in _ag.calls[0]["prompt"]

_amb = _ScriptedAgent([json.dumps({"verdict": "needs_input",
                                   "questions": ["Which columns?"]})])
_res2, _ = AN.analyse(_amb, mk("T21", "Export", "x"), [],
                      {"key": "pulse-backend"}, "", [], cwd=Path("."))
a9 = _res2.verdict == "needs_input" and _res2.questions == ["Which columns?"]

_bad = _ScriptedAgent(["I could not do it", "still no json"])
try:
    AN.analyse(_bad, mk("T22", "x", "y"), [], {"key": "r"}, "", [], cwd=Path("."))
    a10 = False
except ValueError:
    a10 = len(_bad.calls) == 2   # retried once before giving up

_str_plan = _ScriptedAgent([json.dumps({
    "verdict": "ready", "plan": "do the thing",
})])
_res11, _ = AN.analyse(_str_plan, mk("T23", "x", "y"), [],
                       {"key": "r"}, "", [], cwd=Path("."))
a11 = _res11.plan == ["do the thing"] and _res11.verdict == "ready"

_no_plan = _ScriptedAgent([json.dumps({"verdict": "ready"})])
_res12, _ = AN.analyse(_no_plan, mk("T24", "x", "y"), [],
                       {"key": "r"}, "", [], cwd=Path("."))
a12 = _res12.verdict == "needs_input" and len(_res12.questions) == 1

for label, ok in [("analyst returns a ready verdict", a1),
                  ("analyst criteria are parsed", a2),
                  ("analyst cost is reported", a3),
                  ("analyst honours the model override", a4),
                  ("analyst runs with read-only tools", a5),
                  ("planner tools grant no write capability", a6),
                  ("brain is injected into the prompt", a7),
                  ("triage criteria are injected into the prompt", a8),
                  ("analyst can ask for clarification", a9),
                  ("unparseable output is retried once then raises", a10),
                  ("a string plan is coerced, not iterated char-by-char", a11),
                  ("a missing plan still triggers the empty-plan downgrade", a12)]:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(ok)


# --- reviewer station -------------------------------------------------------
print("\nreviewer station")
from runner import review as RV

_approve = json.dumps({
    "verdict": "approve",
    "criteria_results": [{"criterion": "GET /reports.csv returns text/csv",
                          "pass": True, "evidence": "route added, test passes"}],
    "blocking_findings": [], "suggestions": ["consider streaming"],
    "summary": "Small, correct, tested.",
    "brain_notes": ["Reports tests need PGHOST=localhost."],
})
_rg = _ScriptedAgent([_approve])
_rr, _rc = RV.review(
    _rg, "T30", ["GET /reports.csv returns text/csv"], "main",
    "ai/t30-add-csv", "added a serializer and a route", "",
    cwd=Path("."), model="opus",
    tools=RV.tools_for({"key": "r", "test": "pytest -q"}))

v1 = _rr.verdict == "approve"
v2 = _rr.brain_notes == ["Reports tests need PGHOST=localhost."]
v3 = _rr.criteria_results[0]["pass"] is True
v4 = _rc == 0.2 and _rg.calls[0]["model"] == "opus"
v5 = "Bash(pytest -q:*)" in _rg.calls[0]["allowed_tools"]
v6 = "Write" not in RV.REVIEWER_TOOLS and "Edit" not in RV.REVIEWER_TOOLS
v7 = "ai/t30-add-csv" in _rg.calls[0]["prompt"]
v8 = RV.tools_for({"key": "r"}) == RV.REVIEWER_TOOLS  # no test cmd -> no Bash add

_rc2 = _ScriptedAgent([json.dumps({"verdict": "request_changes",
                                   "blocking_findings": ["no test for the empty case"],
                                   "summary": "close"})])
_rr2, _ = RV.review(_rc2, "T31", [], "main", "b", "s", "", cwd=Path("."))
v9 = _rr2.verdict == "request_changes" and bool(_rr2.blocking_findings)

# A request_changes with no findings is unactionable; treat it as approve.
_rc3 = _ScriptedAgent([json.dumps({"verdict": "request_changes",
                                   "blocking_findings": [], "summary": "eh"})])
_rr3, _ = RV.review(_rc3, "T32", [], "main", "b", "s", "", cwd=Path("."))
v10 = _rr3.verdict == "approve"

for label, ok in [("reviewer returns an approve verdict", v1),
                  ("reviewer brain notes are parsed", v2),
                  ("per-criterion results are parsed", v3),
                  ("reviewer honours model override and reports cost", v4),
                  ("repo test command is added to the tool surface", v5),
                  ("reviewer tools grant no write capability", v6),
                  ("branch under review is named in the prompt", v7),
                  ("a repo with no test command adds no Bash tool", v8),
                  ("request_changes carries findings", v9),
                  ("request_changes with no findings downgrades to approve", v10)]:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(ok)

# --- planning station -------------------------------------------------------
print("\nplanning station")

def _plan_case(name, scope, mode, expect_state):
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config.load("config.example.yaml")
        cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w",
                                  log_dir=f"{tmp}/l", brain_dir=f"{tmp}/b",
                                  mode=mode)
        src = cfg.sources(only=["mycorp"])[0]
        task = mk("T40", "Add CSV export to reports", "button downloads a CSV")
        prov = FakeProvider([task])
        store = Store(f"{tmp}/s.db")
        agent = FakeAgent({"decision": "execute", "confidence": 0.9,
                           "scope": scope, "plan": ["a"],
                           "acceptance_criteria": ["csv downloads"]})
        pipe = Pipeline(cfg, src, prov, agent, store)
        pipe.tick(dt.datetime(2026, 8, 10, 7, 0))
        got = store.get("mycorp:T40")
        ok = got["state"] == expect_state.value
        print(f"  {'PASS' if ok else 'FAIL':<5} {name:<44} -> {got['state']}")
        results.append(ok)
        return got

# xs is below the gate: straight to queued, no planning run.
_plan_case("xs skips planning", "xs", "execute", AIState.QUEUED)
# m is at the gate: planning first.
_row_m = _plan_case("m enters planning", "m", "execute", AIState.PLANNING)
# shadow always gates, and planning is read-only so it still runs.
_plan_case("shadow enters planning", "xs", "shadow", AIState.PLANNING)

# Triage criteria are persisted, not just commented.
_meta_ok = "csv downloads" in (_row_m["meta"] or "")
print(f"  {'PASS' if _meta_ok else 'FAIL':<5} triage acceptance criteria are stored")
results.append(_meta_ok)

# --- planning crash recovery -------------------------------------------------
print("\nplanning crash recovery")

class _CrashingAgent:
    """reason() triages fine; work() always dies, as if the analyst crashed."""
    def __init__(self, decision): self.decision = decision
    def reason(self, prompt, *, max_turns=1):
        return AgentResult(ok=True, cost_usd=0.01, text=json.dumps(self.decision))
    def work(self, prompt, cwd, *, log_path=None, model=None, allowed_tools=None):
        raise RuntimeError("analyst crashed")

with tempfile.TemporaryDirectory() as tmp:
    from runner import vcs as _vcs2

    cfg = Config.load("config.example.yaml")
    cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w",
                              log_dir=f"{tmp}/l", brain_dir=f"{tmp}/b")
    cfg.data["runner"]["limits"]["max_fix_attempts"] = 2
    src = cfg.sources(only=["mycorp"])[0]
    task = mk("T50", "Add CSV export to reports", "button downloads a CSV")
    prov = FakeProvider([task])
    store = Store(f"{tmp}/s.db")
    agent = _CrashingAgent({"decision": "execute", "confidence": 0.9, "scope": "m",
                            "plan": ["a"], "acceptance_criteria": ["csv downloads"]})
    pipe = Pipeline(cfg, src, prov, agent, store)
    when = dt.datetime(2026, 8, 10, 7, 0)

    # Offline suite: stub the worktree checkout so the crash under test is
    # the analyst's, not git's. See "approval round trip" above.
    _checkout = Path(tmp) / "checkout"
    _checkout.mkdir()
    _real_ensure_worktree2 = _vcs2.ensure_worktree
    _vcs2.ensure_worktree = lambda *a, **k: _checkout
    try:
        pipe.tick(when)   # triage: routes into planning (sets plan_pending)
        pipe.tick(when)   # planning, fresh entry: analyst raises. This is the
                          # first attempt, not a retry, so it must not count
                          # against `attempts` (R6 fix).

        row0 = store.get("mycorp:T50")
        c0 = row0["state"] == AIState.PLANNING.value and int(row0["attempts"]) == 0
        print(f"  {'PASS' if c0 else 'FAIL':<5} a crash on the first planning attempt does not count against attempts")
        results.append(c0)

        pipe.tick(when)   # planning attempt 1 (retry): analyst raises

        row1 = store.get("mycorp:T50")
        c1 = row1["state"] == AIState.PLANNING.value and int(row1["attempts"]) == 1
        print(f"  {'PASS' if c1 else 'FAIL':<5} an ordinary exception in planning increments attempts")
        results.append(c1)

        pipe.tick(when)   # planning attempt 2: analyst raises again
        pipe.tick(when)   # planning attempt 3: exceeds max_fix_attempts -> escalate

        row2 = store.get("mycorp:T50")
        c2 = row2["state"] == AIState.BLOCKED.value
        print(f"  {'PASS' if c2 else 'FAIL':<5} exceeding max_fix_attempts escalates planning to blocked")
        results.append(c2)
    finally:
        _vcs2.ensure_worktree = _real_ensure_worktree2

# --- triage error handling ---------------------------------------------------
print("\ntriage error handling")

class _BadReasonAgent:
    """reason() returns unparseable text, so triage() raises inside _do_triage."""
    def reason(self, prompt, *, max_turns=1):
        return AgentResult(ok=True, cost_usd=0.0, text="not json at all")
    def work(self, prompt, cwd, *, log_path=None, model=None, allowed_tools=None):
        return AgentResult(ok=True, text="did the thing", cost_usd=0.0)

with tempfile.TemporaryDirectory() as tmp:
    cfg = Config.load("config.example.yaml")
    cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w",
                              log_dir=f"{tmp}/l", brain_dir=f"{tmp}/b")
    src = cfg.sources(only=["mycorp"])[0]
    task = mk("T51", "Broken triage", "x")
    prov = FakeProvider([task])
    store = Store(f"{tmp}/s.db")
    pipe = Pipeline(cfg, src, prov, _BadReasonAgent(), store)
    pipe.tick(dt.datetime(2026, 8, 10, 7, 0))

    row = store.get("mycorp:T51")
    e1 = row is not None and bool(row.get("last_error"))
    e2 = store.get("T51") is None   # no unnamespaced row created
    print(f"  {'PASS' if e1 else 'FAIL':<5} triage failure records last_error on the namespaced row")
    print(f"  {'PASS' if e2 else 'FAIL':<5} triage failure creates no unnamespaced row")
    results += [e1, e2]

# --- review station ---------------------------------------------------------
print("\nreview station")

def _review_flow(verdicts, *, cycles=2, ticks=10):
    """Drive one ticket from queued through the review loop. Returns (store, prov, brain_dir)."""
    tmp = tempfile.mkdtemp()
    cfg = Config.load("config.example.yaml")
    cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w",
                              log_dir=f"{tmp}/l", brain_dir=f"{tmp}/b",
                              mode="execute")
    cfg.data["runner"]["limits"]["max_review_cycles"] = cycles
    src = cfg.sources(only=["mycorp"])[0]
    task = mk("T50", "Fix footer typo", "'Copyrigth' -> 'Copyright'")
    prov = FakeProvider([task])
    store = Store(f"{tmp}/s.db")

    replies = []
    for v in verdicts:
        replies.append("implemented it")           # the implementer run
        replies.append(json.dumps(v))              # the reviewer run
    agent = FakeAgent({"decision": "execute", "confidence": 0.95, "scope": "xs",
                       "plan": ["fix typo"],
                       "acceptance_criteria": ["footer reads Copyright"]},
                      work_replies=replies)
    pipe = Pipeline(cfg, src, prov, agent, store)

    real = {n: getattr(vcs, n) for n in
            ("ensure_worktree", "ensure_review_worktree", "commit_all", "push",
             "create_pr", "diff_stat", "remove_worktree")}
    made = []
    vcs.ensure_worktree = lambda *a, **k: Path(tmp)
    vcs.ensure_review_worktree = lambda *a, **k: Path(tmp)
    vcs.commit_all = lambda *a, **k: True
    vcs.push = lambda *a, **k: None
    vcs.diff_stat = lambda *a, **k: "1 file changed"
    vcs.create_pr = lambda *a, **k: (made.append(1), (7, "http://pr/7"))[1]
    vcs.remove_worktree = lambda *a, **k: None
    try:
        when = dt.datetime(2026, 8, 10, 7, 0)
        for _ in range(ticks):
            pipe.tick(when)
    finally:
        for n, f in real.items():
            setattr(vcs, n, f)
    return store.get("mycorp:T50"), prov, Path(f"{tmp}/b"), len(made)

_approve_v = {"verdict": "approve",
              "criteria_results": [{"criterion": "footer reads Copyright",
                                    "pass": True, "evidence": "diff shows it"}],
              "blocking_findings": [], "suggestions": [], "summary": "fine",
              "brain_notes": ["Footer copy lives in templates/base.html."]}
_changes_v = {"verdict": "request_changes",
              "blocking_findings": ["the test is missing"],
              "summary": "nearly", "brain_notes": []}
_reject_v = {"verdict": "reject", "blocking_findings": ["wrong file entirely"],
             "summary": "no", "brain_notes": []}

_r_ok, _p_ok, _b_ok, _n_ok = _review_flow([_approve_v])
c1 = _r_ok["state"] == AIState.PR_OPEN.value
c2 = _n_ok == 1
c3 = "Footer copy lives in" in B.read(B.brain_path(_b_ok, "mycorp", "pulse-backend"))

_r_cy, _, _, _n_cy = _review_flow([_changes_v, _approve_v])
c4 = _r_cy["state"] == AIState.PR_OPEN.value and _n_cy == 1

_r_cap, _, _b_cap, _n_cap = _review_flow([_changes_v, _changes_v, _changes_v], cycles=2)
c5 = _r_cap["state"] == AIState.BLOCKED.value and _n_cap == 0
c6 = B.read(B.brain_path(_b_cap, "mycorp", "pulse-backend")) == ""

_r_rej, _, _, _n_rej = _review_flow([_reject_v])
c7 = _r_rej["state"] == AIState.BLOCKED.value and _n_rej == 0

for label, ok in [("approve opens the PR", c1),
                  ("the PR is opened exactly once", c2),
                  ("brain notes are written on approve", c3),
                  ("request_changes then approve still ships one PR", c4),
                  ("exhausted review cycles block, with no PR", c5),
                  ("no brain notes are kept when nothing is approved", c6),
                  ("reject blocks without a PR", c7)]:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(ok)

# A dead reviewer or analyst process must be recovered by `attempts`, and must
# not silently consume a review cycle.
with tempfile.TemporaryDirectory() as tmp:
    cfg = Config.load("config.example.yaml")
    cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w",
                              log_dir=f"{tmp}/l", brain_dir=f"{tmp}/b",
                              mode="execute")
    src = cfg.sources(only=["mycorp"])[0]
    task = mk("T55", "Fix footer typo", "x")
    prov = FakeProvider([task])
    store = Store(f"{tmp}/s.db")
    pipe = Pipeline(cfg, src, prov, FakeAgent({}), store)
    when = dt.datetime(2026, 8, 10, 7, 0)

    # A row abandoned mid-review: state REVIEW, lease long expired.
    store.upsert("mycorp:T55", source="mycorp", provider_task_id="T55",
                 task_key="T55", repo_key="pulse-backend", branch="ai/t55",
                 state=AIState.REVIEW.value, attempts=0,
                 meta={"review_cycle": 0, "acceptance_criteria": []})
    store.conn.execute(
        "UPDATE tasks SET lease_owner='dead', lease_until=1 WHERE task_id=?",
        ("mycorp:T55",))
    store.conn.commit()

    real_rw = vcs.ensure_review_worktree
    vcs.ensure_review_worktree = lambda *a, **k: Path(tmp)
    try:
        pipe.tick(when)
    except Exception:
        pass                      # the fake agent returns no JSON; that is fine
    finally:
        vcs.ensure_review_worktree = real_rw

    _crashed = store.get("mycorp:T55")
    c8 = int(_crashed["attempts"]) == 1
    c9 = json.loads(_crashed["meta"] or "{}").get("review_cycle", 0) == 0

for label, ok in [("an expired review lease increments attempts", c8),
                  ("crash recovery does not consume a review cycle", c9)]:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(ok)

# R6 fix: `max_review_cycles` must be authoritative even when it exceeds
# `max_fix_attempts`. With cap=5 and default max_fix_attempts=3, six
# non-crashing request_changes verdicts must NOT trip the crash-recovery
# escalation at review-arm entry #3 (attempts never gets there, because a
# healthy entry consumes the `review_pending` marker instead of counting).
# The row only blocks once the review-cycle cap itself is exceeded, and the
# escalation message must say so, not "crashed".
_r_capfix, _, _, _n_capfix = _review_flow([_changes_v] * 6, cycles=5, ticks=20)
c10 = _r_capfix["state"] == AIState.BLOCKED.value
c11 = int(_r_capfix["attempts"]) == 0
_capfix_err = (_r_capfix["last_error"] or "").lower()
c12 = "revision" in _capfix_err and "crashed" not in _capfix_err
c13 = _n_capfix == 0

for label, ok in [("max_review_cycles=5 outlives max_fix_attempts=3", c10),
                  ("a healthy review loop never touches attempts", c11),
                  ("the escalation names cycle exhaustion, not a crash", c12),
                  ("no PR opens once cycles are exhausted", c13)]:
    print(f"  {'PASS' if ok else 'FAIL':<5} {label}")
    results.append(ok)

# A healthy (non-crashing) review run leaves `attempts` untouched.
c14 = int(_r_ok["attempts"]) == 0
print(f"  {'PASS' if c14 else 'FAIL':<5} a healthy review run leaves attempts at 0")
results.append(c14)

# A healthy (non-crashing) planning run must leave `attempts` at 0 too — the
# same R6 defect, and the same fix shape, applied to PLANNING.
with tempfile.TemporaryDirectory() as tmp:
    cfg = Config.load("config.example.yaml")
    cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w",
                              log_dir=f"{tmp}/l", brain_dir=f"{tmp}/b")
    src = cfg.sources(only=["mycorp"])[0]
    task = mk("T57", "Add CSV export to reports", "button downloads a CSV")
    prov = FakeProvider([task])
    store = Store(f"{tmp}/s.db")
    agent = FakeAgent(
        {"decision": "execute", "confidence": 0.9, "scope": "m",
         "plan": ["a", "b"], "acceptance_criteria": ["csv downloads"]},
        work_replies=[json.dumps({"verdict": "ready", "plan": ["a", "b"],
                                  "acceptance_criteria": ["csv downloads"]})])
    pipe = Pipeline(cfg, src, prov, agent, store)
    when = dt.datetime(2026, 8, 10, 7, 0)

    _checkout = Path(tmp) / "checkout"
    _checkout.mkdir()
    _real_ew = vcs.ensure_worktree
    vcs.ensure_worktree = lambda *a, **k: _checkout
    try:
        pipe.tick(when)   # triage: routes into planning
        pipe.tick(when)   # planning: analyst grounds the plan (healthy run)
    finally:
        vcs.ensure_worktree = _real_ew

    _healthy_plan = store.get("mycorp:T57")
    c15 = (_healthy_plan["state"] == AIState.AWAITING_PLAN_OK.value
          and int(_healthy_plan["attempts"]) == 0)

print(f"  {'PASS' if c15 else 'FAIL':<5} a healthy planning run leaves attempts at 0")
results.append(c15)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
