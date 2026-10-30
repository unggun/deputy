"""Coding-agent abstraction. Implement this to add Codex / Cursor / Aider."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import AgentResult


class CodingAgent(ABC):
    @abstractmethod
    def reason(self, prompt: str, *, max_turns: int = 1,
               model: str | None = None,
               system_prompt: str | None = None) -> AgentResult:
        """Pure text/JSON completion with no filesystem or shell access.

        `model` and `system_prompt` override the triage defaults for this one
        call. The scoring judge is the second caller: same no-tools,
        no-ambient-context contract as triage, different job.
        """

    @abstractmethod
    def work(self, prompt: str, cwd: Path, *,
             log_path: Path | None = None,
             model: str | None = None,
             allowed_tools: list[str] | None = None) -> AgentResult:
        """Full agentic run inside `cwd`. May edit files and run commands.

        `model` and `allowed_tools` override the configured defaults for this
        one call. That is what lets a single agent serve the implementer, the
        analyst (read-only tools) and the reviewer (read-only tools plus the
        repo's test command) without three adapters.

        An empty `allowed_tools` list means "no tools" and is honoured as such;
        only `None` falls back to the configured allowlist.
        """
