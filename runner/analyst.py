"""The analyst station: a plan that has actually read the code.

Triage is deliberately repo-blind — it is one tool-less turn that classifies
from ticket text. That is the right cost for classification, but it means the
plan a human approves was written without opening a single file. This station
closes that gap for exactly the tickets a human is going to sign off on.

Read-only by construction: the tool allowlist grants no write capability, which
is what makes it safe to run in shadow mode.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .agents.base import CodingAgent
from .jsonio import parse_json_object
from .models import AnalysisResult, Comment, Task

log = logging.getLogger(__name__)

PLANNER_TOOLS = ["Read", "Glob", "Grep", "Bash(git log:*)", "Bash(git diff:*)"]

SYSTEM = """You are the analyst stage of an automated engineering pipeline.
You have a read-only checkout of the target repository as your working
directory. You produce the plan a human will approve or reject; you never
write code.

Read before you plan. Open the files that matter, follow the conventions
already in the codebase, and ground every step in something you actually saw.
A plan that names real files and real functions is worth ten that describe
intentions.

Choose "needs_input" when the code makes the ticket ambiguous in a way the
ticket text hid: two plausible call sites, a convention the ticket contradicts,
a dependency that is not there. A specific question now is far cheaper than a
wrong implementation later.

Otherwise choose "ready" and write acceptance criteria that are checkable
against a diff. "Works correctly" is not a criterion; "GET /reports.csv
returns text/csv with a header row" is.

Reply with a SINGLE JSON object and nothing else. No markdown, no prose:
{
  "verdict": "ready" | "needs_input",
  "problem": "one paragraph: what is actually wrong or missing",
  "approach": "one paragraph: how you would fix it, and why this way",
  "plan": ["ordered implementation steps, naming real files"],
  "acceptance_criteria": ["checkable against a diff"],
  "risks": ["what could go wrong"],
  "files_of_interest": ["path/to/file.py - why it matters"],
  "questions": ["only when verdict is needs_input"]
}"""

_RETRY_SUFFIX = (
    "\n\nYour previous reply was not a single JSON object. Reply with the JSON "
    "object only: no prose before it, no code fence around it."
)


def _str_list(value: Any) -> list[str]:
    """Coerce a model's list field into a list of strings.

    A model that returns prose where an array was asked for is common enough
    to handle deliberately: iterating a bare string would yield one entry per
    character, and a non-empty list of single letters defeats the empty-plan
    guard below.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return [str(value)]


def build_prompt(task: Task, comments: list[Comment], repo: dict[str, Any],
                 brain_block: str, criteria: list[str]) -> str:
    thread = "\n".join(
        f"[{'AI' if c.is_bot else c.author}] {c.text.strip()}" for c in comments[-20:]
    ) or "(no comments)"
    crit = "\n".join(f"- {c}" for c in criteria) or "(none inferred at triage)"
    return (
        f"{SYSTEM}\n\n"
        f"=== TICKET ===\n{task.as_prompt_block()}\n"
        f"=== REPO ===\n{repo.get('key')} ({repo.get('language', 'unknown stack')})\n"
        f"Test command: {repo.get('test', '(none configured)')}\n"
        f"Notes: {repo.get('notes', '-')}\n\n"
        f"{brain_block}\n"
        f"=== ACCEPTANCE CRITERIA INFERRED AT TRIAGE ===\n{crit}\n"
        f"(Refine these. They were written without reading the code.)\n\n"
        f"=== COMMENT THREAD ===\n{thread}\n\n"
        "Return the JSON object now."
    )


def analyse(agent: CodingAgent, task: Task, comments: list[Comment],
            repo: dict[str, Any], brain_block: str, criteria: list[str], *,
            cwd: Path, model: str | None = None, tools: list[str] | None = None,
            log_path: Path | None = None) -> tuple[AnalysisResult, float]:
    """Run the analyst in `cwd`. Retries once on unparseable output."""
    prompt = build_prompt(task, comments, repo, brain_block, criteria)
    allowed = PLANNER_TOOLS if tools is None else tools

    cost = 0.0
    data: dict[str, Any] | None = None
    for attempt in (1, 2):
        res = agent.work(prompt if attempt == 1 else prompt + _RETRY_SUFFIX,
                         cwd, log_path=log_path, model=model,
                         allowed_tools=allowed)
        cost += res.cost_usd
        if not res.ok:
            raise RuntimeError(f"analyst agent failed: {res.error}")
        try:
            data = parse_json_object(res.text)
            break
        except ValueError:
            log.warning("analyst returned unparseable output (attempt %d)", attempt)
    if data is None:
        raise ValueError("analyst returned no JSON object after a retry")

    verdict = str(data.get("verdict", "needs_input"))
    out = AnalysisResult(
        verdict=verdict if verdict in ("ready", "needs_input") else "needs_input",
        problem=str(data.get("problem", "")),
        approach=str(data.get("approach", "")),
        plan=_str_list(data.get("plan")),
        acceptance_criteria=_str_list(data.get("acceptance_criteria")),
        risks=_str_list(data.get("risks")),
        files_of_interest=_str_list(data.get("files_of_interest")),
        questions=_str_list(data.get("questions")),
    )

    # A "ready" verdict with no plan is not ready, whatever the model says.
    if out.verdict == "ready" and not out.plan:
        out.verdict = "needs_input"
        out.questions = out.questions or [
            "I could not turn this into concrete implementation steps from the "
            "code. Could you point me at the area you have in mind?"]
    return out, cost
