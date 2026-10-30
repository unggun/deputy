"""Scorers: a rubric, a judge, and a verdict on one finished agent run.

A scorer is a markdown file in `runner.scorers_dir` (default
`~/.deputy/scorers`) with YAML frontmatter naming the stations it judges, its
labels and their scores, and how many runs to sample; the body is the rubric.
No directory, or an empty one, means scoring is off -- nothing here runs and
nothing is spent.

The judge is `CodingAgent.reason()`: tool-less, one turn, and isolated the
same way triage is, with `JUDGE_SYSTEM_PROMPT` in place of the triage one. It
reads a condensed transcript of the run from the run's own JSONL log, never
the worktree. A judge that could open files would be grading the repository
as it is now, not the run as it happened.

Three rules shape everything below:

* **Never cut the run in half.** A transcript that does not fit is clipped
  per message, harder at each level, and if even the tightest level does not
  fit the verdict is `insufficient_evidence` with no agent call. Dropping the
  middle of a run would hand the judge a confident-looking story with the
  decisive part missing -- the same reason `_run()` refuses an oversize
  prompt rather than truncating it.
* **`insufficient_evidence` is always a label.** A judge forced to pick
  between "passed" and "failed" on a transcript that shows neither invents
  an answer. It has no score, so it never moves a pass rate.
* **A broken judge is `error`, not a fail.** An unparseable reply or an
  agent failure records `error` with no score, for the same reason.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import yaml

from .jsonio import parse_json_object
from .models import Spend, agent_log_name, run_outcome
from .redact import redact

log = logging.getLogger(__name__)

DEFAULT_DIR = "~/.deputy/scorers"

# Stations whose runs leave a transcript. Triage has no log (`reason()` does
# not stream), so there is nothing for a judge to read.
STATIONS = ("plan", "implement", "review", "fix")

INSUFFICIENT = "insufficient_evidence"
ERROR = "error"
_RESERVED = (INSUFFICIENT, ERROR)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Used when the agent does not declare a prompt ceiling (a test fake). The
# real adapter's `max_prompt_bytes` is the one that applies.
DEFAULT_PROMPT_BYTES = 120_000

JUDGE_SYSTEM_PROMPT = (
    "You grade the finished work of an autonomous coding agent against one "
    "rubric. You have no tools and no access to any files, repository, shell "
    "or environment. Judge only from the transcript and context in the user's "
    "message. When the transcript does not show enough to decide, say so with "
    "the insufficient_evidence label instead of guessing. Reply in the format "
    "the message asks for.")

# (assistant text, tool input, tool result, final result) per clip level, in
# characters. Each level clips every message harder; none drops a message.
# Level 2 keeps only the size and error flag of a tool result -- by then the
# calls the agent made, in order, are what is left to judge.
CLIP_LEVELS = ((2000, 600, 800, 6000),
               (800, 300, 300, 4000),
               (300, 150, 0, 2000))


@dataclass
class Scorer:
    name: str
    stations: list[str]
    labels: dict[str, float]
    rubric: str
    pass_score: float
    sample_rate: int = 100
    repos: list[str] = field(default_factory=list)
    model: str | None = None
    path: Path | None = None

    def judges(self, station: str) -> bool:
        """Does this scorer apply to a run logged under `station`?

        `station` is the event's kind (`implement`), not its numbered log
        name (`implement-2`): a scorer for implement runs judges every pass.
        """
        return station in self.stations


@dataclass
class Verdict:
    label: str
    reasoning: str
    score: float | None = None
    passed: bool | None = None
    # None when no agent ran: an oversize or empty transcript is decided
    # without spending anything.
    spend: Spend | None = None

    @classmethod
    def of(cls, sc: Scorer, label: str, reasoning: str,
           spend: Spend | None = None) -> "Verdict":
        if label not in sc.labels:
            return cls(label=label, reasoning=reasoning, spend=spend)
        score = sc.labels[label]
        return cls(label=label, reasoning=reasoning, score=score,
                   passed=score >= sc.pass_score, spend=spend)


# --- loading ------------------------------------------------------------
def parse(text: str, path: Path | None = None) -> Scorer:
    """One scorer file. Raises ValueError with a sentence an operator can fix."""
    if not text.startswith("---"):
        raise ValueError("no YAML frontmatter (the file must start with ---)")
    _, sep, rest = text.partition("\n")
    head, sep, body = rest.partition("\n---")
    if not sep:
        raise ValueError("frontmatter is not closed with ---")
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    rubric = body.partition("\n")[2].strip()

    name = str(meta.get("name") or (path.stem if path else "")).strip()
    if not _NAME.match(name):
        raise ValueError(f"name {name!r} must be lowercase letters, digits, "
                         f"- or _ (at most 64)")
    stations = meta.get("stations")
    if isinstance(stations, str):
        stations = [stations]
    if not stations or not isinstance(stations, list):
        raise ValueError(f"stations is required: any of {', '.join(STATIONS)}")
    stations = [str(s).strip() for s in stations]
    unknown = [s for s in stations if s not in STATIONS]
    if unknown:
        raise ValueError(f"unknown station(s) {', '.join(unknown)}; triage has "
                         f"no transcript, so only {', '.join(STATIONS)} can be "
                         f"scored")

    raw = meta.get("labels")
    if not isinstance(raw, dict) or len(raw) < 2:
        raise ValueError("labels must map at least two label names to scores")
    labels: dict[str, float] = {}
    for label, score in raw.items():
        label = str(label).strip()
        if label in _RESERVED:
            raise ValueError(f"{label} is reserved and always available")
        if not _NAME.match(label):
            raise ValueError(f"label {label!r} must be lowercase letters, "
                             f"digits, - or _")
        try:
            value = float(score)
        except (TypeError, ValueError):
            raise ValueError(f"label {label} needs a numeric score") from None
        if not 0 <= value <= 1:
            raise ValueError(f"label {label} score {value} is outside 0..1")
        labels[label] = value

    # Unset means only the best label passes. A three-level rubric whose
    # middle label quietly counted as a pass would overstate every rate.
    pass_score = float(meta.get("pass_score", max(labels.values())))
    if not any(v >= pass_score for v in labels.values()):
        raise ValueError(f"pass_score {pass_score} is above every label's "
                         f"score, so nothing could pass")
    try:
        rate = int(meta.get("sample_rate", 100))
    except (TypeError, ValueError):
        raise ValueError("sample_rate must be a whole percentage") from None
    if not 0 <= rate <= 100:
        raise ValueError("sample_rate must be between 0 and 100")
    if not rubric:
        raise ValueError("the rubric (the text after the frontmatter) is empty")
    repos = meta.get("repos") or []
    if isinstance(repos, str):
        repos = [repos]
    return Scorer(name=name, stations=stations, labels=labels, rubric=rubric,
                  pass_score=pass_score, sample_rate=rate,
                  repos=[str(r) for r in repos],
                  model=(str(meta["model"]) if meta.get("model") else None),
                  path=path)


def load(directory: Path | str | None) -> tuple[list[Scorer], list[str]]:
    """Every scorer in `directory`, and one error line per file that failed.

    A bad file is skipped, not fatal: one typo'd rubric must not stop the
    others, or the tick. `doctor` reports the errors, so they are not silent.
    """
    if not directory:
        return [], []
    root = Path(str(directory)).expanduser()
    if not root.is_dir():
        return [], []
    found: list[Scorer] = []
    errors: list[str] = []
    for path in sorted(root.glob("*.md")):
        try:
            sc = parse(path.read_text(encoding="utf-8"), path)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if any(s.name == sc.name for s in found):
            errors.append(f"{path.name}: another file already defines "
                          f"scorer {sc.name}")
            continue
        found.append(sc)
    return found, errors


def sampled(sc: Scorer, event_id: int) -> bool:
    """Is this run in the scorer's sample?

    Deterministic: a hash of (scorer, run), not a dice roll, so a run left out
    stays out on every later tick instead of getting another chance each time,
    and a 20% sample really is 20% of runs rather than drifting upward.
    """
    if sc.sample_rate >= 100:
        return True
    digest = hashlib.sha1(f"{sc.name}:{event_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 100 < sc.sample_rate


def judgeable(ev: dict[str, Any]) -> bool:
    """A run worth judging: it has a log and its agent run did not fail.

    A crashed or timed-out run is already counted as a failure on the Runs
    tab; spending a judge on it buys nothing a rubric can add.
    """
    return bool(ev.get("log")) and not run_outcome(ev["kind"], ev.get("detail"))[1]


def log_path(log_root: Path, row: dict[str, Any], log_name: str) -> Path:
    """Where the run's transcript is. Built here rather than in `pipeline.py`,
    whose own count of `agent_log_name(` call sites a smoke test pins."""
    return log_root / agent_log_name(row.get("task_key") or "",
                                     row.get("repo_key") or "", log_name)


# --- transcript ---------------------------------------------------------
def _clip(text: str, n: int) -> str:
    """Head and tail of `text`, at most about `n` characters, marked."""
    text = text.strip()
    if len(text) <= n:
        return text
    if n <= 0:
        return f"[{len(text)} chars]"
    head, tail = n * 2 // 3, n // 3
    return (f"{text[:head]} …[{len(text) - head - tail} chars omitted]… "
            f"{text[-tail:]}" if tail else f"{text[:head]} …")


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def condense(path: Path, level: int = 0) -> str:
    """The run as a judge reads it: what the agent said, called and got back.

    Read from the JSONL event log `_EventLog` wrote, which is already
    redacted. Every message is kept, in order; only its length changes with
    `level`. The run's own markers (timeout, truncated log) are kept verbatim,
    because they are exactly what a judge must not mistake for a clean end.
    """
    text_n, input_n, result_n, final_n = CLIP_LEVELS[min(level, len(CLIP_LEVELS) - 1)]
    out: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        kind = rec.get("type")
        if kind == "timeout":
            out.append(f"[RUN TIMED OUT after {rec.get('after_s')}s; the "
                       f"process group was killed]")
        elif kind == "log_truncated":
            out.append("[LOG TRUNCATED: recording stopped here; the run "
                       "continued unrecorded]")
        elif kind == "run_failed":
            out.append(f"[RUN FAILED TO START: {_clip(str(rec.get('error')), 300)}]")
        elif kind == "output" and rec.get("stream") == "stderr":
            text = str(rec.get("text") or "").strip()
            if text:
                out.append(f"STDERR: {_clip(text, max(result_n, 120))}")
        elif kind == "output":
            out.extend(_stream_line(str(rec.get("text") or ""),
                                    text_n, input_n, result_n, final_n))
    return "\n".join(out)


def _stream_line(text: str, text_n: int, input_n: int, result_n: int,
                 final_n: int) -> list[str]:
    try:
        ev = json.loads(text)
    except ValueError:
        return []
    if not isinstance(ev, dict):
        return []
    kind = ev.get("type")
    content = (ev.get("message") or {}).get("content") if kind in (
        "assistant", "user") else None
    out: list[str] = []
    if kind == "assistant" and isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and str(c.get("text") or "").strip():
                out.append(f"ASSISTANT: {_clip(str(c['text']), text_n)}")
            elif c.get("type") == "tool_use":
                args = json.dumps(c.get("input") or {}, ensure_ascii=False)
                out.append(f"TOOL CALL {c.get('name')}: {_clip(args, input_n)}")
    elif kind == "user" and isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                body = _result_text(c.get("content"))
                tag = "TOOL RESULT (error)" if c.get("is_error") else "TOOL RESULT"
                out.append(f"{tag}: {_clip(body, result_n)}" if result_n
                           else f"{tag}: [{len(body)} chars]")
    elif kind == "result":
        out.append(f"FINAL ({ev.get('subtype')}, {ev.get('num_turns')} turns): "
                   f"{_clip(str(ev.get('result') or ''), final_n)}")
    return out


# --- prompt and verdict -------------------------------------------------
def _bullets(items: Any) -> str:
    if isinstance(items, str):
        items = [items]
    return "\n".join(f"- {x}" for x in (items or [])) or "(none recorded)"


def build_prompt(sc: Scorer, ev: dict[str, Any], row: dict[str, Any],
                 repo: dict[str, Any], meta: dict[str, Any],
                 transcript: str) -> str:
    """The judge's whole input.

    What the run was *asked* to do comes from the row's `meta` (the plan,
    approach and acceptance criteria triage and the analyst wrote), not from
    the ticket: the provider is never called here, and the station prompt is
    deliberately not in the log. `meta` is the row as it is now, which for
    those three fields is what it was when the run started -- they are
    written before execution and not rewritten after.
    """
    labels = "\n".join(f"- `{k}` (score {v:g})" for k, v in sc.labels.items())
    allowed = ", ".join([*sc.labels, INSUFFICIENT])
    return f"""Grade one run of an autonomous coding agent against the rubric below.

