# wildlife-detector frontend

React + TypeScript rewrite of the operator UI. Migrated in phased PRs; PR 1
lands the scaffold + one live-wired proof-of-pipeline widget (`<CostChip />`).

## Stack

- **Bun 1.x** — install + script runner (replaces npm; ~10x faster cold install)
- **Vite 5** — bundler / dev server
- **React 18** — UI
- **TypeScript 5.6, strict** — no `any`, no `unknown` without narrowing
- **Biome 1.9** — lint + format in one Rust binary (replaces ESLint + Prettier)

Kept the dep tree small on purpose. State management (context/reducer)
and data-fetching abstractions land in PR 2+ if they earn their weight.

Biome config lives in `biome.json`. Strict rules that would catch real bugs
in this codebase: `useExhaustiveDependencies` (React hook deps),
`useHookAtTopLevel`, `noExplicitAny`, `useImportType`.

## Layout

```
frontend/
  index.html          # Vite entry — mounts <App /> into #root
  vite.config.ts      # proxy → Flask :8100; base=/react/ for prod
  tsconfig.json       # strict mode, react-jsx, ES2022
  src/
    main.tsx          # React root
    App.tsx           # Landing page (PR 1)
    api/
      status.ts       # /status typed client
    hooks/
      useStatus.ts    # Polling hook with abort-on-unmount
    components/
      CostChip.tsx    # Live cost/cache widget
```

## Dev loop

Two terminals:

```bash
# 1. Flask backend on :8100 (via compose or local python)
docker compose up -d web detector-yard detector-rooftop

# 2. Vite dev server on :5173 with proxy to :8100
cd frontend
bun install    # first time only (fast — ~1s cold on modern hardware)
bun run dev
```

Open `http://localhost:5173` — hot-reload on any src/ edit. API calls
(`/status`, `/api/*`, `/snapshots`, `/stream.mjpg`) are transparently
proxied to Flask on :8100 by the Vite dev server.

## Lint / typecheck / format

Local:
```bash
bun run lint       # biome check — lint + format check, non-mutating
bun run lint:fix   # biome check --write — apply autofixes
bun run typecheck  # tsc -b, strict mode
bun run format     # biome format --write — format only, no lint
```

CI runs `lint` + `typecheck` + `build` on every PR touching `frontend/`
or the web Dockerfile — see `.github/workflows/frontend.yml`.

## Production build

The web container's Dockerfile does this automatically via a multi-stage
build (see `docker/web/Dockerfile`). To do it manually:

```bash
cd frontend
bun run build   # outputs to frontend/dist/
```

`docker compose build web` copies `frontend/dist/` into
`/app/static/react/` in the web image. Flask serves it from `/react/*`:

- `GET /react/` → `index.html`
- `GET /react/assets/<hash>.js` → hashed Vite output, immutable-cached

Open `http://localhost:8100/react/` to see the shell after a rebuild.

## Migration status

| PR | Scope | Status |
|----|-------|--------|
| 1 | Scaffold + `<CostChip />` proof-of-pipeline | ✅ merged |
| 2 | Alerts page skeleton (feature parity) | ✅ merged |
| 3 | Alerts page interactivity (lightbox, nav) | ✅ merged |
| 4 | Alerts cutover — delete `_ALERTS_HTML`, `/alerts` → `/react/alerts` | ✅ merged |
| 5 | Baselines page (parallel) | ✅ merged |
| 6 | **Baselines cutover** — delete `_BASELINES_HTML`, `/baselines` → `/react/baselines` | **this PR** |
| 7 | Shared header chips (funnel/cost/resources) | pending |
| 8+ | Live preview, zone/mask editors | pending |

After this PR: `/alerts` and `/baselines` both 302-redirect to their
React equivalents. Vanilla templates for both are deleted.
`/` (live preview) still on vanilla — the hard one, migrates next
via shared header chips first (PR 7), then the streaming preview +
zone/mask canvas editors.
