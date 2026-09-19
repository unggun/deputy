"""FastAPI routes. Thin by design: parse, call db/logs, serialize.

Everything interesting is in `db.py` and `logs.py`, which are HTTP-free and
tested offline. If a question about correctness leads here, it is in the wrong
module.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import graph
from ..config import Config
from . import logs as weblogs
from .db import Reader

STATIC = Path(__file__).parent / "static"

# `tasks.meta` is a JSON TEXT column and `Reader._row` does not scrub it (it
# is not in `Reader._DIRTY` -- it isn't subprocess output, it's whatever the
# provider integration stuffed in there). Dropping it here, once, at the HTTP
# boundary is cheaper than teaching every route to remember.
_DROP = ("meta",)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in _DROP}


def _secrets(cfg: Config) -> list[str]:
    """Exact token values to scrub, gathered as Pipeline._known_secrets does.

    Read by env var *name*, so a custom `token_env: GH_TOKEN_ACME` is caught
    the same as the default.
    """
    import os
    out: list[str] = []
    for profile in (cfg.get("vcs_profiles", {}) or {}).values():
        name = (profile or {}).get("token_env")
        if name and os.environ.get(name):
            out.append(os.environ[name])
    return out


def build_app(cfg: Config) -> FastAPI:
    db_path = cfg.get("runner.db_path", "~/.ai-runner/state.db")
    log_root = Path(str(cfg.get("runner.log_dir", "~/.ai-runner/logs"))).expanduser()
    limits = cfg.get("runner.limits", {}) or {}
    secrets = _secrets(cfg)

    app = FastAPI(title="ai-task-runner", docs_url=None, redoc_url=None)

    def reader() -> Reader:
        # One connection per request: sqlite3 objects are not safely shared
        # across threads, and a read-only open is cheap.
        return Reader(db_path, secrets=secrets, limits=limits)

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        r = reader()
        try:
            out = r.overview(dt.datetime.now())
            out["runner_id"] = cfg.get("runner.id", "runner")
            out["mode"] = cfg.get("runner.mode", "shadow")
            out["windows"] = cfg.get("runner.windows", {}) or {}
            # `days` rides along with `windows` because the last-tick
            # indicator has to reproduce `Pipeline.tick`'s gate exactly, and
            # that gate is day-of-week AND window. Same default as the
            # pipeline's, so an unset key does not make the tile disagree
            # with what the runner actually does.
            out["days"] = cfg.get("runner.days",
                                  ["mon", "tue", "wed", "thu", "fri"])
            return out
        finally:
            r.close()

    @app.get("/api/tasks")
    def tasks(source: str | None = None, state: str | None = None,
              include_terminal: bool = False) -> dict[str, Any]:
        r = reader()
        try:
            return {"tasks": [_public(row) for row in r.tasks(
                        source=source, state=state,
                        include_terminal=include_terminal)],
                    "stuck": [_public(row) for row in
                              r.stuck(int(dt.datetime.now().timestamp()))]}
        finally:
            r.close()

    @app.get("/api/task")
    def task(id: str = Query(...)) -> dict[str, Any]:
        r = reader()
        try:
            row = r.task(id)
            if row is None:
                raise HTTPException(404, "no such row")
            row["stations"] = weblogs.stations(
                log_root, row["task_key"] or "", row["repo_key"] or "")
            return _public(row)
        finally:
            r.close()

    @app.get("/api/task/log")
    def task_log(id: str = Query(...), station: str = Query(...),
                 offset: int = 0) -> dict[str, Any]:
        r = reader()
        try:
            # task_key/repo_key must come from a store row we looked up, never
            # from raw request input -- logs.tail builds a filesystem path
            # from them and only validates `station`. Look the row up first
            # and 404 if it doesn't exist; do not read these off the query
            # string.
            row = r.task(id)
            if row is None:
                raise HTTPException(404, "no such row")
            return weblogs.tail(log_root, row["task_key"] or "",
                                row["repo_key"] or "", station, offset,
                                secrets=secrets)
        finally:
            r.close()

    @app.get("/api/spend")
    def spend(days: int = 30) -> dict[str, Any]:
        r = reader()
        try:
            return {"days": r.spend_by_day(days),
                    "by_task": [_public(row) for row in r.cost_by_task(10)]}
        finally:
            r.close()

    @app.get("/api/throughput")
    def throughput(days: int = 30) -> dict[str, Any]:
        r = reader()
        try:
            return {"days": r.throughput_by_day(days),
                    "cycle_times": r.cycle_times(days),
                    "review_cycles": r.review_cycles(days)}
        finally:
            r.close()

    @app.get("/api/graph")
    def state_graph() -> dict[str, Any]:
        # Structured, so the UI can render a table without a diagram engine.
        # The mermaid source rides along for pasting into anything that does
        # render it -- both come from graph.TRANSITIONS, so neither can drift.
        return {
            "transitions": [
                {"from": src.value, "to": dst.value, "label": label}
                for src, targets in graph.TRANSITIONS.items()
                for dst, label in targets.items()
            ],
            "mermaid": graph.to_mermaid(),
        }

    @app.get("/api/config")
    def config_summary() -> dict[str, Any]:
        # Hand-built. Never serialize the Config object: it holds expanded
        # ${VAR} secrets.
        return {
            "runner_id": cfg.get("runner.id", "runner"),
            "mode": cfg.get("runner.mode", "shadow"),
            "days": cfg.get("runner.days", []),
            "windows": cfg.get("runner.windows", {}) or {},
            "limits": cfg.get("runner.limits", {}) or {},
            "sources": [{"key": s.get("key"),
                         "repos": [r.get("key") for r in (s.get("repos") or [])]}
                        for s in (cfg.get("sources", []) or [])],
        }

    # Guard on the `assets` subdirectory, not STATIC itself: STATIC always
    # exists (it's `runner/web/static` in the source tree) but `assets` is
    # only produced by the frontend's `npm run build`. Mounting StaticFiles
    # against a directory that doesn't exist yet raises at startup, and a
    # fresh checkout with no frontend build is a supported state -- `serve`
    # must still come up and answer the JSON routes.
    if (STATIC / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"),
                  name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str) -> FileResponse:
            # The catch-all exists so client routing survives a hard refresh.
            # It must not answer for /api: an unknown endpoint has to 404, or
            # a typo'd path returns an HTML document to a caller parsing JSON
            # and the error surfaces far from its cause.
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(404, "no such endpoint")
            index = STATIC / "index.html"
            if not index.exists():
                raise HTTPException(
                    500, "dashboard assets are missing; run `npm run build` "
                         "in web/")
            return FileResponse(index)

    return app
