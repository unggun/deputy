"""CLI entrypoint. Works identically on macOS, Linux and Windows."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path

from . import brain, graph, providers, scorers, vcs
from .agents.claude_code import ClaudeCodeAgent
from .config import Config
from .models import AgentResult, AIState
from .pipeline import Pipeline
from .state import Store

BOT_MARKER_DEFAULT = "<!-- ai-runner -->"

# The reply an isolated triage call gives when it can see no project at all.
# Kept verbatim in the prompt and in CLAUDE.md's description of correct
# isolation, so the doc and the check cannot drift apart.
CANARY_SENTINEL = "NO PROJECT CONTEXT"
CANARY_PROMPT = (
    "What software project are you currently working in? Answer with the "
    "project name and one sentence about what it does. If you have no project "
    f"context at all, reply with exactly: {CANARY_SENTINEL}"
)


def _logging(cfg: Config, verbose: bool) -> None:
    log_dir = cfg.path("runner.log_dir", "~/.deputy/logs")
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
    store = Store(cfg.get("runner.db_path", "~/.deputy/state.db"))
    marker = str(cfg.get("runner.bot_marker", BOT_MARKER_DEFAULT))
    out = []
    for src in cfg.sources(only=only):
        provider = providers.build(src.provider, marker, dry_run)
        out.append(Pipeline(cfg, src, provider, agent, store, dry_run=dry_run))
    return out


def _clickup_state_gaps(cfg: Config) -> list[str]:
    """Every AIState value needs a `state_field_options` entry on a ClickUp
    source using the `custom_field` backend, or `set_ai_state` raises the
    moment a ticket reaches an unmapped state (see clickup.py:103 and the
    README 'Upgrading' section). `doctor` is what operators are told to run
    to confirm the mapping is complete after adding new states — this is the
    check that actually does that; without it, `doctor` exits clean and the
    gap only surfaces mid-flight on the next affected ticket.

    Sources on the `tag` backend and Jira sources are exempt: `tag` has no
    fixed option set, and Jira state lives in an issue entity property that
    accepts any value.
    """
    problems: list[str] = []
    for src in cfg.sources():
        if src.provider_type != "clickup":
            continue
        backend = str(src.provider.get("state_backend", "custom_field"))
        if backend != "custom_field":
            continue
        options = src.provider.get("state_field_options", {}) or {}
        missing = [s.value for s in AIState if s.value not in options]
        if missing:
            problems.append(
                f"source {src.key}: state_field_options is missing "
                f"{', '.join(missing)} for the custom_field backend")
    return problems


def _isolation_status(isolate_triage: bool,
                      res: AgentResult) -> tuple[str, str]:
    """Did triage really run with no ambient project context?

    `--tools ""` removes the ability to act, not the ambient context,
    and `_triage_cwd` deliberately degrades to the launch directory when the
    triage dir cannot be created. Both leave triage reading whatever repo cron
    happened to start in, and neither says so at the point it matters. This is
    the check that asks.

    Returns one of `ok`, `skipped`, `broken`, `unverified` plus a detail line.
    `unverified` is deliberately distinct from `ok`: doctor is what operators
    are told to run before trusting the runner, so a check that could not be
    performed must never read as a pass.
    """
    if not isolate_triage:
        return ("skipped",
                "agent.isolate_triage is false, so triage inherits the launch "
                "directory's CLAUDE.md, settings and git state")
    if not res.ok:
        why = "timed out" if res.timed_out else (res.error or "no reply")
        return ("unverified", f"the canary run did not complete: {why}")
    reply = (res.text or "").strip()
    if not reply:
        return ("unverified", "the canary returned an empty reply")
    if CANARY_SENTINEL in reply:
        return ("ok", "triage sees no project context")
    return ("broken",
            f"triage can see a project, so it will judge tickets against it: "
            f"{reply[:300]}")


def _doctor(cfg: Config) -> int:
    log = logging.getLogger("doctor")
    problems: list[str] = []

    problems += [f"workflow graph: {p}" for p in graph.validate_graph()]
    problems += vcs.preflight(cfg.get("vcs_profiles", {}) or {})
    problems += _clickup_state_gaps(cfg)

    try:
        agent = ClaudeCodeAgent(cfg.get("agent", {}) or {})
        res = vcs.run([agent.binary, "auth", "status"], check=False)
        if res.returncode != 0:
            problems.append("claude is not logged in on this machine — run `claude` once "
                            "interactively, or `claude auth login`")
        else:
            log.info("Claude Code authenticated")
            canary = agent.reason(CANARY_PROMPT)
            status, detail = _isolation_status(agent.isolate_triage, canary)
            log.info("triage isolation canary: %s (%d tokens, $%.4f)",
                     status, canary.usage.total, canary.cost_usd)
            if status in ("broken", "unverified"):
                problems.append(f"triage isolation {status}: {detail}")
            elif status == "skipped":
                log.warning("triage isolation check skipped: %s", detail)
            else:
                log.info("triage isolation OK — %s", detail)
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
            bp = brain.brain_path(cfg.path("runner.brain_dir", "~/.deputy/brain"),
                                  src.key, repo["key"])
            cap = int((cfg.get("runner.limits", {}) or {})
                      .get("brain_max_bytes", brain.DEFAULT_MAX_BYTES))
            size = bp.stat().st_size if bp.exists() else 0
            log.info("  brain %-17s %d/%d bytes%s", repo["key"], size, cap,
                     "  (at cap — prune it)" if size >= cap else "")

    # Scorers: list what will be judged, and fail on a file that will not
    # load -- the tick skips it with a warning nobody reads.
    sdir = cfg.get("runner.scorers_dir", scorers.DEFAULT_DIR)
    found, errors = scorers.load(sdir)
    if not found and not errors:
        log.info("scorers: none in %s (scoring is off)", sdir)
    for sc in found:
        log.info("scorer %-24s %s, %d%% sampled, labels %s (passes at %g)",
                 sc.name, "/".join(sc.stations), sc.sample_rate,
                 ", ".join(f"{k}={v:g}" for k, v in sc.labels.items()),
                 sc.pass_score)
    problems += [f"scorer {e}" for e in errors]

    # Every ticket now costs a review run, and scope-gated ones a planning run
    # too. On subscription auth that spend is invisible to daily_budget_usd.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning(
            "running on Claude subscription auth: the runner spends your own "
            "5-hourly quota and limits.daily_budget_usd cannot see it. Set "
            "ANTHROPIC_API_KEY (and CLAUDE_CODE_SIMPLE=1) in the cron "
            "environment to bill this to pay-as-you-go credits instead.")

    # Dashboard: the two halves can be missing independently -- the optional
    # extra may be uninstalled, or a wheel may have been built without the
    # frontend's static assets. Neither is a problem: a runner with no
    # dashboard is a valid, supported configuration.
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        log.info("dashboard: web extra installed")
    except ImportError:
        log.warning("dashboard: not installed (pip install -e '.[web]')")
    index = Path(__file__).parent / "web" / "static" / "index.html"
    if index.exists():
        log.info("dashboard: assets present (%s)", index)
    else:
        log.warning("dashboard: assets MISSING -- run `npm run build` in web/")

    if problems:
        for p in problems:
            log.error("%s", p)
        return 1
    log.info("all good — mode=%s", cfg.get("runner.mode"))
    return 0


def _score(cfg: Config, name: str, run_ids: list[int], last: int,
           record: bool) -> int:
    """Judge real runs with one scorer and print the verdicts.

    For writing a rubric: point it at a draft file and the runs it should
    pass and fail, read the reasoning, edit, repeat. Nothing is recorded
    unless `record` is set, and then exactly as the tick would record it
    (claimed first, so a run already judged is reported, not judged again).
    The judge's spend goes on the ledger either way, so the daily limits see
    it.
    """
    sdir = cfg.get("runner.scorers_dir", scorers.DEFAULT_DIR)
    path = Path(name).expanduser()
    try:
        if path.suffix == ".md" and path.is_file():
            sc = scorers.parse(path.read_text(encoding="utf-8"), path)
        else:
            found, errors = scorers.load(sdir)
            sc = next((s for s in found if s.name == name), None)
            if sc is None:
                print(f"no scorer named {name} in {sdir}"
                      + (f" ({len(errors)} file(s) failed to load; run "
                         f"`deputy doctor`)" if errors else ""), file=sys.stderr)
                return 1
    except (OSError, ValueError) as exc:
        print(f"{name}: {exc}", file=sys.stderr)
        return 1

    store = Store(cfg.get("runner.db_path", "~/.deputy/state.db"))
    if run_ids:
        runs = [store.event(i) for i in run_ids]
        missing = [i for i, ev in zip(run_ids, runs) if ev is None]
        if missing:
            print(f"no run with id {', '.join(map(str, missing))}", file=sys.stderr)
            return 1
        wrong = [ev for ev in runs if ev and not sc.judges(ev["kind"])]
        if wrong:
            print(f"{sc.name} judges {'/'.join(sc.stations)} runs; run "
                  f"{wrong[0]['id']} is a {wrong[0]['kind']} run", file=sys.stderr)
            return 1
    else:
        runs = [ev for ev in store.recent_runs(sc.stations, 200)
                if scorers.judgeable(ev)][:last]
        if not runs:
            print(f"no finished {'/'.join(sc.stations)} runs with a log yet")
            return 0

    agent = ClaudeCodeAgent(cfg.get("agent", {}) or {})
    repos = {(src.key, r["key"]): r for src in cfg.sources() for r in src.repos}
    secrets: list[str] = []
    for repo in repos.values():
        try:
            secrets.extend(vcs.profile_env(cfg.profile_for_repo(repo)).values())
        except Exception:  # noqa: BLE001
            continue
    log_root = Path(str(cfg.get("runner.log_dir", "~/.deputy/logs"))).expanduser()
    for ev in filter(None, runs):
        row = store.get(ev["task_id"] or "") or {}
        verdict = scorers.score_run(
            agent, store, sc, ev, log_root=log_root,
            repo=repos.get((row.get("source"), row.get("repo_key"))),
            model=(cfg.get("agent", {}) or {}).get("scorer_model"),
            record=record, secrets=secrets)
        head = (f"run {ev['id']}  {row.get('task_key') or ev['task_id']} "
                f"{row.get('repo_key') or ''} {ev['log']}")
        if verdict is None:
            print(f"{head}\n  already judged by {sc.name}; not judged again\n")
            continue
        mark = ("" if verdict.passed is None
                else " PASS" if verdict.passed else " FAIL")
        cost = (f"  ${verdict.spend.cost_usd:.4f}, {verdict.spend.tokens:,} tokens"
                if verdict.spend else "  (no judge run)")
        score = "" if verdict.score is None else f" ({verdict.score:g})"
        print(f"{head}\n  {verdict.label}{score}{mark}{cost}\n  {verdict.reasoning}\n")
    if not record:
        print("not recorded; pass --record to store these verdicts")
    return 0


def _serve(cfg: Config, host: str, port: int, open_browser: bool) -> int:
    """Run the read-only dashboard.

    Imports are local on purpose: FastAPI is an optional extra and `run` --
    the cron entrypoint -- must keep starting without it.
    """
    import sqlite3

    # Only the two optional third-party packages are probed here. Wrapping
    # the `runner.web.*` imports in the same `except ImportError` reported an
    # unrelated ImportError *inside* our own modules -- a typo'd import, a
    # missing sibling -- as "install the web extra", sending an operator to
    # reinstall a dependency that was never the problem. Those imports are
    # left bare, so a real breakage arrives as its own traceback.
    try:
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError:
        print("the dashboard needs its optional dependencies:\n"
              "    pip install -e '.[web]'", file=sys.stderr)
        return 1

    from runner.web.api import build_app
    from runner.web.db import Reader

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {host} exposes ticket titles, PR URLs and "
              f"error text to anything that can reach this machine.",
              file=sys.stderr)

    db = Path(str(cfg.get("runner.db_path", "~/.deputy/state.db"))).expanduser()
    # Refuse rather than serve an empty or broken dashboard. Zeros rendered
    # from a missing database are indistinguishable from a runner that ran
    # and found nothing to do -- exactly the ambiguity this dashboard exists
    # to remove -- so this probes with an actual open-and-query rather than
    # `db.exists()`, which is true for a directory or a corrupt file and
    # would otherwise bind the port and then 500 on every request.
    try:
        probe = Reader(str(db), limits={})
        probe.conn.execute("SELECT 1 FROM tasks LIMIT 1")
        probe.close()
    except FileNotFoundError:
        print(f"no state database at {db}\n"
              f"run `deputy run` at least once first.", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        # Exists but is not a usable runner database -- a directory, a
        # corrupt file, or something else entirely.
        print(f"{db} is not a readable deputy database: {exc}",
              file=sys.stderr)
        return 1

    url = f"http://{host}:{port}/"
    print(f"dashboard on {url}  (read-only; ctrl-c to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    uvicorn.run(build_app(cfg), host=host, port=port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deputy")
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
    score = sub.add_parser(
        "score", help="judge real runs with one scorer (for writing rubrics)")
    score.add_argument("scorer", help="a scorer name, or the path of a .md file")
    score.add_argument("--run", type=int, action="append", dest="runs", default=[],
                       metavar="ID", help="ledger id of a run (repeatable; the "
                       "Runs and Scorers tabs show it)")
    score.add_argument("--last", type=int, default=3,
                       help="without --run, judge this many recent runs")
    score.add_argument("--record", action="store_true",
                       help="store the verdicts, as the tick would")
    serve = sub.add_parser("serve", help="read-only dashboard on localhost")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--no-open", action="store_true")

    args = ap.parse_args(argv)

    # `graph` renders the static transition table. It needs no config, no
    # credentials and no log directory, so resolve it before touching any.
    if args.cmd == "graph":
        print(graph.to_mermaid())
        return 0

    # `serve` renders a read-only view of the local SQLite file and log
    # directory; it never calls a provider. Requiring the provider tokens to
    # be exported before it will start is a credential demand with nothing
    # behind it, and it blocks the dashboard in exactly the interactive shell
    # where cron's environment is absent.
    # `score` reads the store and the logs and calls the agent; like
    # `serve`, it never builds a provider.
    cfg = Config.load(args.config, require_env=(args.cmd not in ("serve", "score")))
    _logging(cfg, args.verbose)
    log = logging.getLogger("cli")

    if args.cmd == "doctor":
        return _doctor(cfg)

    if args.cmd == "status":
        store = Store(cfg.get("runner.db_path", "~/.deputy/state.db"))
        rows = store.in_flight()
        if not rows:
            print("nothing in flight")
        for r in rows:
            # Both currencies: on a subscription login the dollar figure is
            # notional and the token count is the one `daily_tokens` gates on.
            print(f"{r['source']:<16} {r['task_key']:<12} {r['state']:<14} "
                  f"attempts={r['attempts']} ${r['cost_usd']:.2f} "
                  f"{r['tokens'] or 0:>9,}tok {r['pr_url'] or ''}")
        return 0

    if args.cmd == "serve":
        return _serve(cfg, args.host, args.port, not args.no_open)

    if args.cmd == "score":
        return _score(cfg, args.scorer, args.runs, max(1, args.last), args.record)

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
