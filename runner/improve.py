"""Self-improvement proposals: what keeps going wrong in a repo, written down
for a human to accept.

The brain learns only from approvals: `review.py` writes `brain_notes` on an
approving review and nothing else ever does. The richer signal is the
opposite one, the scorer verdicts that failed and the reviews that asked for
changes. This module turns one repo's recent failures into at most one
proposed brain note, cites the failing runs as its evidence, and changes
nothing until a human runs `deputy improve accept`.

The run is the analyst's seam: `work()` with the read-only planner tools, in a
detached checkout of the repo's base branch, so it can check a claim against
the code and see whether `CLAUDE.md` already says it.

Four rules shape everything below:

* **Propose, never apply.** A run writes a `pending` row. Only `accept()`
  writes the brain, and only through `brain.append()` and its size cap.
* **Every proposal cites its evidence.** A proposal that names no ref it was
  shown is recorded as no proposal.
* **Evidence is spent once.** What a run was shown is never shown to a later
  run, whatever came of it, so a quiet repo costs nothing and a rejected idea
  is not re-derived from the same failures. A run that failed (`error`)
  spends no evidence; the tick leaves that repo alone for a day instead.
* **Rejections are remembered.** Every rejected note and its reason go into
  every later prompt for the repo.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import brain, scorers, vcs
from .analyst import PLANNER_TOOLS
from .jsonio import StationError, parse_json_object, str_list
from .models import Spend, agent_log_name
from .redact import redact

log = logging.getLogger(__name__)

# Off until configured, like scoring: an existing install that upgrades must
# not start spending on review findings it already has.
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "min_evidence": 5,       # uncited failures on one repo before a run
    "max_age_days": 7,       # ...or the oldest of them is this old
    "lookback_days": 30,     # older failures are not evidence any more
    "max_per_run": 1,        # improve runs per tick, per source
    "max_evidence": 20,      # failures shown to one run, oldest first
}
# A run that failed leaves its evidence unspent; the tick waits this long
# before trying that repo again, so a broken setup costs one run a day.
RETRY_AFTER_ERROR = 86400
# A note is one line of the brain, injected into every station prompt.
NOTE_MAX = 500
# A condensed transcript longer than this is left out of the evidence; the
# judge's reasoning, which is the point of a score item, stays.
TRANSCRIPT_MAX = 6000
FINDING_MAX = 600
FINDINGS_PER_RUN = 12

SYSTEM = f"""You improve an automated coding pipeline by writing down what it
keeps getting wrong in one repository.

The pipeline's agents plan, implement and review tickets in this repository.
Below are its recent failures there: verdicts from scorers that grade
finished runs against a rubric, and blocking findings from the pipeline's own
reviewer. Your working directory is a read-only checkout of the repository's
base branch.

You may propose at most one note for the repository's brain: a short file of
durable, repo-level facts injected into every future plan, implementation and
review prompt for this repository. A good note would have prevented several
of these failures: the test command and the env var it needs, a convention
the implementer keeps missing, two files that must always change together.

Rules:
- Look for a cause shared by at least two pieces of evidence. One failure is
  an anecdote, unless it names a fact so durable and costly that writing it
  down is plainly worth it.
- Verify in the checkout before you write. Read the files, grep for the
  convention, check the git log. Never write a claim you did not confirm.
- Durable and repo-level only: nothing about one ticket, branch or PR.
- Do not repeat what the brain already says, what a pending proposal already
  proposes, or anything a human rejected below. Read the rejection reasons.
- If CLAUDE.md or the README already says it and the agents still miss it,
  the note should point at that file rather than restate it.
- One or two sentences, at most {NOTE_MAX} characters, written as an
  instruction to the agent.
- Cite only refs listed below (like "score:12" or "review:40"): the ones the
  note addresses.
- Proposing nothing is a good answer when nothing here generalises.

