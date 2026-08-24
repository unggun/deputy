"""Triage: hard policy rules first, LLM classification second."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .agents.base import CodingAgent
from .models import AgentResult, Comment, Task, TriageResult

log = logging.getLogger(__name__)

# Non-negotiable. These override whatever the model decides.
DEFAULT_DENYLIST = [
    r"\bprod(uction)?\b.*\b(data|db|database|record|user)\b",
    r"\bmigration\b", r"\bdrop\s+table\b", r"\bbackfill\b",
    r"\bsecret\b", r"\bapi[_\s-]?key\b", r"\bcredential\b", r"\.env\b",
    r"\bpayment\b", r"\bbilling\b", r"\bstripe\b", r"\bpayout\b",
    r"\bauth(entication|orization)\b", r"\brbac\b", r"\bpermission model\b",
    r"\bdns\b", r"\bssl\b", r"\bcertificate\b", r"\bfirewall\b", r"\bterraform\b",
    r"\bgdpr\b", r"\bpii\b",
]

SYSTEM = """You are the triage stage of an automated engineering pipeline.
You classify a ticket into exactly one of three buckets. You are deliberately
conservative: when in doubt, choose "clarify". A wrong "execute" wastes an
engineer's review time and can ship a bad change; a wrong "clarify" only costs
one comment.

Choose "execute" ONLY IF ALL of these hold:
- The desired end state is unambiguous and testable.
- It is scoped to application code in one repository.
- No production data, credentials, infrastructure, auth model, or payments.
- A competent engineer new to the ticket could start without asking anything.

Choose "clarify" if requirements, acceptance criteria, affected surface,
expected behaviour, or reproduction steps are missing or ambiguous.

Choose "human_only" if the work needs human judgement, environment access,
a live production check, a third-party console, a design decision, or any
verification that cannot be done from the repository.

Reply with a SINGLE JSON object and nothing else. No markdown, no prose:
{
  "decision": "execute" | "clarify" | "human_only",
  "confidence": 0.0-1.0,
  "scope": "xs" | "s" | "m" | "l",
  "reasoning": "two sentences max",
  "questions": ["specific, answerable questions - only for clarify"],
  "hints": ["concrete next steps or likely causes - only for human_only"],
  "risk_flags": ["prod-data" | "migration" | "secrets" | "auth" | "payments" | "infra" | "ux-decision"],
  "acceptance_criteria": ["what 'done' means, inferred from the ticket"],
  "plan": ["ordered implementation steps - only for execute"]
}"""


def _build_prompt(task: Task, comments: list[Comment], repo: dict[str, Any] | None) -> str:
    thread = "\n".join(
        f"[{'AI' if c.is_bot else c.author}] {c.text.strip()}" for c in comments[-20:]
    ) or "(no comments)"
    repo_note = (
        f"Target repository: {repo['key']} ({repo.get('language', 'unknown stack')})\n"
        f"Notes: {repo.get('notes', '-')}\n" if repo else
        "No repository is mapped to this list. That alone forces 'clarify'.\n"
    )
    return (
        f"{SYSTEM}\n\n=== TICKET ===\n{task.as_prompt_block()}\n"
        f"=== REPO ===\n{repo_note}\n=== COMMENT THREAD ===\n{thread}\n\n"
        "Return the JSON object now."
    )


def _parse(res: AgentResult) -> dict[str, Any]:
    text = res.text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in triage output: {text[:300]}")
    return json.loads(text[start:end + 1])


def hard_rule_violation(task: Task, patterns: list[str]) -> str | None:
    blob = f"{task.title}\n{task.description}".lower()
    for pat in patterns:
        if re.search(pat, blob):
            return pat
    return None


def triage(agent: CodingAgent, task: Task, comments: list[Comment],
           repo: dict[str, Any] | None, policy: dict[str, Any]) -> tuple[TriageResult, float]:
    patterns = policy.get("denylist_patterns") or DEFAULT_DENYLIST
    hit = hard_rule_violation(task, patterns)
    if hit:
        return TriageResult(
            decision="human_only", confidence=1.0, scope="?",
            reasoning=f"Blocked by policy rule /{hit}/ — this class of change is never automated.",
            hints=["A human must scope and verify this change."],
            risk_flags=["policy-denylist"],
        ), 0.0

    res = agent.reason(_build_prompt(task, comments, repo))
    if not res.ok:
        raise RuntimeError(f"triage agent failed: {res.error}")
    data = _parse(res)

    out = TriageResult(
        decision=str(data.get("decision", "clarify")),
        confidence=float(data.get("confidence", 0.0)),
        scope=str(data.get("scope", "m")),
        reasoning=str(data.get("reasoning", "")),
        questions=[str(x) for x in data.get("questions", [])],
        hints=[str(x) for x in data.get("hints", [])],
        risk_flags=[str(x) for x in data.get("risk_flags", [])],
        plan=[str(x) for x in data.get("plan", [])],
        acceptance_criteria=[str(x) for x in data.get("acceptance_criteria", [])],
        repo_key=repo["key"] if repo else None,
    )

    # Post-conditions the model does not get to override.
    min_conf = float(policy.get("min_execute_confidence", 0.75))
    if out.decision == "execute":
        if repo is None:
            out.decision, out.reasoning = "clarify", "No repository mapped to this list."
            out.questions = out.questions or ["Which repository should this change land in?"]
        elif out.confidence < min_conf:
            out.decision = "clarify"
            out.questions = out.questions or [
                "Could you confirm the expected behaviour and acceptance criteria?"]
        elif out.scope == "l" and policy.get("block_large_scope", True):
            out.decision = "clarify"
            out.questions = out.questions or [
                "This looks large. Can it be split into smaller, independently shippable tickets?"]
    return out, res.cost_usd
