"""Triage: hard policy rules first, LLM classification second."""
from __future__ import annotations

import logging
import re
from typing import Any

from .agents.base import CodingAgent
from .jsonio import StationError, parse_json_object
from .models import Comment, Spend, Task, TriageResult

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
    # Prompt-injection shapes. A ticket body is data; a ticket that tries to
    # issue instructions to the runner is by definition not a normal ticket, and
    # a human should read it before an agent does. \s+ (not literal spaces)
    # because ticket bodies wrap lines routinely — a single space would let a
    # line-wrapped "ignore all previous\ninstructions" slip straight through.
    r"\bignore\s+(all\s+)?previous\s+instructions\b",
    r"\bsystem\s+override\b",
    r"\bdisregard\s+(the\s+)?(above|prior|previous)\b",
    r"\bpush\s+(directly\s+)?to\s+main\b",
]

SYSTEM = """You are the triage stage of an automated engineering pipeline.
You classify a ticket into exactly one of three buckets. You are deliberately
conservative: when in doubt, choose "clarify". A wrong "execute" wastes an
engineer's review time and can ship a bad change; a wrong "clarify" only costs
one comment.

Choose "execute" ONLY IF ALL of these hold:
- The desired end state is unambiguous and testable.
- It is scoped to application code in the repositories listed under REPOS.
- No production data, credentials, infrastructure, auth model, or payments.
- A competent engineer new to the ticket could start without asking anything.

You also route the ticket. "repos" is the list of repositories that need a
code change for this ticket to be done. Each one gets its OWN branch, PR and
review, so name a repo only if it genuinely needs changing:
- Most tickets touch exactly one repo. Prefer one.
- Name several only when the work truly spans them - e.g. a new API endpoint
  in the backend AND the screen that calls it in the frontend.
- Use the exact `key` from the REPOS section. Never invent a key.
- If you cannot tell which repo it belongs to, choose "clarify" and ask.

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
  "plan": ["ordered implementation steps - only for execute"],
  "repos": ["repo keys that need a code change - only for execute"]
}"""


OVERRIDE_TAG_DEFAULT = "ai-override"

OVERRIDE_NOTE = """=== POLICY OVERRIDE ===
A human reviewed this ticket's match against the runner's policy keywords
and tagged it for automation anyway. Do not refuse it for mentioning
production, credentials or similar words alone; still judge whether the
actual code change is clear, scoped and doable from the repository.
"""


def _build_prompt(task: Task, comments: list[Comment],
                  repos: list[dict[str, Any]], override: bool = False) -> str:
    thread = "\n".join(
        f"[{'AI' if c.is_bot else c.author}] {c.text.strip()}" for c in comments[-20:]
    ) or "(no comments)"
    if not repos:
        repo_note = "No repository is mapped to this list. That alone forces 'clarify'.\n"
    else:
        repo_note = "\n".join(
            f"- key: {r['key']}\n"
            f"  stack: {r.get('language', 'unknown stack')}\n"
            f"  notes: {r.get('notes', '-')}"
            for r in repos
        ) + "\n"
    return (
        f"{SYSTEM}\n\n=== TICKET ===\n{task.as_prompt_block()}\n"
        f"=== REPOS ===\n{repo_note}\n=== COMMENT THREAD ===\n{thread}\n\n"
        f"{OVERRIDE_NOTE + chr(10) if override else ''}"
        "Return the JSON object now."
    )


def _pick_repos(data: dict[str, Any], repos: list[dict[str, Any]]) -> list[str]:
    """Resolve the model's `repos` against the candidates it was offered.

    The model does not get to invent a repo: anything not in `repos` is
    dropped. With a single candidate the model is not consulted at all --
    there is nothing to choose, and letting a hallucinated key empty the list
    would turn a routable ticket into a spurious `clarify`.
    """
    valid = {str(r["key"]) for r in repos}
    if len(valid) <= 1:
        return [str(r["key"]) for r in repos]
    raw = data.get("repos") or []
    if isinstance(raw, str):        # a model that answered with a bare string
        raw = [raw]
    picked = [k for k in (str(x) for x in raw) if k in valid]
    seen: set[str] = set()
    out = [k for k in picked if not (k in seen or seen.add(k))]
    if not out:
        log.warning("triage returned no usable repo from %s; offered %s", raw, sorted(valid))
    return out


