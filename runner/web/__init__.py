"""Read-only HTTP view over the runner's local state.

Nothing here writes. `db.Reader` opens SQLite with `mode=ro` so that is true
at the storage layer rather than by convention, and every string that can
carry subprocess output is passed through `redact.redact` on the way out --
a web endpoint is an outward path, the same as a ticket comment.

This package must never be imported from `cli.py` at module level: FastAPI is
an optional extra and the cron path must keep starting without it.
"""
