"""YAML config: ${ENV_VAR} expansion, sources, repos, VCS profiles."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            got = os.environ.get(m.group(1))
            if got is None:
                raise RuntimeError(f"Config references unset env var {m.group(1)}")
            return got
        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class Source:
    """One task-manager workspace and the repos that belong to it."""
    key: str
    provider: dict[str, Any]
    repos: list[dict[str, Any]] = field(default_factory=list)

    @property
    def provider_type(self) -> str:
        return str(self.provider.get("type", "clickup"))

    def repos_for_container(self, container_id: str,
                            container_name: str) -> list[dict[str, Any]]:
        """Every repo a ticket in this container could land in.

        A ClickUp list usually maps to exactly one repo, but a Jira project
        routinely covers a backend and a frontend. Returning all the matches
        (rather than the first) is what lets triage pick between them, and
        what lets one ticket fan out to several of them.
        """
        cid, cname = str(container_id), str(container_name).lower()
        hits = []
        for repo in self.repos:
            ids = [str(x) for x in repo.get("containers", [])]
            names = [str(x).lower() for x in repo.get("container_names", [])]
            if cid in ids or cname in names:
                hits.append(repo)
        # A source with exactly one repo needs no mapping at all.
        if not hits and len(self.repos) == 1:
            return list(self.repos)
        return hits

    def repo_for_container(self, container_id: str, container_name: str) -> dict[str, Any] | None:
        """First match only. Kept for callers that genuinely want one repo."""
        hits = self.repos_for_container(container_id, container_name)
        return hits[0] if hits else None

    def repo_by_key(self, key: str) -> dict[str, Any] | None:
        return next((r for r in self.repos if r.get("key") == key), None)


@dataclass
class Config:
    data: dict[str, Any]
    source_path: Path

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path).expanduser()
        with p.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        cfg = cls(_expand(raw), p)
        cfg.validate()
        return cfg

    # --- lookups ------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        got = self.get(dotted, _MISSING)
        if got is _MISSING:
            raise RuntimeError(f"Missing required config key: {dotted}")
        return got

    def path(self, dotted: str, default: str) -> Path:
        p = Path(str(self.get(dotted, default))).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    # --- sources ------------------------------------------------------
    def sources(self, only: list[str] | None = None) -> list[Source]:
        out: list[Source] = []
        for raw in self.get("sources", []) or []:
            if raw.get("enabled") is False:
                continue
            if only and raw["key"] not in only:
                continue
            out.append(Source(key=str(raw["key"]),
                              provider=raw.get("provider", {}) or {},
                              repos=raw.get("repos", []) or []))
        return out

    # --- vcs profiles --------------------------------------------------
    def vcs_profile(self, name: str | None) -> dict[str, Any]:
        profiles = self.get("vcs_profiles", {}) or {}
        if name is None:
            name = str(self.get("vcs.default_profile", "default"))
        if name not in profiles:
            raise RuntimeError(
                f"Unknown vcs_profile '{name}'. Defined: {', '.join(profiles) or '(none)'}")
        return {"name": name, **profiles[name]}

    def profile_for_repo(self, repo: dict[str, Any]) -> dict[str, Any]:
        return self.vcs_profile(repo.get("vcs_profile"))

    # --- validation ----------------------------------------------------
    def validate(self) -> None:
        problems: list[str] = []
        keys = [s["key"] for s in self.get("sources", []) or []]
        if not keys:
            problems.append("no sources defined")
        if len(keys) != len(set(keys)):
            problems.append("duplicate source keys")
        profiles = self.get("vcs_profiles", {}) or {}
        seen_repos: set[str] = set()
        for src in self.sources():
            for repo in src.repos:
                rk = f"{src.key}/{repo.get('key')}"
                if rk in seen_repos:
                    problems.append(f"duplicate repo key {rk}")
                seen_repos.add(rk)
                pname = repo.get("vcs_profile", self.get("vcs.default_profile", "default"))
                if pname not in profiles:
                    problems.append(f"{rk}: unknown vcs_profile '{pname}'")
                if not repo.get("remote"):
                    problems.append(f"{rk}: missing remote")
        if problems:
            raise RuntimeError("Config problems:\n  - " + "\n  - ".join(problems))


_MISSING = object()