def hard_rule_violation(task: Task, patterns: list[str],
                        comments: list[Comment] | None = None) -> str | None:
    """Scan ticket text and human comment text against the denylist.

    Comment text reaches the model (it's in the prompt via `_build_prompt`)
    but never the hard rule that overrides the model — a denylist phrase
    slipped into a reply could route straight to "execute". Bot comments are
    excluded: they're the runner's own plan/question posts, which legitimately
    quote the ticket back (including any denylist phrase already caught when
    the ticket itself was scanned), and scanning them would just be scanning
    our own output.
    """
    parts = [task.title, task.description]
    parts += [c.text for c in (comments or []) if not c.is_bot]
    blob = "\n".join(parts).lower()
    for pat in patterns:
        if re.search(pat, blob):
            return pat
    return None


def _match_excerpt(task: Task, pattern: str,
                   comments: list[Comment] | None = None, width: int = 40) -> str:
    """The text a denylist rule matched, with a little context either side.

    A bare regex tells the human nothing about *why*; the excerpt is what lets
    them see at a glance whether it is a real hit or a false positive.
    """
    parts = [task.title, task.description]
    parts += [c.text for c in (comments or []) if not c.is_bot]
    blob = "\n".join(parts)             # same join as hard_rule_violation
    m = re.search(pattern, blob.lower())
    if not m:
        return ""
    lo, hi = max(0, m.start() - width), min(len(blob), m.end() + width)
    if m.end() - m.start() > 3 * width:     # `.*` can span a whole line
        text = f"{blob[lo:m.start() + width]} … {blob[m.end() - width:hi]}"
    else:
        text = blob[lo:hi]
    text = " ".join(text.split())
    return f"{'…' if lo else ''}{text}{'…' if hi < len(blob) else ''}"


def triage(agent: CodingAgent, task: Task, comments: list[Comment],
           repos: list[dict[str, Any]] | dict[str, Any] | None,
           policy: dict[str, Any]) -> tuple[TriageResult, float]:
    # Callers used to pass a single repo dict (or None). Accept both shapes so
    # a stale caller routes to one repo instead of failing obscurely.
    if repos is None:
        repos = []
    elif isinstance(repos, dict):
        repos = [repos]
    patterns = policy.get("denylist_patterns") or DEFAULT_DENYLIST
    override_tag = str(policy.get("override_tag", OVERRIDE_TAG_DEFAULT)).lower()
    # The only way past the denylist: a human put this tag on the ticket after
    # reading it. The model can never waive the denylist, and neither can a
    # comment -- comment text is exactly what the denylist scans.
    override = bool(override_tag) and override_tag in task.tags
    hit = hard_rule_violation(task, patterns, comments)
    if hit and override:
        log.info("[%s] denylist /%s/ waived by the %r tag", task.key, hit, override_tag)
    elif hit:
        return TriageResult(
            decision="human_only", confidence=1.0, scope="?",
            reasoning=(f"The ticket matched a policy rule, so I stopped before "
                       f"doing anything.\n\nRule: `{hit}`\n"
                       f"Matched text: \"{_match_excerpt(task, hit, comments)}\""),
            hints=["If this is a false positive, add the "
                   f"`{override_tag}` tag and set AI State back to `new`. "
                   "I'll re-triage and always ask you to approve the plan before "
                   "writing code.",
                   "Otherwise a human needs to pick this one up."],
            risk_flags=["policy-denylist"],
        ), Spend()

    res = agent.reason(_build_prompt(task, comments, repos, override=override))
    if not res.ok:
        raise StationError(f"triage agent failed: {res.error}", Spend.of(res))
    data = parse_json_object(res.text)

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
        repo_keys=_pick_repos(data, repos),
        policy_override=bool(hit and override),
    )

    # Post-conditions the model does not get to override.
    min_conf = float(policy.get("min_execute_confidence", 0.75))
    if out.decision == "execute":
        if not out.repo_keys:
            out.decision = "clarify"
            out.reasoning = ("No repository mapped to this list." if not repos else
                             "Could not tell which repository this ticket belongs in.")
            out.questions = out.questions or ["Which repository should this change land in?"]
        elif out.confidence < min_conf:
            out.decision = "clarify"
            out.questions = out.questions or [
                "Could you confirm the expected behaviour and acceptance criteria?"]
        elif out.scope == "l" and policy.get("block_large_scope", True):
            out.decision = "clarify"
            out.questions = out.questions or [
                "This looks large. Can it be split into smaller, independently shippable tickets?"]
    return out, Spend.of(res)