## Rubric: {sc.name}

{sc.rubric}

## Labels

Choose exactly one:
{labels}
- `{INSUFFICIENT}`: the transcript does not show enough to decide. Use this rather than guess.

## The run

Ticket: {row.get('task_key')}: {row.get('title') or ''}
Repo: {repo.get('key') or row.get('repo_key')} ({repo.get('language') or 'language not configured'})
Test command: {repo.get('test') or '(none configured)'}
Station: {ev.get('kind')}, logged as {ev.get('log')}
Recorded outcome: {ev.get('detail') or '(none)'}

### What the run was asked to do

Approach: {meta.get('approach') or '(none recorded)'}

Plan:
{_bullets(meta.get('plan'))}

Acceptance criteria:
{_bullets(meta.get('acceptance_criteria'))}

### Transcript

Condensed from the run's log. Long messages and tool output are clipped and marked; nothing is left out entirely.

{transcript}

## Reply

Reply with a single JSON object and nothing else:
{{"label": "<one of: {allowed}>", "reasoning": "<two or three sentences naming what in the transcript decided it>"}}
"""


def parse_verdict(text: str, sc: Scorer) -> tuple[str, str]:
    """(label, reasoning) from the judge's reply. Raises ValueError."""
    obj = parse_json_object(text)
    label = str(obj.get("label") or "").strip().strip("`").lower()
    if label not in sc.labels and label != INSUFFICIENT:
        raise ValueError(f"label {label!r} is not one of the scorer's labels")
    return label, str(obj.get("reasoning") or "").strip()[:2000]


