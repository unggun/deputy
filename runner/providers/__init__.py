"""Provider registry."""
from __future__ import annotations

from typing import Any

from .base import TaskProvider
from .clickup import ClickUpProvider
from .jira import JiraProvider

REGISTRY = {"clickup": ClickUpProvider, "jira": JiraProvider}


def build(cfg: dict[str, Any], bot_marker: str, dry_run: bool = False) -> TaskProvider:
    kind = str(cfg.get("type", "clickup"))
    if kind not in REGISTRY:
        raise RuntimeError(f"Unknown provider type '{kind}'. Known: {', '.join(REGISTRY)}")
    return REGISTRY[kind](cfg, bot_marker, dry_run)


__all__ = ["TaskProvider", "ClickUpProvider", "JiraProvider", "build", "REGISTRY"]