Reply with a SINGLE JSON object and nothing else. No markdown, no prose:
{{
  "proposal": {{"problem": "what keeps going wrong, in one paragraph",
               "solution": "why this note fixes it",
               "note": "the exact text for the brain",
               "evidence": ["score:12", "review:40"]}} or null,
  "reasoning": "what you checked, and why this note or why none"
}}"""

_RETRY_SUFFIX = (
    "\n\nYour previous reply was not a single JSON object. Reply with the JSON "
    "object only: no prose before it, no code fence around it."
)


def settings(raw: Any) -> dict[str, Any]:
    """`runner.improve` merged over the defaults, with the numbers coerced."""
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if k in DEFAULTS and v is not None})
    # A `${VAR}`-expanded or quoted "false" is a string, and bool("false") is
    # True: that would switch on a station that spends quota.
    flag = out["enabled"]
    out["enabled"] = (flag.strip().lower() in ("1", "true", "yes", "on")
                      if isinstance(flag, str) else bool(flag))
    # This runs in Pipeline.__init__, outside the per-source guard, so a typo
    # must cost a default, not every source's tick.
    for k in DEFAULTS:
        if k != "enabled":
            try:
                out[k] = max(0, int(out[k]))
            except (TypeError, ValueError):
                log.warning("runner.improve.%s=%r is not a whole number; using %d",
                            k, out[k], DEFAULTS[k])
                out[k] = DEFAULTS[k]
    return out


def claim_ttl(agent_cfg: dict[str, Any]) -> int:
    """How long a claim may be held: two attempts at the agent's timeout,
    twice over, so a live run is never taken from under itself."""
    return 4 * 60 * int(agent_cfg.get("timeout_minutes", 45) or 45)


@dataclass
class Context:
    """What one improve run needs, built by the tick and by the CLI alike."""
    agent: Any
    store: Any
    source_key: str
    log_root: Path
    work_root: Path
    brain_root: Path
    settings: dict[str, Any]
    model: str | None = None
    tools: list[str] = field(default_factory=lambda: list(PLANNER_TOOLS))
    secrets: Sequence[str] = ()
    rubrics: dict[str, str] = field(default_factory=dict)
    claim_ttl: int = 4 * 45 * 60


def due(store: Any, source_key: str, repo_key: str, now: dt.datetime,
        conf: dict[str, Any]) -> bool:
    """Is it time for an improve run on this repo?

    Enough uncited failures, or an old enough one -- the same two triggers
    Warp uses, with a bar sized for one developer's volume. A repo whose last
    run failed waits `RETRY_AFTER_ERROR` before the tick tries again.
    """
    ts = int(now.timestamp())
    items = store.improve_evidence(source_key, repo_key,
                                   ts - 86400 * conf["lookback_days"])
    if not items:
        return False
    last = store.improvements(source=source_key, repo_key=repo_key, limit=1)
    if last and last[0]["status"] == "error" and last[0]["ts"] > ts - RETRY_AFTER_ERROR:
        return False
    return (len(items) >= max(1, conf["min_evidence"])
            or items[0]["ts"] <= ts - 86400 * conf["max_age_days"])


# --- evidence -----------------------------------------------------------
def _row_for(store: Any, task_id: str) -> dict[str, Any]:
    """The run's row, or what is left of it after a human reset."""
    row = store.get(task_id) or {}
    if row.get("task_key"):
        return row
    placeholder = store.get(task_id.split("#", 1)[0]) or {}
    return {"task_key": placeholder.get("task_key"), "title": placeholder.get("title"),
            "repo_key": store.repo_of(task_id)}


def render(store: Any, ev: dict[str, Any], log_root: Path) -> str | None:
    """One piece of evidence as the prompt shows it. None when there is
    nothing to show: a review whose log no longer says what it found."""
    row = _row_for(store, ev["task_id"] or "")
    if not row.get("task_key"):
        return None
    ticket = f"{row['task_key']} \"{row.get('title') or ''}\""
    path = scorers.log_path(log_root, row, str(ev.get("log") or ""))
    if ev["kind"] == "score":
        transcript = scorers.condense(path, len(scorers.CLIP_LEVELS) - 1)
        if not transcript:
            body = "(the run's log is no longer on disk)"
        elif len(transcript) > TRANSCRIPT_MAX:
            body = "(the run's transcript is too long to include; the judge's reasoning is above)"
        else:
            body = f"Transcript of that run, condensed:\n{transcript}"
        return (f"[{ev['ref']}] scorer `{ev['scorer']}` failed the {ev['log']} run on "
                f"{ticket} with label `{ev['label']}`.\n"
                f"Judge's reasoning: {ev.get('reasoning') or '(none recorded)'}\n{body}")
    final = scorers.final_text(path)
    try:
        reply = parse_json_object(final or "")
    except ValueError:
        return None
    findings = [scorers.clip(f, FINDING_MAX)
                for f in str_list(reply.get("blocking_findings"))][:FINDINGS_PER_RUN]
    summary = scorers.clip(str(reply.get("summary") or ""), FINDING_MAX)
    # A rejection of the whole approach can carry its reason in the summary
    # alone; that is still evidence. Only a reply that says nothing is not.
    if not findings and not summary:
        return None
    what = ("rejected the approach" if ev.get("verdict") == "reject"
            else "asked for changes")
    return (f"[{ev['ref']}] the reviewer {what} on {ticket} ({ev['log']}):\n"
            + "\n".join(f"- {f}" for f in findings)
            + (f"\nSummary: {summary}" if summary else ""))


