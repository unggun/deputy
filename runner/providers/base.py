"""Task-manager abstraction. Implement this to add Jira / Linear / Asana."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import AIState, Comment, Task


class TaskProvider(ABC):
    @abstractmethod
    def whoami(self) -> str:
        """Return the provider user id the runner acts as."""

    @abstractmethod
    def fetch_candidates(self) -> list[Task]:
        """Tasks assigned to me, in the configured 'ready' status."""

    @abstractmethod
    def get_task(self, task_id: str) -> Task:
        ...

    @abstractmethod
    def list_comments(self, task_id: str) -> list[Comment]:
        ...

    @abstractmethod
    def add_comment(self, task_id: str, markdown: str) -> str:
        ...

    @abstractmethod
    def set_ai_state(self, task_id: str, state: AIState) -> None:
        ...

    @abstractmethod
    def set_status(self, task_id: str, status: str) -> None:
        ...

    # Optional. The ticket identifier that goes into the branch name. Override
    # when the provider's integration only recognises the id in a decorated
    # form (ClickUp wants `CU-<id>`); `task.key` is right for everyone else.
    def branch_key(self, task: Task) -> str:
        return task.key

    # Optional. Providers that expose a native dependency graph should override.
    # Return [{"key": str, "closed": bool}, ...] for tickets this one waits on.
    def blocked_by(self, task_id: str) -> list[dict[str, Any]]:
        return []