def judge(agent: Any, sc: Scorer, ev: dict[str, Any], row: dict[str, Any],
          repo: dict[str, Any], meta: dict[str, Any], path: Path, *,
          model: str | None = None) -> Verdict:
    """Judge one run. Never raises for a judge-side failure; returns `error`."""
    budget = int(getattr(agent, "max_prompt_bytes", 0) or DEFAULT_PROMPT_BYTES)
    if not path.exists():
        return Verdict(INSUFFICIENT, f"the run's log {path.name} is gone")
    prompt = None
    for level in range(len(CLIP_LEVELS)):
        transcript = condense(path, level)
        if not transcript.strip():
            return Verdict(INSUFFICIENT, f"the run's log {path.name} has no "
                                         f"transcript in it")
        candidate = build_prompt(sc, ev, row, repo, meta, transcript)
        if len(candidate.encode("utf-8")) <= budget:
            prompt = candidate
            break
    if prompt is None:
        return Verdict(INSUFFICIENT, f"the transcript does not fit the "
                                     f"{budget} byte prompt ceiling even at "
                                     f"the tightest clip level, and a run cut "
                                     f"in half is not judged")
    res = agent.reason(prompt, model=sc.model or model,
                       system_prompt=JUDGE_SYSTEM_PROMPT)
    spend = Spend.of(res)
    if not res.ok:
        return Verdict(ERROR, f"judge run failed: {res.error}", spend=spend)
    try:
        label, reasoning = parse_verdict(res.text, sc)
    except ValueError as exc:      # json.JSONDecodeError is a ValueError
        return Verdict(ERROR, f"unparseable verdict: {str(exc)[:300]}", spend=spend)
    return Verdict.of(sc, label, reasoning, spend)


