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
    def __init__(self, decision): self.decision = decision
    def reason(self, prompt, *, max_turns=1):
        import json
        return AgentResult(ok=True, cost_usd=0.01, text=json.dumps(self.decision))
    def work(self, prompt, cwd, *, log_path=None):
        return AgentResult(ok=True, text="did the thing", cost_usd=0.5)


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
    AIState.AWAITING_PLAN_OK)[0])

results.append(run_case(
    "small scope, shadow mode still gates",
    mk("T6", "Fix typo in footer", "'Copyrigth' -> 'Copyright'"),
    {"decision": "execute", "confidence": 0.95, "scope": "xs", "plan": ["fix typo"]},
    AIState.AWAITING_PLAN_OK)[0])

# --- approval round trip + idempotency -------------------------------------
print("\napproval round trip")
with tempfile.TemporaryDirectory() as tmp:
    cfg = Config.load("config.example.yaml")
    cfg.data["runner"].update(db_path=f"{tmp}/s.db", work_dir=f"{tmp}/w", log_dir=f"{tmp}/l")
    src = cfg.sources(only=["mycorp"])[0]
    task = mk("T7", "Add CSV export", "button downloads a CSV")
    prov = FakeProvider([task])
    store = Store(f"{tmp}/s.db")
    agent = FakeAgent({"decision": "execute", "confidence": 0.9, "scope": "m",
                       "plan": ["a", "b"], "acceptance_criteria": ["csv downloads"]})
    pipe = Pipeline(cfg, src, prov, agent, store)
    when = dt.datetime(2026, 8, 10, 7, 0)

    pipe.tick(when)
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
for label, ok in [("new -> planning exists", n1),
                  ("planning -> plan-review exists", n2),
                  ("planning -> needs-input exists", n3),
                  ("in-progress -> review exists", n4),
                  ("review -> pr-open exists", n5),
                  ("review -> queued exists (request_changes)", n6),
                  ("in-progress -> pr-open is gone", n7),
                  ("new states are not terminal", n8)]:
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

# --- agent: the two seams build different command lines --------------------
print("\nagent seams")
import runner.agents.claude_code as CC

class _Proc:
    returncode = 0
    stderr = ""
    stdout = json.dumps({"result": "{}", "total_cost_usd": 0.0,
                         "num_turns": 1, "duration_ms": 1})

def _capture(cfg):
    """Run reason() and work() with subprocess stubbed; return both argv lists."""
    seen = []
    agent = CC.ClaudeCodeAgent({"binary": sys.executable, **cfg})
    real = CC.subprocess.run
    CC.subprocess.run = lambda cmd, **kw: (seen.append(cmd), _Proc())[1]
    try:
        agent.reason("classify this")
        agent.work("implement this", Path("."))
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

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
