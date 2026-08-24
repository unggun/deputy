"""Git worktree + GitHub (via the `gh` CLI) helpers.

Worktrees keep the agent completely out of the developer's own checkout.
`gh` is used instead of the REST API because it handles auth, enterprise
hosts and pagination for us, and works identically on mac/Linux/Windows.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    pass


def run(cmd: list[str], cwd: Path | None = None, check: bool = True,
        timeout: int = 900,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout, env=merged)
    if check and proc.returncode != 0:
        raise CommandError(f"{' '.join(cmd)} -> {proc.returncode}\n{proc.stderr[:1500]}")
    return proc


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "task"


def profile_env(profile: dict[str, Any]) -> dict[str, str]:
    """Isolate a GitHub identity into environment variables.

    Three mechanisms, in order of preference:
      * token_env   - GH_TOKEN from a named env var. Simplest, fully parallel-safe.
      * gh_config_dir - a separate `gh` config tree, each authed independently.
      * (neither)   - falls back to whatever `gh auth` is globally active.

    Never use `gh auth switch`: it mutates global state and races between runs.
    """
    env: dict[str, str] = {}
    host = str(profile.get("host", "github.com"))
    if profile.get("token_env"):
        token = os.environ.get(str(profile["token_env"]))
        if not token:
            raise CommandError(
                f"vcs_profile '{profile.get('name')}' needs env var {profile['token_env']}")
        env["GH_TOKEN"] = token
        if host != "github.com":
            env["GH_ENTERPRISE_TOKEN"] = token
    elif profile.get("gh_config_dir"):
        env["GH_CONFIG_DIR"] = str(Path(str(profile["gh_config_dir"])).expanduser())
    env["GH_HOST"] = host
    if profile.get("ssh_key"):
        key = Path(str(profile["ssh_key"])).expanduser()
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new")
    # Attribute commits to the right identity regardless of global git config.
    if profile.get("git_user_name"):
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = str(profile["git_user_name"])
    if profile.get("git_user_email"):
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = str(profile["git_user_email"])
    return env


def preflight(profiles: dict[str, dict[str, Any]] | None = None) -> list[str]:
    missing = [b for b in ("git", "gh") if shutil.which(b) is None]
    if missing:
        return missing
    for name, profile in (profiles or {"default": {}}).items():
        try:
            env = profile_env({"name": name, **profile})
        except CommandError as exc:
            missing.append(str(exc))
            continue
        res = run(["gh", "auth", "status"], check=False, env=env)
        if res.returncode != 0:
            missing.append(f"vcs_profile '{name}' is not authenticated "
                           f"(GH_CONFIG_DIR={env.get('GH_CONFIG_DIR', '-')})")
    return missing


# --- worktrees --------------------------------------------------------
def ensure_worktree(repo: dict[str, Any], branch: str, work_root: Path,
                    env: dict[str, str] | None = None) -> Path:
    """Create (or reuse) an isolated worktree for `branch`."""
    main = Path(str(repo["path"])).expanduser()
    if not (main / ".git").exists():
        main.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", repo["remote"], str(main)], env=env)

    base = repo.get("base_branch", "main")
    run(["git", "fetch", "origin", base, "--prune"], cwd=main, env=env)

    wt = work_root / repo["key"] / slug(branch, 60)
    if wt.exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)

    # `-b` is fatal if the branch already exists, which happens whenever a
    # worktree was cleaned up but its branch survived - the PR-polling path
    # re-creates a worktree for a branch that is already pushed. Never reset
    # such a branch: it carries the commits the open PR is made of.
    if branch_exists(main, branch, env=env):
        run(["git", "worktree", "add", str(wt), branch], cwd=main, env=env)
    elif remote_branch_exists(main, branch, env=env):
        run(["git", "fetch", "origin", branch], cwd=main, env=env)
        run(["git", "worktree", "add", "--track", "-b", branch, str(wt),
             f"origin/{branch}"], cwd=main, env=env)
    else:
        run(["git", "worktree", "add", "-b", branch, str(wt), f"origin/{base}"],
            cwd=main, env=env)
    return wt


def branch_exists(main: Path, branch: str, env: dict[str, str] | None = None) -> bool:
    return run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
               cwd=main, check=False, env=env).returncode == 0


def remote_branch_exists(main: Path, branch: str,
                         env: dict[str, str] | None = None) -> bool:
    out = run(["git", "ls-remote", "--heads", "origin", branch],
              cwd=main, check=False, env=env)
    return out.returncode == 0 and bool(out.stdout.strip())


def remove_worktree(repo: dict[str, Any], wt: Path,
                    env: dict[str, str] | None = None) -> None:
    main = Path(str(repo["path"])).expanduser()
    run(["git", "worktree", "remove", "--force", str(wt)], cwd=main, check=False, env=env)


# --- commit / push / PR ----------------------------------------------
def has_changes(wt: Path) -> bool:
    return bool(run(["git", "status", "--porcelain"], cwd=wt).stdout.strip())


def commit_all(wt: Path, message: str, env: dict[str, str] | None = None) -> bool:
    if not has_changes(wt):
        return False
    run(["git", "add", "-A"], cwd=wt, env=env)
    run(["git", "commit", "-m", message], cwd=wt, env=env)
    return True


def push(wt: Path, branch: str, env: dict[str, str] | None = None) -> None:
    run(["git", "push", "-u", "origin", branch], cwd=wt, env=env)


def diff_stat(wt: Path, base: str) -> str:
    return run(["git", "diff", "--stat", f"origin/{base}...HEAD"], cwd=wt).stdout.strip()


def create_pr(wt: Path, title: str, body: str, base: str, draft: bool = True,
              env: dict[str, str] | None = None) -> tuple[int, str]:
    cmd = ["gh", "pr", "create", "--base", base, "--title", title, "--body", body]
    if draft:
        cmd.append("--draft")
    url = run(cmd, cwd=wt, env=env).stdout.strip().splitlines()[-1]
    number = int(url.rstrip("/").split("/")[-1])
    return number, url


PR_FIELDS = "state,isDraft,mergedAt,reviewDecision,statusCheckRollup,url,title"


def pr_status(wt: Path, number: int, env: dict[str, str] | None = None) -> dict[str, Any]:
    out = run(["gh", "pr", "view", str(number), "--json", PR_FIELDS], cwd=wt, env=env).stdout
    data = json.loads(out)
    checks = data.get("statusCheckRollup") or []
    states = {str(c.get("conclusion") or c.get("state") or "").upper() for c in checks}
    if not checks:
        ci = "none"
    elif states & {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", ""}:
        ci = "pending"
    elif states & {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
        ci = "failing"
    else:
        ci = "passing"
    return {
        "state": data.get("state"),
        "merged": bool(data.get("mergedAt")),
        "draft": bool(data.get("isDraft")),
        "review_decision": data.get("reviewDecision"),
        "ci": ci,
        "failed_checks": [c.get("name") for c in checks
                          if str(c.get("conclusion") or "").upper()
                          in {"FAILURE", "ERROR", "TIMED_OUT"}],
        "url": data.get("url"),
    }


def failing_check_logs(wt: Path, number: int, limit: int = 6000,
                       env: dict[str, str] | None = None) -> str:
    out = run(["gh", "pr", "checks", str(number)], cwd=wt, check=False, env=env).stdout
    return out[-limit:]


def human_feedback(wt: Path, number: int, allowlist: list[str],
                   since_iso: str | None = None,
                   env: dict[str, str] | None = None) -> list[str]:
    """Review comments from allowlisted humans (never bots, never ourselves)."""
    out = run(["gh", "pr", "view", str(number), "--json", "comments,reviews"],
              cwd=wt, env=env).stdout
    data = json.loads(out)
    notes: list[str] = []
    for bucket, key in (("reviews", "body"), ("comments", "body")):
        for item in data.get(bucket, []):
            author = ((item.get("author") or {}).get("login") or "").lower()
            body = (item.get(key) or "").strip()
            created = item.get("createdAt") or item.get("submittedAt") or ""
            if not body or author.endswith("[bot]"):
                continue
            if allowlist and author not in [a.lower() for a in allowlist]:
                continue
            if since_iso and created and created <= since_iso:
                continue
            notes.append(f"@{author}: {body}")
    return notes


def mark_ready(wt: Path, number: int, env: dict[str, str] | None = None) -> None:
    run(["gh", "pr", "ready", str(number)], cwd=wt, check=False, env=env)