def score_run(agent: Any, store: Any, sc: Scorer, ev: dict[str, Any], *,
              log_root: Path, repo: dict[str, Any] | None,
              model: str | None = None, record: bool = True,
              secrets: Sequence[str] = ()) -> Verdict | None:
    """Judge one ledger run and, with `record`, store the verdict.

    The one path both the tick and `deputy score` take, so the CLI's verdict
    is the tick's verdict. With `record` the run is claimed first and None
    comes back when it is already claimed -- two overlapping ticks must not
    pay twice for one verdict. The judge's spend is always on the ledger,
    recorded or not: quota spent trying a rubric is still quota spent.

    The ledger line carries no task id, so scoring spend is not accrued to
    the row. A row's cost is the cost of the work; measuring it is overhead.
    """
    row = store.get(ev["task_id"]) or {}
    if record and not store.claim_score(sc.name, ev["id"], task_id=ev["task_id"],
                                        station=ev["kind"], log=ev.get("log")):
        return None
    try:
        meta = json.loads(row.get("meta") or "{}")
        verdict = judge(agent, sc, ev, row, repo or {"key": row.get("repo_key")},
                        meta, log_path(log_root, row, str(ev.get("log") or "")),
                        model=model)
    except Exception as exc:  # noqa: BLE001
        log.exception("scorer %s failed on run %s", sc.name, ev["id"])
        verdict = Verdict(ERROR, f"scoring crashed: {exc}")
    verdict.reasoning = redact(verdict.reasoning, secrets)
    if verdict.spend is not None:
        store.record("score", None,
                     f"{sc.name} {ev['task_id']} {ev.get('log')}: {verdict.label}",
                     verdict.spend.cost_usd, verdict.spend.tokens,
                     **verdict.spend.run_fields())
    if record:
        spend = verdict.spend or Spend()
        store.finish_score(sc.name, ev["id"], label=verdict.label,
                           score=verdict.score, passed=verdict.passed,
                           pass_score=sc.pass_score, reasoning=verdict.reasoning,
                           cost_usd=spend.cost_usd, tokens=spend.tokens,
                           model=",".join(spend.models) or None)
    return verdict
