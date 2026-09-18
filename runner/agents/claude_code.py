"""Claude Code headless adapter.

`reason()` uses `--output-format json`: one turn, no tools, a single JSON
object on stdout. Nothing to stream and nothing to lose.

`work()` uses `--output-format stream-json --verbose`, which emits one JSON
object per line as the run proceeds. That costs a slightly more involved
parse and buys three things a 45-minute blocking `subprocess.run` cannot:
output lands on disk as it is produced (so a run that is killed still leaves
evidence), the recording is bounded rather than held whole in memory, and the
final `type: "result"` event carries the same `result` / `total_cost_usd` /
`num_turns` / `duration_ms` / `usage` fields the blocking form returns.

Docs: https://docs.claude.com/en/docs/claude-code/overview
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

from ..models import AgentResult, TokenUsage
from ..redact import redact
from .base import CodingAgent

log = logging.getLogger(__name__)

DEFAULT_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep", "Agent",
    "Bash(git:*)", "Bash(npm:*)", "Bash(pnpm:*)", "Bash(yarn:*)",
    "Bash(go:*)", "Bash(python:*)", "Bash(pytest:*)", "Bash(make:*)",
]

# Cap on one run's event log. A runaway agent looping on a command that prints
# megabytes must not fill the disk the worktrees live on. At the cap the log
# records that it stopped and keeps draining the pipes -- see `_EventLog`.
MAX_LOG_BYTES = 32 << 20

# The prompt travels in argv, so a ticket carrying a pasted stack trace can
# push the command line past the kernel's limit -- which surfaces as a bare
# OSError from exec, at the one moment an operator most needs a sentence they
# can act on. macOS shares ~1 MB of ARG_MAX between the command line and the
# environment; Linux additionally caps any *single* argument at 128 KiB
# (MAX_ARG_STRLEN), which is the tighter bound. Both leave room for a real
# ticket: an oversize prompt means a thread or a description that wants
# trimming, not a limit that wants raising.
MAX_PROMPT_BYTES = 120_000 if sys.platform.startswith("linux") else 400_000

# Git reads these from the environment and they override the working
# directory. A runner invoked from a git hook (or from any process that set
# them) would otherwise hand the agent an environment in which `git status`
# inside the worktree reports on a *different* repository, and `commit_all`
# writes to someone else's index.
GIT_ENV_VARS = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_PREFIX", "GIT_INDEX_VERSION",
    "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
})

# Set on every child. An agent that finds this in its environment is already
# running inside a runner-managed run and must not start another one.
RUN_ID_ENV = "AI_TASK_RUNNER_RUN_ID"

# Where `reason()` runs. Triage is a tool-less classification of ticket text,
# so any repository it happens to stand in is context it must not have -- see
# `ClaudeCodeAgent.reason`. A *fixed* path rather than a fresh temp directory
# because the cwd is part of the system prompt, and a path that changes every
# call misses the prompt cache on every call.
DEFAULT_TRIAGE_DIR = "~/.ai-runner/triage"

# Flags that stop `claude` pulling in ambient context. `--allowedTools ""`
# removes the model's ability to *act*, but not the CLAUDE.md, MCP tool
# definitions, skills and settings it is handed before it is asked anything.
TRIAGE_ISOLATION = [
    "--strict-mcp-config",       # no MCP servers (none are configured for it)
    "--disable-slash-commands",  # no skills
    "--setting-sources", "",     # no user/project/local settings or CLAUDE.md
]

# How long the process group gets to exit on SIGTERM before SIGKILL. The agent
# may be mid-write in a worktree; a graceful first signal lets it finish the
# file it is on. Bounded, so a process that ignores SIGTERM cannot stall the
# tick past this.
TERM_GRACE_SECONDS = 10


def _sanitized_env(run_id: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_VARS}
    env[RUN_ID_ENV] = run_id
    return env


# Env var names whose *values* must never appear in a log or a ticket comment.
# Matching on the name (not the value) is what catches `token_env: GH_TOKEN_ACME`
# and `JIRA_API_TOKEN` — names this adapter cannot enumerate in advance.
# Substring, not word-anchored: `\b` does not match between "TOKEN" and "_" in
# `GH_TOKEN_ACME`, which is precisely the `token_env:` naming the config
# documents. The suffix exclusion below does the narrowing instead.
_SECRET_NAME = re.compile(
    r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|_PAT)")
# ...except when the name says it holds a path to the secret rather than the
# secret itself. Redacting `~/.ssh/id_ed25519` out of every message helps
# nobody.
_SECRET_NAME_EXCLUDE = re.compile(r"(?i)_(PATH|FILE|DIR|NAME|ENV|ID)$")


def _env_secrets(environ: dict[str, str] | None = None) -> list[str]:
    source = os.environ if environ is None else environ
    return [
        value for name, value in source.items()
        if _SECRET_NAME.search(name) and not _SECRET_NAME_EXCLUDE.search(name)
        and len(value or "") >= 16
    ]


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the agent *and everything it spawned*.

    `subprocess`'s own timeout kills the direct child only. Claude Code
    spawns bash tool invocations, MCP servers and subagents; those survive the
    parent, keep holding the worktree, and keep spending quota with nothing
    watching them. The child is started with `start_new_session=True`, so it
    leads its own process group and the whole tree can be signalled at once.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (AttributeError, OSError):
        # Windows has no process groups, and ESRCH means it already exited.
        proc.kill()
        return
    for sig, wait in ((signal.SIGTERM, TERM_GRACE_SECONDS), (signal.SIGKILL, 5)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return                      # whole group is gone
        except PermissionError:
            # macOS reports EPERM for a group whose leader has already been
            # reaped. Fall back to the direct child, which is harmless if it
            # is also gone.
            proc.kill()
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
    log.error("agent process group %s survived SIGKILL", pgid)


class _EventLog:
    """Append-only JSONL record of one run, written as output arrives.

    Bounded by `MAX_LOG_BYTES`. At the cap it writes one `log_truncated`
    event and then silently drops further output -- but the caller must keep
    *reading* the pipes regardless, because a child whose stdout pipe fills up
    blocks on write and never exits, turning a disk-space problem into a hung
    tick.
    """

    def __init__(self, path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self._max = max_bytes
        self._written = 0
        self._truncated = False
        self._seq = 0

    def event(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self._write({"type": kind, **fields})

    def output(self, stream: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._write({"type": "output", "stream": stream, "text": text})

    def _write(self, record: dict[str, Any]) -> None:
        if self._truncated:
            return
        self._seq += 1
        record = {"seq": self._seq, **record}
        try:
            line = json.dumps(record, ensure_ascii=False) + "\n"
        except (TypeError, ValueError):
            line = json.dumps({"seq": self._seq, "type": "unserializable"}) + "\n"
        if self._written + len(line) > self._max:
            self._truncated = True
            self._fh.write(json.dumps({
                "seq": self._seq, "type": "log_truncated",
                "message": f"stopped recording after {self._written} bytes; "
                           f"the run continues",
            }) + "\n")
            self._fh.flush()
            return
        self._fh.write(line)
        self._written += len(line)
        # Flushed per event on purpose: the whole point is that the file is
        # readable while the run is in flight and complete after a SIGKILL.
        self._fh.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except OSError:
                pass


class _NullLog:
    """Stand-in for callers that pass no `log_path`."""

    def event(self, kind: str, **fields: Any) -> None: ...
    def output(self, stream: str, text: str) -> None: ...
    def close(self) -> None: ...


class ClaudeCodeAgent(CodingAgent):
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.binary = cfg.get("binary", "claude")
        self.model = cfg.get("model")
        self.triage_model = cfg.get("triage_model") or self.model
        self.reviewer_model = cfg.get("reviewer_model") or self.model
        self.max_turns = int(cfg.get("max_turns", 60))
        self.permission_mode = cfg.get("permission_mode", "dontAsk")
        self.allowed_tools = cfg.get("allowed_tools", DEFAULT_TOOLS)
        self.max_budget_usd = cfg.get("max_budget_usd")
        self.timeout = int(cfg.get("timeout_minutes", 45)) * 60
        self.max_log_bytes = int(cfg.get("max_log_bytes", MAX_LOG_BYTES))
        self.max_prompt_bytes = int(
            cfg.get("max_prompt_bytes", MAX_PROMPT_BYTES))
        self.triage_dir = Path(str(cfg.get("triage_dir", DEFAULT_TRIAGE_DIR))
                               ).expanduser()
        self.isolate_triage = bool(cfg.get("isolate_triage", True))
        # Literal secret values to scrub from anything this adapter records.
        # Collected by env var *name*, because the interesting ones are named
        # by the developer's own config (`token_env: GH_TOKEN_ACME`) and this
        # adapter has no way to know which those are.
        self.secrets = _env_secrets()
        if shutil.which(self.binary) is None:
            raise RuntimeError(f"`{self.binary}` not found on PATH")

    def reason(self, prompt: str, *, max_turns: int = 1) -> AgentResult:
        """Tool-less classification. Judges only what the prompt contains.

        `cwd` used to be None, which meant triage inherited whatever directory
        the runner was launched from -- and `claude` loads that directory's
        `CLAUDE.md`, settings and git state into the system prompt before the
        prompt is even read. Triage therefore silently ingested an unrelated
        project's conventions, and answered about them: run from this
        repository, a "classify this ticket" prompt came back discussing this
        repository. `--allowedTools ""` never prevented that; it removes the
        ability to act, not the ambient context.

        So triage runs in a fixed empty directory with the ambient sources
        switched off. Measured on one ticket-shaped prompt, that moved a
        triage call from ~37.5k tokens to ~23k, and -- more to the point --
        from the wrong answer to the right one.
        """
        cmd = [self.binary, "-p", prompt,
               "--output-format", "json",
               "--max-turns", str(max_turns),
               "--allowedTools", "",
               "--permission-mode", "dontAsk"]
        if self.triage_model:
            cmd += ["--model", self.triage_model]
        cwd = None
        if self.isolate_triage:
            cmd += TRIAGE_ISOLATION
            cwd = self._triage_cwd()
        return self._run(cmd, cwd=cwd, timeout=300, stream=False)

    def _triage_cwd(self) -> Path | None:
        """The empty directory triage runs in, created on demand.

        Returns None if it cannot be created: an unwritable path is a reason
        to fall back to the old behaviour with a warning, not to fail triage
        and strand every new ticket.
        """
        try:
            self.triage_dir.mkdir(parents=True, exist_ok=True)
            return self.triage_dir
        except OSError as exc:
            log.warning("cannot create triage dir %s (%s); triage will run in "
                        "the launch directory and may pick up its context",
                        self.triage_dir, exc)
            return None

    def work(self, prompt: str, cwd: Path, *,
             log_path: Path | None = None,
             model: str | None = None,
             allowed_tools: list[str] | None = None) -> AgentResult:
        tools = self.allowed_tools if allowed_tools is None else allowed_tools
        chosen = model or self.model
        cmd = [self.binary, "-p", prompt,
               "--output-format", "stream-json", "--verbose",
               "--max-turns", str(self.max_turns),
               "--permission-mode", self.permission_mode,
               "--allowedTools", ",".join(tools)]
        if chosen:
            cmd += ["--model", chosen]
        if self.max_budget_usd:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        return self._run(cmd, cwd=cwd, timeout=self.timeout,
                         log_path=log_path, stream=True)

    # ------------------------------------------------------------------
    def _run(self, cmd: list[str], cwd: Path | None, timeout: int, *,
             stream: bool, log_path: Path | None = None) -> AgentResult:
        """Own the event log handle; `_execute` does the work.

        The split exists so the handle is closed on *every* path including an
        unexpected exception. `daemon` mode calls this on a loop for the life
        of the process, so a leaked descriptor is a slow outage rather than a
        one-run annoyance.
        """
        # `cmd` is always [binary, "-p", prompt, ...]; the prompt is the one
        # argument without a bound. Checked here rather than in `reason()` and
        # `work()` so a seam added later cannot skip it, and before anything is
        # opened or spawned, so an over-long prompt costs nothing.
        prompt_bytes = len(cmd[2].encode("utf-8")) if len(cmd) > 2 else 0
        if prompt_bytes > self.max_prompt_bytes:
            log.error("refusing to run: prompt is %d bytes (ceiling %d)",
                      prompt_bytes, self.max_prompt_bytes)
            return AgentResult(
                ok=False, text="",
                error=(f"prompt is {prompt_bytes} bytes, over the "
                       f"{self.max_prompt_bytes} byte ceiling -- it travels in "
                       f"argv and would fail at exec. Trim the ticket text or "
                       f"comment thread, or raise agent.max_prompt_bytes."))

        run_id = uuid.uuid4().hex[:16]
        events: _EventLog | _NullLog = _NullLog()
        if log_path:
            try:
                events = _EventLog(log_path, self.max_log_bytes)
            except OSError as exc:
                log.warning("could not open agent log %s: %s", log_path, exc)
        try:
            return self._execute(cmd, cwd, timeout, stream=stream,
                                 run_id=run_id, events=events)
        finally:
            events.close()

    def _execute(self, cmd: list[str], cwd: Path | None, timeout: int, *,
                 stream: bool, run_id: str, events: Any) -> AgentResult:
        # cmd is always [binary, "-p", prompt, ...]. Drop the prompt: it is
        # the one argument that is huge and already reproduced in the station
        # that built it.
        events.event("run_started", run_id=run_id, cwd=str(cwd) if cwd else None,
                     argv=cmd[:2] + cmd[3:], timeout_s=timeout)

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # `claude -p` reads a non-TTY stdin and *appends it to the
                # prompt*. Inheriting the runner's stdin therefore feeds the
                # agent whatever happens to be on it -- a heredoc, a pipe, a
                # terminal that then blocks forever. The prompt goes in argv;
                # nothing should arrive this way.
                stdin=subprocess.DEVNULL,
                text=True, bufsize=1,
                env=_sanitized_env(run_id),
                # Leads its own process group so `_kill_tree` can reach the
                # agent's children. POSIX only; on Windows this is ignored and
                # `_kill_tree` degrades to killing the direct child.
                start_new_session=True,
            )
        except OSError as exc:
            events.event("run_failed", error=str(exc))
            return AgentResult(ok=False, text="", error=f"could not start agent: {exc}")

        timed_out = threading.Event()

        def on_timeout() -> None:
            timed_out.set()
            events.event("timeout", after_s=timeout)
            _kill_tree(proc)

        timer = threading.Timer(timeout, on_timeout)
        timer.daemon = True
        timer.start()

        stderr_chunks: list[str] = []
        drain = threading.Thread(
            target=self._drain, args=(proc.stderr, "stderr", events, stderr_chunks),
            daemon=True)
        drain.start()

        payload: dict[str, Any] | None = None
        stdout_tail: list[str] = []
        try:
            if stream:
                payload = self._consume_stream(proc, events, stdout_tail)
            else:
                payload = self._consume_blob(proc, events, stdout_tail)
            proc.wait()
        finally:
            timer.cancel()
            # Reap anything the agent left behind even on a clean exit: a
            # backgrounded MCP server outlives its parent otherwise.
            if proc.poll() is None:
                _kill_tree(proc)
            drain.join(timeout=5)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass

        stderr_text = self._clean("".join(stderr_chunks))
        if timed_out.is_set():
            events.event("run_finished", outcome="timeout")
            return AgentResult(
                ok=False, text="", timed_out=True,
                error=f"agent timed out after {timeout}s (process group killed)")

        if payload is None:
            events.event("run_finished", outcome="unparseable",
                         exit_code=proc.returncode)
            return AgentResult(
                ok=False,
                text=self._clean("".join(stdout_tail))[-4000:],
                error=(f"unparseable agent output (exit {proc.returncode}): "
                       f"{stderr_text[-800:]}"),
            )

        ok = proc.returncode == 0 and payload.get("subtype") != "error"
        self._check_model(cmd, payload)
        usage = TokenUsage.from_payload(payload)
        events.event("run_finished", outcome="ok" if ok else "failed",
                     exit_code=proc.returncode, cost_usd=payload.get("total_cost_usd"),
                     tokens=usage.total)
        return AgentResult(
            ok=ok,
            text=str(payload.get("result", "")),
            cost_usd=float(payload.get("total_cost_usd") or 0.0),
            num_turns=int(payload.get("num_turns") or 0),
            duration_ms=int(payload.get("duration_ms") or 0),
            usage=usage,
            error=None if ok else self._clean(
                str(payload.get("result") or stderr_text))[:1000],
            raw=payload,
        )

    def _check_model(self, cmd: list[str], payload: dict[str, Any]) -> None:
        """`--model` is a request; `modelUsage` reports what actually answered.

        Warn and continue, never fail. A `work()` run legitimately reports
        several models -- `allowed_tools` includes `Agent`, and subagents bill
        separately -- so the test is that the requested model is *present*,
        not that it ran alone. An absent or unrecognised `modelUsage` stays
        silent: a schema change upstream must not warn on every run.
        """
        if "--model" not in cmd:
            return
        wanted = cmd[cmd.index("--model") + 1]
        reported = payload.get("modelUsage")
        if not isinstance(reported, dict) or not reported:
            return
        ran: set[str] = set()
        for key, entry in reported.items():
            ran.add(str(key))
            if isinstance(entry, dict) and entry.get("canonicalModel"):
                ran.add(str(entry["canonicalModel"]))
        # `--model` accepts an alias ("sonnet") as readily as a canonical id
        # ("claude-sonnet-5"), so an alias counts as present when it names
        # part of an id that ran.
        if any(wanted == r or wanted in r for r in ran):
            return
        log.warning("requested model %s but %s answered", wanted,
                    ", ".join(sorted(ran)))

    # ------------------------------------------------------------------
    def _consume_stream(self, proc: subprocess.Popen[str], events: Any,
                        tail: list[str]) -> dict[str, Any] | None:
        """Read NDJSON as it arrives; return the final `result` event.

        Every line is logged whether or not it parses, so a malformed stream
        is still diagnosable. The last `type: "result"` object wins: Claude
        Code emits exactly one, at the end.
        """
        result: dict[str, Any] | None = None
        for line in self._lines(proc.stdout):
            # Parse the original, record the scrubbed copy. Tool output in
            # this stream is whatever the agent read from the repo, which can
            # include a credential it was never meant to print.
            events.output("stdout", self._clean(line))
            self._remember(tail, line)
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") == "result":
                result = record
        return result

    def _consume_blob(self, proc: subprocess.Popen[str], events: Any,
                      tail: list[str]) -> dict[str, Any] | None:
        text = "".join(self._lines(proc.stdout))
        events.output("stdout", self._clean(text))
        self._remember(tail, text)
        try:
            record = json.loads(text or "{}")
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None

    @staticmethod
    def _lines(pipe: Any) -> Iterator[str]:
        if pipe is None:
            return
        try:
            for line in pipe:
                yield line
        except ValueError:
            # Pipe closed under us by `_kill_tree`. Whatever we already read
            # stands; the timeout path owns the outcome.
            return

    def _drain(self, pipe: Any, stream: str, events: Any,
               sink: list[str]) -> None:
        """Keep stderr moving.

        This runs on its own thread for one reason: a child that fills its
        stderr pipe blocks on write forever, and the main thread is busy
        reading stdout. Draining is not optional even when nobody wants the
        bytes.
        """
        for line in self._lines(pipe):
            events.output(stream, self._clean(line))
            self._remember(sink, line)

    @staticmethod
    def _remember(sink: list[str], text: str, keep: int = 200) -> None:
        """Hold only the tail. The event log has the whole story; this is for
        the error message, which is capped at a few hundred bytes anyway."""
        sink.append(text)
        if len(sink) > keep:
            del sink[:-keep]

    def _clean(self, text: str) -> str:
        return redact(text, self.secrets)