def build_prompt(repo: dict[str, Any], brain_text: str, items: list[str],
                 prior: list[dict[str, Any]], rubrics: dict[str, str]) -> str:
    pending = [p for p in prior if p["status"] == "pending"]
    rejected = [p for p in prior if p["status"] == "rejected"]
    blocks = [
        SYSTEM,
        (f"=== REPO ===\n{repo.get('key')} ({repo.get('language', 'unknown stack')})\n"
         f"Base branch: {repo.get('base_branch', 'main')}\n"
         f"Test command: {repo.get('test', '(none configured)')}\n"
         f"Notes: {repo.get('notes', '-')}"),
        "=== THE BRAIN AS IT IS ===\n" + (brain_text or "(empty)"),
    ]
    if pending:
        blocks.append("=== PROPOSALS ALREADY WAITING FOR A HUMAN (do not repeat) ===\n"
                      + "\n".join(f"- {p['note']}" for p in pending))
    if rejected:
        blocks.append("=== PROPOSALS A HUMAN REJECTED (do not propose again) ===\n"
                      + "\n".join(f"- {p['note']}\n  Reason: {p['reason'] or '(none given)'}"
                                  for p in rejected))
    if rubrics:
        blocks.append("=== RUBRICS OF THE SCORERS BELOW ===\n"
                      + "\n\n".join(f"`{name}`:\n{scorers.clip(text, 1500)}"
                                    for name, text in sorted(rubrics.items())))
    blocks.append("=== EVIDENCE ===\n" + "\n\n".join(items))
    blocks.append("Return the JSON object now.")
    return "\n\n".join(blocks)


def parse_proposal(text: str, shown: set[str]) -> tuple[dict[str, Any] | None, str]:
    """(proposal or None, the run's reasoning). Raises ValueError on no JSON.

    A proposal must have a note within `NOTE_MAX` and cite at least one ref it
    was shown; otherwise it is not one, and the reason joins the reasoning.
    """
    obj = parse_json_object(text)
    reasoning = str(obj.get("reasoning") or "").strip()
    p = obj.get("proposal")
    if not isinstance(p, dict):
        return None, reasoning
    note = " ".join(str(p.get("note") or "").split())
    cited = [r for r in dict.fromkeys(str_list(p.get("evidence"))) if r in shown]
    problem = None
    if not note:
        problem = "the proposal had no note"
    elif len(note) > NOTE_MAX:
        problem = f"the note was {len(note)} characters, over the {NOTE_MAX} limit"
    elif not cited:
        problem = "the proposal cited none of the evidence it was shown"
    if problem:
        return None, f"{reasoning} [Not recorded as a proposal: {problem}.]".strip()
    return {"problem": str(p.get("problem") or "").strip(),
            "solution": str(p.get("solution") or "").strip(),
            "note": note, "cited": cited}, reasoning


