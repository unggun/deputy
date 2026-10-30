"""CLI entrypoint. Works identically on macOS, Linux and Windows."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

from . import analyst, bench, bench_impl, brain, graph, improve, providers, scorers, vcs
from .agents.claude_code import ClaudeCodeAgent
from .config import Config
from .models import AgentResult, AIState
from .pipeline import Pipeline, _work_prompt, limits_exceeded
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
    conf = improve.settings(cfg.get("runner.improve"))
    if conf["enabled"]:
        log.info("improve: on, %d run(s) per tick; a repo is due at %d uncited "
                 "failures or one %d days old", conf["max_per_run"],
                 conf["min_evidence"], conf["max_age_days"])
    else:
        log.info("improve: off (runner.improve.enabled); `deputy improve run "
                 "--repo KEY` still works on demand")

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


def _repo_secrets(cfg: Config) -> list[str]:
    """Every token the vcs profiles inject, for redaction, as the tick does."""
    out: list[str] = []
    for src in cfg.sources():
        for repo in src.repos:
            try:
                out.extend(vcs.profile_env(cfg.profile_for_repo(repo)).values())
            except Exception:  # noqa: BLE001
                continue
    return sorted(set(out), key=len, reverse=True)


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

    def row_of(ev: dict) -> dict:
        return store.get(ev["task_id"] or "") or {}

    if run_ids:
        found_runs = [store.event(i) for i in run_ids]
        missing = [i for i, ev in zip(run_ids, found_runs) if ev is None]
        if missing:
            print(f"no run with id {', '.join(map(str, missing))}", file=sys.stderr)
            return 1
        runs = [ev for ev in found_runs if ev]
        # The scorer's own scope applies here exactly as in the tick: a
        # verdict on a run the scorer does not cover would land in its pass
        # rate with `--record`, and tells a rubric author nothing without it.
        refusals = [
            f"{sc.name} judges {'/'.join(sc.stations)} runs; run {ev['id']} "
            f"is a {ev['kind']} run" for ev in runs if not sc.judges(ev["kind"])
        ] + [
            f"{sc.name} covers {', '.join(sc.repos)}; run {ev['id']} is in "
            f"{row_of(ev).get('repo_key')}" for ev in runs if not sc.covers(row_of(ev))
        ] + [
            f"run {ev['id']} failed, and the tick never records a verdict on a "
            f"failed run" for ev in runs if record and not scorers.judgeable(ev)
        ]
        if refusals:
            print(refusals[0], file=sys.stderr)
            return 1
    else:
        runs = [ev for ev in store.recent_runs(sc.stations, 500)
                if scorers.judgeable(ev) and sc.covers(row_of(ev))][:last]
        if not runs:
            print(f"no finished {'/'.join(sc.stations)} runs with a log yet")
            return 0

    agent = ClaudeCodeAgent(cfg.get("agent", {}) or {})
    repos = {(src.key, r["key"]): r for src in cfg.sources() for r in src.repos}
    secrets = _repo_secrets(cfg)
    log_root = Path(str(cfg.get("runner.log_dir", "~/.deputy/logs"))).expanduser()
    limits = cfg.get("runner.limits", {}) or {}
    for ev in runs:
        # Operator-run, but it spends the same quota: held to the same daily
        # limits as the tick, checked before every judge call.
        spent = limits_exceeded(store, limits, dt.datetime.now())
        if spent:
            print(f"{spent}; not judging further runs today", file=sys.stderr)
            return 1
        row = row_of(ev)
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


def _age(ts: int | None) -> str:
    if not ts:
        return "-"
    secs = max(0, int(time.time()) - int(ts))
    return f"{secs // 86400}d" if secs >= 86400 else f"{secs // 3600}h"


def _improve(cfg: Config, args: argparse.Namespace) -> int:
    """`deputy improve`: review proposals, or ask for one now.

    `list`, `show`, `accept` and `reject` only read and write the store and
    the brain. `run` is the tick's own path (`improve.run_for_repo`) for one
    repo, skipping the "is it due" check on purpose -- it is how a person
    sees what the station makes of their history before turning it on -- and
    never the daily limits.
    """
    import json

    store = Store(cfg.get("runner.db_path", "~/.deputy/state.db"))
    limits = cfg.get("runner.limits", {}) or {}
    brain_root = Path(str(cfg.get("runner.brain_dir", "~/.deputy/brain"))).expanduser()

    def refs(row: dict, col: str) -> list[str]:
        try:
            return [str(x) for x in json.loads(row.get(col) or "[]")]
        except ValueError:
            return []

    def headline(r: dict) -> str:
        what = r["note"] or r["reasoning"] or ""
        return (f"#{r['id']:<5} {r['source']}/{r['repo_key']:<18} {r['status']:<9} "
                f"{_age(r['ts']):>4}  {len(refs(r, 'cited'))}/{len(refs(r, 'evidence'))} "
                f"cited  {what[:90]}")

    if args.action == "list":
        rows = store.improvements(statuses=None if args.all else ("pending",),
                                  limit=args.limit)
        if not rows:
            print("no improve runs yet" if args.all else "no proposals waiting")
            return 0
        for r in rows:
            print(headline(r))
        if any(r["status"] == "pending" for r in rows):
            print("\nread one with `deputy improve show ID`, then `deputy improve "
                  "accept ID` or `deputy improve reject ID --reason \"...\"`")
        return 0

    if args.action in ("show", "accept", "reject"):
        row = store.improvement(args.id)
        if row is None:
            print(f"no proposal {args.id}", file=sys.stderr)
            return 1
        if args.action == "show":
            print(headline(row))
            for label, col in (("Problem", "problem"), ("Solution", "solution"),
                               ("Note", "note"), ("Reasoning", "reasoning"),
                               ("Rejected because", "reason")):
                if row.get(col):
                    print(f"\n{label}:\n  {row[col]}")
            cited = refs(row, "cited")
            if cited:
                print("\nEvidence it cites:")
                for ref in cited:
                    print(f"  {improve.describe(store, ref)}")
            rest = [r for r in refs(row, "evidence") if r not in cited]
            if rest:
                print(f"\nAlso shown ({len(rest)}): {', '.join(rest)}")
            if row.get("log"):
                log_root = Path(str(cfg.get("runner.log_dir", "~/.deputy/logs"))).expanduser()
                print(f"\nLog: {log_root / row['log']}  "
                      f"(${float(row['cost_usd'] or 0):.4f}, {int(row['tokens'] or 0):,} tokens)")
            return 0
        if args.action == "accept":
            ok, msg = improve.accept(
                store, args.id, brain_root,
                max_bytes=int(limits.get("brain_max_bytes", brain.DEFAULT_MAX_BYTES)))
        else:
            ok, msg = improve.reject(store, args.id, args.reason)
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    # run
    matches = [(src, r) for src in cfg.sources(only=args.sources) for r in src.repos
               if r.get("key") == args.repo]
    if not matches:
        print(f"no repo {args.repo} in any source", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"{args.repo} is in {', '.join(s.key for s, _ in matches)}; pick one "
              f"with --source", file=sys.stderr)
        return 1
    src, repo = matches[0]
    spent = limits_exceeded(store, limits, dt.datetime.now())
    if spent:
        print(f"{spent}; not running", file=sys.stderr)
        return 1
    acfg = cfg.get("agent", {}) or {}
    found, _ = scorers.load(cfg.get("runner.scorers_dir", scorers.DEFAULT_DIR))
    ctx = improve.Context(
        agent=ClaudeCodeAgent(acfg), store=store, source_key=src.key,
        log_root=cfg.path("runner.log_dir", "~/.deputy/logs"),
        work_root=cfg.path("runner.work_dir", "~/.deputy/work"),
        brain_root=brain_root,
        settings=improve.settings(cfg.get("runner.improve")),
        model=acfg.get("improve_model") or acfg.get("model"),
        tools=list(acfg.get("planner_tools") or analyst.PLANNER_TOOLS),
        secrets=_repo_secrets(cfg), rubrics={sc.name: sc.rubric for sc in found},
        claim_ttl=improve.claim_ttl(acfg))
    row = improve.run_for_repo(ctx, repo, vcs.profile_env(cfg.profile_for_repo(repo)),
                               dt.datetime.now())
    if row is None:
        print(f"nothing new to look at on {src.key}/{args.repo}, or another run "
              f"holds it")
        return 0
    print(headline(row))
    if row["status"] == "pending":
        print(f"\nread it with `deputy improve show {row['id']}`")
    elif row.get("reasoning"):
        print(f"  {row['reasoning']}")
    return 1 if row["status"] == "error" else 0


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def _columns(head: tuple[str, ...], body: list[tuple[str, ...]]) -> list[str]:
    """Aligned columns: the first left, the rest right."""
    widths = [max(len(row[i]) for row in (head, *body)) for i in range(len(head))]
    return ["  ".join(c.ljust(w) if i == 0 else c.rjust(w)
                      for i, (c, w) in enumerate(zip(row, widths))).rstrip()
            for row in (head, *body)]


def _many(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _range(r: dict) -> str:
    return "-" if r["low"] is None else f"{_pct(r['low'])}-{_pct(r['high'])}"


def _bench_table(trials: list[dict]) -> list[str]:
    """The per-variant summary, as `deputy bench triage` and `show` print it."""
    return _columns(
        ("variant", "accuracy", "95% range", "routing", "same as then",
         "errors", "avg cost", "avg tokens"),
        [(r["variant"], f"{r['correct']}/{r['n']} {_pct(r['accuracy'])}", _range(r),
          f"{r['routed_ok']}/{r['routed_n']}" if r["routed_n"] else "-",
          f"{r['same']}/{r['compared']}", str(r["errors"]),
          f"${r['avg_cost_usd']:.4f}", f"{r['avg_tokens']:,.0f}")
         for r in bench.summarize(trials)])


def _impl_table(trials: list[dict], labels: list[str]) -> list[str]:
    """The per-variant summary of an implement run."""
    rows = bench_impl.summarize(trials, labels)
    out = _columns(
        ("variant", "correct", "95% range", "tests passed", "partial", "no changes",
         "failed", "avg cost", "avg time"),
        [(r["variant"], f"{r['correct']}/{r['n']} {_pct(r['rate'])}", _range(r),
          f"{r['tests_ok']}/{r['tests_n']}" if r["tests_n"] else "-",
          str(r["partial"]), str(r["no_changes"]), str(r["errors"]),
          f"${r['avg_cost_usd']:.2f}", f"{r['avg_minutes']:.1f} min")
         for r in rows])
    left = max((r["left_out"] for r in rows), default=0)
    if left:
        out.append(f"(trials on a ticket not every variant finished are left out: "
                   f"{sum(r['left_out'] for r in rows)})")
    return out


def _labels(run: dict) -> list[str]:
    import json
    try:
        return [str(v["label"]) for v in json.loads(run.get("variants") or "[]")]
    except (TypeError, ValueError, KeyError):
        return []


def _impl_show(run: dict, trials: list[dict]) -> None:
    print("\n".join(_impl_table(trials, _labels(run))))
    missed = bench_impl.misses(trials)
    if missed:
        print("\ngraded wrong:")
    for t in missed:
        tests = {1: "tests passed", 0: "tests failed"}.get(t["tests"], "tests not run")
        print(f"  {t['task_key'] or t['case_name']:<12} {t['variant']}: {t['decision']}, "
              f"{tests}, +{t['additions'] or 0}/-{t['deletions'] or 0}")
        if t["reasoning"]:
            print(f"  {'':<12} {t['reasoning'][:200]}")


def _bench_implement(cfg: Config, args: argparse.Namespace, store: Store) -> int:
    """`deputy bench implement`: replay merged tickets under other models."""
    acfg = cfg.get("agent", {}) or {}
    log_root = cfg.path("runner.log_dir", "~/.deputy/logs")
    repos_now = {(src.key, str(r["key"])): r for src in cfg.sources() for r in src.repos}
    cases, left_out = bench_impl.load(store, log_root, repos_now, sources=args.sources,
                                      repos=args.repos or None)
    total, graded = len(cases) + sum(left_out.values()), len(cases)
    cases = cases[:max(1, args.limit)]
    print(f"implementation benchmark: {_many(graded, 'merged ticket')} can be replayed "
          f"(of {_many(total, 'merged row')})"
          + (f"; replaying the newest {len(cases)}" if graded > len(cases) else ""))
    if left_out:
        print("  left out: " + ", ".join(f"{v} {k}" for k, v in left_out.most_common()))
    variants = bench_impl.variants(args.models, default_model=acfg.get("model"))
    print("variants, baseline first:")
    for v in variants:
        print(f"  {v.label}")
    runs = len(cases) * len(variants) * max(1, args.repeat)
    seen = [c.cost_usd for c in cases if c.cost_usd]
    guess = (f"; the merged runs cost ${sum(seen) / len(seen):.2f} each, so about "
             f"${runs * sum(seen) / len(seen):.2f} plus a judge call per run" if seen else "")
    print(f"{runs} agent runs{guess} (cap ${args.max_usd:.2f})")
    if args.dry_run or not cases:
        if not cases:
            print("nothing to replay yet: a merged ticket needs its base commit, its PR "
                  "number and the triage snapshot that routed it")
        else:
            print("dry run: nothing was run, fetched or recorded")
        return 0

    agent = ClaudeCodeAgent(acfg)
    limits = cfg.get("runner.limits", {}) or {}

    def progress(case: bench_impl.Case, fields: dict, spend: object) -> None:
        mark = {1: "✓", 0: "✕"}.get(fields["correct"], "?")
        tests = {1: "tests passed", 0: "tests failed"}.get(fields["tests"], "tests not run")
        said = fields["decision"] or (fields["error"] or "failed")[:80]
        print(f"  {case.task_key:<12} {case.repo_key:<16} {fields['variant']}: {said}, "
              f"{tests} {mark}  ${getattr(spend, 'cost_usd', 0.0):.2f}")

    def skipped(case: bench_impl.Case, why: str) -> None:
        print(f"  {case.task_key:<12} {case.repo_key:<16} skipped, no reference: {why}")

    run_id, stop = bench_impl.run(
        agent, store, cases, variants, repeat=max(1, args.repeat), max_usd=args.max_usd,
        limits_check=lambda: limits_exceeded(store, limits, dt.datetime.now()),
        work_prompt=_work_prompt,
        profile_env=lambda repo: vcs.profile_env(cfg.profile_for_repo(repo)),
        work_root=cfg.path("runner.work_dir", "~/.deputy/work"), log_root=log_root,
        brain_root=cfg.path("runner.brain_dir", "~/.deputy/brain"),
        judge_model=args.judge_model or acfg.get("scorer_model"),
        max_prompt_bytes=agent.max_prompt_bytes, secrets=_repo_secrets(cfg),
        keep=args.keep, on_trial=progress, on_skip=skipped)
    run = store.bench_run(run_id) or {}
    print()
    print("\n".join(_impl_table([bench.decode(t) for t in store.bench_trials(run_id)],
                                [v.label for v in variants])))
    if run.get("note"):
        print(f"\n{run['note']}")
    print(f"\nrecorded as bench #{run_id}; `deputy bench show {run_id}` lists what was "
          f"graded wrong")
    return 1 if stop in ("limits", "interrupted") else 0


def _bench(cfg: Config, args: argparse.Namespace) -> int:
    """`deputy bench`: replay past tickets under other models or prompts.

    `triage` replays triage inputs; `implement` replays merged tickets in
    scratch clones. Never part of the tick. Both spend the same quota, so
    they are held to the daily limits like `deputy score`, plus their own
    `--max-usd` cap. `--dry-run` shows the suite and calls nothing.
    """
    import json

    if args.action == "prompt":
        print(bench.triage.SYSTEM)
        return 0
    store = Store(cfg.get("runner.db_path", "~/.deputy/state.db"))
    if args.action == "implement":
        return _bench_implement(cfg, args, store)
    if args.action == "list":
        runs = store.bench_runs(args.limit)
        if not runs:
            print("no benchmark runs yet; try `deputy bench triage --dry-run`")
        for r in runs:
            trials = store.bench_trials(r["id"])
            cost = sum(float(t["cost_usd"] or 0) for t in trials)
            state = ("did not finish" if r["finished"] is None
                     else r["note"] or "finished")
            unit = "ticket" if r["suite"] == bench_impl.SUITE else "triage run"
            print(f"#{r['id']:<4} {_age(r['ts']):>4}  {r['suite']:<9} "
                  f"{_many(r['cases'], unit):>15}  "
                  f"{_many(len(json.loads(r['variants'] or '[]')), 'variant'):<10}  "
                  f"${cost:.2f}  {state}")
        return 0
    if args.action == "show":
        run = store.bench_run(args.id) if args.id else next(iter(store.bench_runs(1)), None)
        if run is None:
            print(f"no benchmark run {args.id}" if args.id else "no benchmark runs yet",
                  file=sys.stderr)
            return 1
        trials = [bench.decode(t) for t in store.bench_trials(run["id"])]
        impl = run["suite"] == bench_impl.SUITE
        print(f"bench #{run['id']}  {run['suite']}  "
              f"{_many(run['cases'], 'ticket' if impl else 'triage run')}  "
              f"{'did not finish' if run['finished'] is None else run['note'] or 'finished'}")
        if impl:
            _impl_show(run, trials)
            return 0
        print("\n".join(_bench_table(trials)))
        missed = bench.misses(trials)
        if missed:
            print("\nwhere a variant disagreed with history:")
        for t in missed:
            said = t["decision"] + (f" {', '.join(t['repos'])}"
                                    if t["decision"] == "execute" and t["repos"] else "")
            want = "/".join(t["expected"] or [])
            if t["correct"] and t["routed"] == 0:
                want = f"execute {', '.join(t['expected_repos'] or [])}"
            print(f"  {t['task_key'] or t['case_name']:<12} {t['why']:<14} "
                  f"history: {want:<16} {t['variant']}: {said}")
            if t["reasoning"]:
                print(f"  {'':<12} {t['reasoning'][:200]}")
        return 0

    # triage
    acfg = cfg.get("agent", {}) or {}
    policy = cfg.get("policy", {}) or {}
    log_root = cfg.path("runner.log_dir", "~/.deputy/logs")
    try:
        variants = bench.variants(args.models, args.prompts,
                                  default_model=acfg.get("triage_model") or acfg.get("model"),
                                  current_repos=args.current_repos)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    cases, left_out = bench.load(log_root, store, now=int(time.time()),
                                 policy=policy, sources=args.sources)
    total = len(cases) + sum(left_out.values())
    graded = len(cases)
    cases = cases[:max(1, args.limit)]
    why = Counter(c.why for c in cases)
    print(f"triage benchmark: {graded} past triage runs history can grade "
          f"(of {total} snapshots in {log_root / 'triage'})"
          + (f"; replaying the newest {len(cases)}" if graded > len(cases) else ""))
    if why:
        print("  " + " · ".join(f"{k} {v}" for k, v in why.most_common()))
    if left_out:
        print("  left out: " + ", ".join(f"{v} {k}" for k, v in left_out.most_common()))
    print("variants, baseline first:")
    for v in variants:
        print(f"  {v.label}")
    calls = len(cases) * len(variants) * max(1, args.repeat)
    seen = [float(c.snap.get("cost_usd") or 0) for c in cases if c.snap.get("cost_usd")]
    guess = (f"; triage averaged ${sum(seen) / len(seen):.4f} a call, so about "
             f"${calls * sum(seen) / len(seen):.2f}" if seen else "")
    print(f"{calls} calls{guess} (cap ${args.max_usd:.2f})")
    if args.dry_run or not cases:
        if not cases:
            print("nothing to replay yet: snapshots need an outcome first "
                  "(a merge, an answered question, a reset)")
        else:
            print("dry run: nothing was called or recorded")
        return 0

    repos_now = {(src.key, str(r["key"])): r for src in cfg.sources() for r in src.repos}
    limits = cfg.get("runner.limits", {}) or {}

    def progress(case: bench.Case, fields: dict, spend: object) -> None:
        mark = ("failed" if fields["decision"] is None
                else "✓" if fields["correct"] and fields["routed"] != 0 else "✕")
        said = fields["decision"] or (fields["error"] or "")[:80]
        print(f"  {case.task_key or case.name:<12} {case.why:<14} {fields['variant']}: "
              f"{said} {mark}")

    run_id, stop = bench.run(
        ClaudeCodeAgent(acfg), store, cases, variants, policy=policy,
        repos_now=repos_now, repeat=max(1, args.repeat), max_usd=args.max_usd,
        limits_check=lambda: limits_exceeded(store, limits, dt.datetime.now()),
        secrets=_repo_secrets(cfg), on_trial=progress)
    run = store.bench_run(run_id) or {}
    print()
    print("\n".join(_bench_table([bench.decode(t) for t in store.bench_trials(run_id)])))
    if run.get("note"):
        print(f"\n{run['note']}")
    print(f"\nrecorded as bench #{run_id}; `deputy bench show {run_id}` lists the "
          f"disagreements")
    # The cap is a choice the person made; the daily limits and a ^C are not
    # the run they asked for.
    return 1 if stop in ("limits", "interrupted") else 0


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
    imp = sub.add_parser(
        "improve", help="review proposed brain notes, or ask for one now")
    isub = imp.add_subparsers(dest="action", required=True)
    ilist = isub.add_parser("list", help="proposals waiting for you")
    ilist.add_argument("--all", action="store_true",
                       help="every improve run, not only pending proposals")
    ilist.add_argument("--limit", type=int, default=50)
    for name, text in (("show", "one proposal and its evidence"),
                       ("accept", "write the note to its repo's brain"),
                       ("reject", "decline it; later runs are told why")):
        p = isub.add_parser(name, help=text)
        p.add_argument("id", type=int)
        if name == "reject":
            p.add_argument("--reason", default=None,
                           help="shown to every later run on the repo")
    irun = isub.add_parser("run", help="run the improve station on one repo now")
    irun.add_argument("--repo", required=True, help="repo key from config")
    ben = sub.add_parser(
        "bench", help="replay past tickets to compare models and prompts")
    bsub = ben.add_subparsers(dest="action", required=True)
    btri = bsub.add_parser("triage", help="replay past triage inputs under each variant")
    btri.add_argument("--model", action="append", dest="models", default=[],
                      help="a model to try (repeatable); the configured one is "
                      "always the baseline")
    btri.add_argument("--prompt", action="append", dest="prompts", default=[],
                      help="a file to use in place of the built-in triage prompt, "
                      "or 'builtin' (repeatable); start one from `deputy bench prompt`")
    btri.add_argument("--current-repos", action="store_true",
                      help="offer each repo with today's language and notes from "
                      "config instead of the ones triage saw")
    btri.add_argument("--limit", type=int, default=50, help="most recent N tickets")
    btri.add_argument("--repeat", type=int, default=1, help="calls per ticket per variant")
    btri.add_argument("--max-usd", type=float, default=5.0,
                      help="stop starting tickets once this much is spent")
    btri.add_argument("--dry-run", action="store_true",
                      help="show the suite and the answer key; call nothing")
    bimp = bsub.add_parser("implement",
                           help="replay merged tickets in scratch clones under each model")
    bimp.add_argument("--model", action="append", dest="models", default=[],
                      help="a model to try (repeatable); the configured one is "
                      "always the baseline")
    bimp.add_argument("--repo", action="append", dest="repos", default=[],
                      help="only this repo key (repeatable)")
    bimp.add_argument("--judge-model", default=None,
                      help="model for the correctness judge (default: agent.scorer_model, "
                      "else the triage model)")
    bimp.add_argument("--limit", type=int, default=10, help="most recent N merged tickets")
    bimp.add_argument("--repeat", type=int, default=1, help="runs per ticket per variant")
    bimp.add_argument("--max-usd", type=float, default=10.0,
                      help="stop starting tickets once this much is spent, or would be")
    bimp.add_argument("--keep", action="store_true",
                      help="keep the scratch clones under work_dir/bench for inspection")
    bimp.add_argument("--dry-run", action="store_true",
                      help="show the suite and an estimate; run, fetch and record nothing")
    blist = bsub.add_parser("list", help="recent benchmark runs")
    blist.add_argument("--limit", type=int, default=20)
    bshow = bsub.add_parser("show", help="one run's results and its disagreements")
    bshow.add_argument("id", type=int, nargs="?", help="default: the latest run")
    bsub.add_parser("prompt", help="print the built-in triage prompt, to start a variant from")
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
    # `score` and `bench` read the store and the logs and call the agent;
    # like `serve`, they never build a provider.
    cfg = Config.load(args.config,
                      require_env=(args.cmd not in ("serve", "score", "improve", "bench")))
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

    if args.cmd == "improve":
        return _improve(cfg, args)

    if args.cmd == "bench":
        return _bench(cfg, args)

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
