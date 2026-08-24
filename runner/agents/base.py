"""Coding-agent abstraction. Implement this to add Codex / Cursor / Aider."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import AgentResult


class CodingAgent(ABC):
    @abstractmethod
    def reason(self, prompt: str, *, max_turns: int = 1) -> AgentResult:
        """Pure text/JSON completion with no filesystem or shell access."""

    @abstractmethod
    def work(self, prompt: str, cwd: Path, *, log_path: Path | None = None) -> AgentResult:
        """Full agentic run inside `cwd`. May edit files and run commands."""
