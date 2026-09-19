# ai-task-runner dashboard (frontend)

The React/TypeScript source for the read-only monitoring dashboard served by
`ai-task-runner serve`. It talks to the FastAPI routes in `runner/web/api.py`
and renders nothing it cannot get from them.

```bash
npm install
npm run dev     # vite dev server; proxies /api to a running `serve`
npm run build   # tsc -b && vite build
npm run lint    # oxlint
```

`npm run build` writes into **`../runner/web/static/`**, which is committed:
the Python package ships the built assets so `pip install` plus
`ai-task-runner serve` works without Node. Any change under `web/src` has to
be rebuilt and the rebuilt `runner/web/static/` committed alongside it, or the
served dashboard silently stays on the previous version.

Things worth knowing before editing:

- **TypeScript runs in `strict` mode.** The `| null`s in `src/lib/api.ts` are
  the cross-layer contract with `runner/web/db.py`; they are load-bearing, not
  decoration. Do not reach for `any` or `!` to get past one.
- **Every view owes an error branch.** React Query leaves `data` undefined
  once retries are exhausted, so `if (!data)` alone spins on "loading…"
  against a dead API, and an empty board reads as "nothing in flight".
  `src/components/FetchError.tsx` is the shared answer.
- **Colors come from `src/lib/palette.ts`.** Station kinds use the categorical
  `SERIES` ramp, task states the reserved `STATUS` ramp, and the two must not
  overlap. `SURFACE` tracks `--card` — the surface charts are actually drawn
  on — not `--background`.
- **Every chart owes a table view**, via `src/components/ChartFrame.tsx`. That
  is what relieves the light palette's known contrast warning.

`src/components/ui/*` is vendored shadcn. `tooltip.tsx` and `separator.tsx`
are currently unused; they are kept because they are cheap and re-adding a
primitive by hand is not.