# --- the run --------------------------------------------------------------
def run_for_repo(ctx: Context, repo: dict[str, Any], env: dict[str, str],
                 now: dt.datetime) -> dict[str, Any] | None:
    """One improve run on one repo, start to finish, and its stored row.

    None when there is nothing to look at or another run holds the repo. The
    caller decides whether it is due and checks the daily limits first; the
    CLI's `deputy improve run` skips the first check on purpose, never the
    second. Never raises for a station failure: that is recorded as `error`.
    """
    key = str(repo["key"])
    since = int(now.timestamp()) - 86400 * ctx.settings["lookback_days"]
    if not ctx.store.improve_evidence(ctx.source_key, key, since):
        return None
    iid = ctx.store.claim_improvement(ctx.source_key, key, ttl=ctx.claim_ttl)
    if iid is None:
        return None
    spend: Spend | None = None
    log_name = f"improve/{agent_log_name(ctx.source_key, key, f'improve-{iid}')}"
    looked: list[str] = []          # spent by this run, rendered or not
    result: dict[str, Any] = {}
    try:
        # Read after the claim, so what this run is shown is exactly what no
        # other run can be shown at the same time.
        evidence = ctx.store.improve_evidence(ctx.source_key, key, since)
        evidence = evidence[:ctx.settings["max_evidence"] or None]
        if not evidence:
            ctx.store.finish_improvement(iid, "abandoned",
                                         reasoning="nothing left to look at")
            return None
        # All of them, however many: a rejection dropped from the prompt and
        # the duplicate check below is one the next run can propose again.
        prior = ctx.store.improvements(source=ctx.source_key, repo_key=key,
                                       statuses=("pending", "rejected"), limit=None)
        brain_file = brain.brain_path(ctx.brain_root, ctx.source_key, key)
        brain_text = brain.read(brain_file)
        rubrics = {ev["scorer"]: ctx.rubrics[ev["scorer"]] for ev in evidence
                   if ev["kind"] == "score" and ev["scorer"] in ctx.rubrics}
        budget = int(getattr(ctx.agent, "max_prompt_bytes", 0)
                     or scorers.DEFAULT_PROMPT_BYTES) - len(_RETRY_SUFFIX.encode())
        items: list[str] = []
        shown: set[str] = set()
        full = False
        for ev in evidence:
            text = render(ctx.store, ev, ctx.log_root)
            if text is None:
                looked.append(ev["ref"])
                continue
            # Whole items or none: a later run gets the ones that do not fit.
            if len(build_prompt(repo, brain_text, items + [text], prior,
                                rubrics).encode()) > budget:
                full = True
                break
            items.append(text)
            shown.add(ev["ref"])
            looked.append(ev["ref"])
        if not items and full:
            result = {"status": "error", "evidence": looked, "reasoning": (
                f"not even one piece of evidence fits the prompt ceiling beside the "
                f"brain and the {len(prior)} pending or rejected notes; prune the "
                f"brain, or raise agent.max_prompt_bytes")}
        elif not items:
            result = {"status": "none", "evidence": looked, "reasoning":
                      "none of the evidence could be read back from its run's log"}
        else:
            prompt = build_prompt(repo, brain_text, items, prior, rubrics)
            wt = vcs.ensure_review_worktree(repo, str(repo.get("base_branch", "main")),
                                            ctx.work_root, env=env)
            spend = Spend()
            reply = None
            for attempt in (1, 2):
                res = ctx.agent.work(prompt if attempt == 1 else prompt + _RETRY_SUFFIX,
                                     wt, log_path=ctx.log_root / log_name,
                                     model=ctx.model, allowed_tools=ctx.tools)
                spend += Spend.of(res)
                if not res.ok:
                    raise StationError(f"improve agent failed: {res.error}", spend)
                try:
                    reply = parse_proposal(res.text, shown)
                    break
                except ValueError:
                    log.warning("improve returned unparseable output (attempt %d)", attempt)
            if reply is None:
                raise StationError("improve returned no JSON object after a retry", spend)
            proposal, reasoning = reply
            result = {"evidence": looked, "reasoning": reasoning}
            if proposal and brain.has_note(brain_file, proposal["note"]):
                result.update(status="none", reasoning=f"{reasoning} [Not recorded: "
                              "the brain already has this note.]".strip())
            elif proposal and any(" ".join(str(p["note"]).split()).casefold()
                                  == proposal["note"].casefold() for p in prior):
                result.update(status="none", reasoning=f"{reasoning} [Not recorded: "
                              "the same note is already pending or rejected.]".strip())
            elif proposal:
                result.update(status="pending", problem=proposal["problem"],
                              solution=proposal["solution"], note=proposal["note"],
                              cited=proposal["cited"])
            else:
                result["status"] = "none"
    except Exception as exc:  # noqa: BLE001
        log.exception("improve run %s on %s failed", iid, key)
        spend = getattr(exc, "spend", spend)
        result = {"status": "error", "reasoning": f"improve run failed: {exc}",
                  "evidence": looked}
    for k in ("problem", "solution", "note", "reasoning"):
        if result.get(k):
            result[k] = redact(str(result[k]), ctx.secrets)
    if spend is not None:
        ctx.store.record("improve", None, f"{ctx.source_key}:{key} #{iid}: "
                         f"{result['status']}", spend.cost_usd, spend.tokens,
                         log=log_name, **spend.run_fields())
        result.update(cost_usd=spend.cost_usd, tokens=spend.tokens,
                      model=",".join(spend.models) or None, log=log_name)
    ctx.store.finish_improvement(iid, result.pop("status"), **result)
    return ctx.store.improvement(iid)


