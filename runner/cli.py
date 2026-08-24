"""CLI entrypoint. Works identically on macOS, Linux and Windows."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path

from . import brain, graph, providers, vcs
from .agents.claude_code import ClaudeCodeAgent
from .config import Config
from .pipeline import Pipeline
from .state import Store

BOT_MARKER_DEFAULT = "<!-- ai-runner -->"


def _logging(cfg: Config, verbose: bool) -> None:
    log_dir = cfg.path("runner.log_dir", "~/.ai-runner/logs")
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "runner.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='{"ts":"%(asctime)s","lvl":"%(levelname)s","runner":"'
               + str(cfg.get("runner.id", "runner"))
               + '","mod":"%(name)s","msg":"%(message)s"}',
        handlers=handlers,
    )


def _pipelines(cfg: Config, dry_run: bool, only: list[str] | None) -> list[Pipeline]:
    agent = ClaudeCodeAgent(cfg.get("agent", {}) or {})
    store = Store(cfg.get("runner.db_path", "~/.ai-runner/state.db"))
    marker = str(cfg.get("runner.bot_marker", BOT_MARKER_DEFAULT))
    out = []
    for src in cfg.sources(only=only):
        provider = providers.build(src.provider, marker, dry_run)
        out.append(Pipeline(cfg, src, provider, agent, store, dry_run=dry_run))
    return out


def _doctor(cfg: Config) -> int:
    log = logging.getLogger("doctor")
    problems: list[str] = []

    problems += [f"workflow graph: {p}" for p in graph.validate_graph()]
    problems += vcs.preflight(cfg.get("vcs_profiles", {}) or {})

    try:
        agent = ClaudeCodeAgent(cfg.get("agent", {}) or {})
        res = vcs.run([agent.binary, "auth", "status"], check=False)
        if res.returncode != 0:
            problems.append("claude is not logged in on this machine — run `claude` once "
                            "interactively, or `claude auth login`")
        else:
            log.info("Claude Code authenticated")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"agent: {exc}")

    marker = str(cfg.get("runner.bot_marker", BOT_MARKER_DEFAULT))
    for src in cfg.sources():
        try:
            p = providers.build(src.provider, marker)
            log.info("source %-16s %-8s OK, acting as %s",
                     src.key, src.provider_type, p.whoami())
        except Exception as exc:  # noqa: BLE001
            problems.append(f"source {src.key}: {exc}")
        for repo in src.repos:
            path = Path(str(repo["path"])).expanduser()
            profile = cfg.profile_for_repo(repo)
            log.info("  repo %-18s profile=%-12s %s (%s)",
                     repo["key"], profile["name"], path,
                     "present" if path.exists() else "will clone")
        for repo in src.repos:
            bp = brain.brain_path(cfg.path("runner.brain_dir", "~/.ai-runner/brain"),
                                  src.key, repo["key"])
            cap = int((cfg.get("runner.limits", {}) or {})
                      .get("brain_max_bytes", brain.DEFAULT_MAX_BYTES))
            size = bp.stat().st_size if bp.exists() else 0
            log.info("  brain %-17s %d/%d bytes%s", repo["key"], size, cap,
                     "  (at cap — prune it)" if size >= cap else "")

    # Every ticket now costs a review run, and scope-gated ones a planning run
    # too. On subscription auth that spend is invisible to daily_budget_usd.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning(
            "running on Claude subscription auth: the runner spends your own "
            "5-hourly quota and limits.daily_budget_usd cannot see it. Set "
            "ANTHROPIC_API_KEY (and CLAUDE_CODE_SIMPLE=1) in the cron "
            "environment to bill this to pay-as-you-go credits instead.")

    if problems:
        for p in problems:
            log.error("%s", p)
        return 1
    log.info("all good — mode=%s", cfg.get("runner.mode"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai-task-runner")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--source", action="append", dest="sources",
                    help="limit to these source keys (repeatable)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check prerequisites, credentials and repo mapping")
    run = sub.add_parser("run", help="execute one tick and exit (use with cron)")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--force", action="store_true", help="ignore time windows")
    daemon = sub.add_parser("daemon", help="loop forever (use where cron is awkward)")
    daemon.add_argument("--interval", type=int, default=900)
    daemon.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="show in-flight tasks")
    sub.add_parser("graph", help="print the workflow state graph as Mermaid")

    args = ap.parse_args(argv)

    # `graph` renders the static transition table. It needs no config, no
    # credentials and no log directory, so resolve it before touching any.
    if args.cmd == "graph":
        print(graph.to_mermaid())
        return 0

    cfg = Config.load(args.config)
    _logging(cfg, args.verbose)
    log = logging.getLogger("cli")

    if args.cmd == "doctor":
        return _doctor(cfg)

    if args.cmd == "status":
        store = Store(cfg.get("runner.db_path", "~/.ai-runner/state.db"))
        rows = store.in_flight()
        if not rows:
            print("nothing in flight")
        for r in rows:
            print(f"{r['source']:<16} {r['task_key']:<12} {r['state']:<14} "
                  f"attempts={r['attempts']} ${r['cost_usd']:.2f} {r['pr_url'] or ''}")
        return 0

    if args.force:
        cfg.data.setdefault("runner", {})["windows"] = {
            "advance": ["00:00-23:59"], "triage": ["00:00-23:59"],
            "execute": ["00:00-23:59"]}
        cfg.data["runner"]["days"] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    pipes = _pipelines(cfg, getattr(args, "dry_run", False), args.sources)

    def _tick_all() -> None:
        for pipe in pipes:
            try:
                pipe.tick(dt.datetime.now())
            except Exception:  # noqa: BLE001
                # One broken workspace must never stop the others.
                log.exception("source %s failed", pipe.source.key)

    if args.cmd == "run":
        _tick_all()
        return 0

    if args.cmd == "daemon":
        while True:
            try:
                _tick_all()
            except KeyboardInterrupt:
                return 0
            time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
