"""The reviewer station: an independent verdict on the pushed branch.

This is the station that decides whether a human is asked to look at anything.
Its independence is the whole point, so it runs in its own detached checkout,
on its own model, and sees the pushed diff rather than the implementer's
account of it.

It cannot fix anything. Findings go back to the implementer; the reviewer never
edits code. That separation is what keeps the verdict worth having.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .agents.base import CodingAgent
from .jsonio import StationError, parse_json_object, str_list
from .models import ReviewResult

log = logging.getLogger(__name__)

REVIEWER_TOOLS = ["Read", "Glob", "Grep", "Bash(git log:*)", "Bash(git diff:*)"]

SYSTEM = """You are the quality gate of an automated engineering pipeline.
You receive a ticket, the acceptance criteria, a base branch, the name of a
branch that has been pushed, and the implementer's own account of what it did.
You decide whether this should reach a human reviewer.

You have no stake in the implementation. Judge it as if a colleague you have
never met submitted it.

Review the real diff, not the summary. Run `git diff <base>...<branch>` and
read it. Summaries describe intent; diffs describe reality.

Where a claim is cheap to check, check it. Re-run the test command if you have
it. Distrust "this should work"; look for output.

Review in this order:
1. Correctness — does the change solve the stated problem? Walk the logic.
2. Acceptance criteria — judge each one individually, pass or fail, with
   evidence from the diff or from output you saw.
3. Safety — unhandled edge cases, error paths, injection, authz gaps, secrets
   in code, data-loss risk.
4. Scope — unrelated changes, silent deviations from the plan, missing pieces.
5. Verification — was the testing adequate for the risk?
6. Quality — naming, readability, consistency with the codebase. Advisory
   unless severe.

Verdicts:
- "approve": ships as-is. Minor notes go in suggestions.
- "request_changes": fixable problems. Every blocking finding must name where
  it is, what is wrong, and why it matters.
- "reject": the approach itself is wrong and iteration will not fix it.

Do not approve out of politeness. Do not request changes over style preference.
Every blocking finding must trace to correctness, the criteria, safety, or scope.

In "brain_notes", record only durable facts about this repository that would
save a future run time: a build quirk, a non-obvious verification step, a
convention the implementer missed. Never one-off details about this ticket, and
never a claim you did not verify. Return an empty list when there is nothing.

Reply with a SINGLE JSON object and nothing else. No markdown, no prose:
{
  "verdict": "approve" | "request_changes" | "reject",
  "criteria_results": [{"criterion": "verbatim", "pass": true,
                        "evidence": "what shows it passing or failing"}],
  "blocking_findings": ["where, what is wrong, why it matters"],
  "suggestions": ["advisory, does not block"],
  "summary": "one paragraph: the verdict and what drove it",
  "brain_notes": ["durable repo-level facts worth keeping"]
}"""

_RETRY_SUFFIX = (
    "\n\nYour previous reply was not a single JSON object. Reply with the JSON "
    "object only: no prose before it, no code fence around it."
)


def tools_for(repo: dict[str, Any], base: list[str] | None = None) -> list[str]:
    """Reviewer tools plus the repo's own test command, when it has one.

    Re-running the checks the implementer claims to have run is most of what
    makes a review evidence-based rather than a reading exercise.
    """
    tools = list(REVIEWER_TOOLS if base is None else base)
    cmd = str(repo.get("test") or "").strip()
    if cmd:
        tools.append(f"Bash({cmd}:*)")
    return tools


def build_prompt(task_key: str, criteria: list[str], base: str, branch: str,
                 impl_summary: str, brain_block: str) -> str:
    crit = "\n".join(f"- {c}" for c in criteria) or "(none were recorded)"
    return (
        f"{SYSTEM}\n\n"
        f"=== TICKET ===\n{task_key}\n\n"
        f"=== BRANCH UNDER REVIEW ===\n"
        f"base: {base}\nbranch: {branch}\n"
        f"diff command: git diff {base}...{branch}\n\n"
        f"{brain_block}\n"
        f"=== ACCEPTANCE CRITERIA ===\n{crit}\n\n"
        f"=== THE IMPLEMENTER'S ACCOUNT (treat as a claim, not evidence) ===\n"
        f"{impl_summary[:4000]}\n\n"
        "Return the JSON object now."
    )


def review(agent: CodingAgent, task_key: str, criteria: list[str], base: str,
           branch: str, impl_summary: str, brain_block: str, *,
           cwd: Path, model: str | None = None, tools: list[str] | None = None,
           log_path: Path | None = None) -> tuple[ReviewResult, float]:
    """Run the reviewer in `cwd`. Retries once on unparseable output."""
    prompt = build_prompt(task_key, criteria, base, branch, impl_summary,
                          brain_block)
    allowed = REVIEWER_TOOLS if tools is None else tools

    cost = 0.0
    data: dict[str, Any] | None = None
    for attempt in (1, 2):
        res = agent.work(prompt if attempt == 1 else prompt + _RETRY_SUFFIX,
                         cwd, log_path=log_path, model=model,
                         allowed_tools=allowed)
        cost += res.cost_usd
        if not res.ok:
            raise StationError(f"reviewer agent failed: {res.error}", cost)
        try:
            data = parse_json_object(res.text)
            break
        except ValueError:
            log.warning("reviewer returned unparseable output (attempt %d)", attempt)
    if data is None:
        raise ValueError("reviewer returned no JSON object after a retry")

    verdict = str(data.get("verdict", "request_changes"))
    if verdict not in ("approve", "request_changes", "reject"):
        verdict = "request_changes"

    out = ReviewResult(
        verdict=verdict,
        criteria_results=[dict(x) for x in data.get("criteria_results", [])
                          if isinstance(x, dict)],
        blocking_findings=str_list(data.get("blocking_findings")),
        suggestions=str_list(data.get("suggestions")),
        summary=str(data.get("summary", "")),
        brain_notes=str_list(data.get("brain_notes")),
    )

    # "Change something, I won't say what" cannot be actioned and would burn a
    # revision cycle producing nothing. Treat it as approval with notes.
    if out.verdict == "request_changes" and not out.blocking_findings:
        log.warning("reviewer requested changes with no findings; treating as approve")
        out.verdict = "approve"
    return out, cost