def describe(store: Any, ref: str) -> str:
    """One evidence ref as a line a human can follow back to the run."""
    kind, _, num = ref.partition(":")
    try:
        n = int(num)
    except ValueError:
        return ref
    if kind == "score":
        s = store.score(n)
        if not s:
            return f"{ref}  (verdict no longer recorded)"
        row = _row_for(store, s["task_id"] or "")
        return (f"{ref}  {s['scorer']}: {s['label']} on {row.get('task_key') or s['task_id']} "
                f"{row.get('repo_key') or ''} {s['log']} (run {s['event_id']})")
    if kind == "review":
        ev = store.event(n)
        if not ev:
            return f"{ref}  (run no longer recorded)"
        row = _row_for(store, ev["task_id"] or "")
        return (f"{ref}  reviewer: {ev['detail']} on {row.get('task_key') or ev['task_id']} "
                f"{row.get('repo_key') or ''} {ev['log']} (run {ev['id']})")
    return ref


# --- decisions ------------------------------------------------------------
def accept(store: Any, improvement_id: int, brain_root: Path, *,
           max_bytes: int = brain.DEFAULT_MAX_BYTES) -> tuple[bool, str]:
    """Write a pending proposal's note to its repo's brain. (done?, message)

    At the brain's cap nothing is written and the proposal stays pending: the
    brain refuses rather than evicts, and the human prunes it and accepts
    again. A note the brain already has is accepted without writing twice.
    """
    row = store.improvement(improvement_id)
    if row is None:
        return False, f"no proposal {improvement_id}"
    # Decide first. The update only takes a pending row, so of an accept and
    # a reject racing on one proposal exactly one wins, and the brain is only
    # written by the accept that did.
    if not store.decide_improvement(improvement_id, "accepted"):
        now = (store.improvement(improvement_id) or row)["status"]
        return False, f"proposal {improvement_id} is {now}, not pending"
    path = brain.brain_path(brain_root, row["source"], row["repo_key"])
    note = str(row["note"] or "")
    try:
        if brain.has_note(path, note):
            return True, f"accepted; {path} already had this note"
        # Empty either at the cap or because another writer added the same
        # note between the check above and the brain's lock.
        present = bool(brain.append(path, [note], max_bytes=max_bytes)
                       or brain.has_note(path, note))
    except OSError as exc:
        # The decision is already committed; a brain that could not be read
        # or written must not leave it saying accepted.
        store.undecide_improvement(improvement_id, "accepted")
        return False, f"could not write {path} ({exc}); still pending"
    if not present:
        store.undecide_improvement(improvement_id, "accepted")
        return False, (f"{path} is at its {max_bytes}-byte cap, so nothing was "
                       f"written; prune it by hand and accept again")
    return True, f"accepted; added to {path}"


def reject(store: Any, improvement_id: int, reason: str | None) -> tuple[bool, str]:
    """Reject a pending proposal. The reason is shown to every later run."""
    row = store.improvement(improvement_id)
    if row is None:
        return False, f"no proposal {improvement_id}"
    if not store.decide_improvement(improvement_id, "rejected",
                                    (reason or "").strip() or None):
        return False, f"proposal {improvement_id} is {row['status']}, not pending"
    return True, "rejected; later runs on this repo will be shown it and the reason"
