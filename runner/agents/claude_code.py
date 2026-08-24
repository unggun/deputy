"""Claude Code headless adapter.

Uses `claude -p ... --output-format json`, which returns a single JSON object
containing `result`, `total_cost_usd`, `num_turns` and `duration_ms`.
Docs: https://docs.claude.com/en/docs/claude-code/overview
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..models import AgentResult
from .base import CodingAgent

log = logging.getLogger(__name__)

DEFAULT_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep", "Agent",
    "Bash(git:*)", "Bash(npm:*)", "Bash(pnpm:*)", "Bash(yarn:*)",
    "Bash(go:*)", "Bash(python:*)", "Bash(pytest:*)", "Bash(make:*)",
]


class ClaudeCodeAgent(CodingAgent):
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.binary = cfg.get("binary", "claude")
        self.model = cfg.get("model")
        # reason() and work() are different jobs: a tool-less one-turn
        # judgement call versus a 60-turn agentic run. Let them differ, and
        # fall back to the single `model` when only one is configured.
        self.triage_model = cfg.get("triage_model") or self.model
        self.max_turns = int(cfg.get("max_turns", 60))
        self.permission_mode = cfg.get("permission_mode", "dontAsk")
        self.allowed_tools = cfg.get("allowed_tools", DEFAULT_TOOLS)
        self.max_budget_usd = cfg.get("max_budget_usd")
        self.timeout = int(cfg.get("timeout_minutes", 45)) * 60
        if shutil.which(self.binary) is None:
            raise RuntimeError(f"`{self.binary}` not found on PATH")

    # ------------------------------------------------------------------
    def reason(self, prompt: str, *, max_turns: int = 1) -> AgentResult:
        # No tools at all: this is classification, not agentic work.
        cmd = [self.binary, "-p", prompt,
               "--output-format", "json",
               "--max-turns", str(max_turns),
               "--allowedTools", "",
               "--permission-mode", "dontAsk"]
        if self.triage_model:
            cmd += ["--model", self.triage_model]
        return self._run(cmd, cwd=None, timeout=300)

    def work(self, prompt: str, cwd: Path, *, log_path: Path | None = None) -> AgentResult:
        cmd = [self.binary, "-p", prompt,
               "--output-format", "json",
               "--max-turns", str(self.max_turns),
               "--permission-mode", self.permission_mode,
               "--allowedTools", ",".join(self.allowed_tools)]
        if self.model:
            cmd += ["--model", self.model]
        if self.max_budget_usd:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        return self._run(cmd, cwd=cwd, timeout=self.timeout, log_path=log_path)

    # ------------------------------------------------------------------
    def _run(self, cmd: list[str], cwd: Path | None, timeout: int,
             log_path: Path | None = None) -> AgentResult:
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(ok=False, text="", error=f"agent timed out after {timeout}s")

        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"$ {' '.join(cmd[:2])} ...\n--- stdout ---\n{proc.stdout}\n"
                f"--- stderr ---\n{proc.stderr}\n", encoding="utf-8",
            )

        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return AgentResult(
                ok=False, text=proc.stdout[-4000:],
                error=f"unparseable agent output (exit {proc.returncode}): {proc.stderr[-800:]}",
            )

        ok = proc.returncode == 0 and payload.get("subtype") != "error"
        return AgentResult(
            ok=ok,
            text=str(payload.get("result", "")),
            cost_usd=float(payload.get("total_cost_usd") or 0.0),
            num_turns=int(payload.get("num_turns") or 0),
            duration_ms=int(payload.get("duration_ms") or 0),
            error=None if ok else str(payload.get("result", proc.stderr))[:1000],
            raw=payload,
        )
