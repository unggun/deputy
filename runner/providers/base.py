"""Task-manager abstraction. Implement this to add Jira / Linear / Asana."""
from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable

from ..models import AIState, Attachment, Comment, Task


class ProviderAuthError(RuntimeError):
    """The task manager refused our credential (revoked, expired, deactivated).

    Raised instead of a plain RuntimeError so the tick can fail the whole
    source at once, record it for `deputy status`, and never retry: every
    other call this tick would be refused the same way.
    """


class TaskProvider(ABC):
    @abstractmethod
    def whoami(self) -> str:
        """Return the provider user id the runner acts as."""

    @abstractmethod
    def fetch_candidates(self) -> Iterable[Task]:
        """Tasks assigned to me, in the configured 'ready' status.

        May be a generator: the pipeline stops iterating at its per-run cap,
        so a provider that must read each candidate separately reads only
        the ones triage reaches.
        """

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

    # Optional. Called once at the start of every tick, before any row is
    # touched. Raise ProviderAuthError if the credential is refused, so the
    # tick fails before it spends a retry, claims a comment or commits a state
    # it cannot push. Providers without a cheap check leave this a no-op.
    def check_auth(self) -> None:
        return None

    # Optional. The ticket identifier that goes into the branch name. Override
    # when the provider's integration only recognises the id in a decorated
    # form (ClickUp wants `CU-<id>`); `task.key` is right for everyone else.
    def branch_key(self, task: Task) -> str:
        return task.key

    # Optional. Providers that expose a native dependency graph should override.
    # Return [{"key": str, "closed": bool}, ...] for tickets this one waits on.
    def blocked_by(self, task_id: str) -> list[dict[str, Any]]:
        return []

    def reset_hint(self, state: AIState) -> str:
        """How a person hands a ticket parked at `state` back to the runner.

        Finishes the sentence "...and ___; I'll re-triage". Bot comments quote
        it, so it must name something a person can do in this task manager's
        own UI.
        """
        return "set AI State back to `new`"

    def download_attachment(self, task: Task, attachment: Attachment,
                            dest: Path, max_bytes: int) -> None:
        """Write one of `task.attachments` to `dest`, refusing past `max_bytes`.

        Raise on any failure; the caller logs it and goes on without that
        file. Only called for attachments the provider itself put on the
        Task, so a provider that lists none never needs to implement it.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot download attachments")


def write_capped(chunks: Iterable[bytes], dest: Path, max_bytes: int) -> None:
    """Stream `chunks` into `dest`, all or nothing.

    Written to a temporary sibling and renamed, so an interrupted or
    oversized download never leaves a truncated image that the next run
    would mistake for the real one and hand to the agent. The temporary
    file is unique per call: two repos' rows on one ticket share the
    attachment folder and may download the same image at once.
    """
    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part",
                                dir=dest.parent)
    tmp = Path(name)
    written = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            for chunk in chunks:
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"over the {max_bytes} byte attachment cap")
                fh.write(chunk)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
