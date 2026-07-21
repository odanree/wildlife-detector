# wildlife-detector frontend

React + TypeScript rewrite of the operator UI. Migrated in phased PRs; PR 1
lands the scaffold + one live-wired proof-of-pipeline widget (`<CostChip />`).

## Stack

- **Vite 5** — bundler / dev server
- **React 18** — UI
- **TypeScript 5.6, strict** — no `any`, no `unknown` without narrowing
- **ESLint** — recommended + react-hooks + react-refresh
- **Prettier** — 100-col width, double quotes, trailing commas

Kept the dep tree small on purpose. State management (context/reducer)
and data-fetching abstractions land in PR 2+ if they earn their weight.

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
npm install    # first time only
npm run dev
```

Open `http://localhost:5173` — hot-reload on any src/ edit. API calls
(`/status`, `/api/*`, `/snapshots`, `/stream.mjpg`) are transparently
proxied to Flask on :8100 by the Vite dev server.

## Production build

The web container's Dockerfile does this automatically via a multi-stage
build (see `docker/web/Dockerfile`). To do it manually:

```bash
cd frontend
npm run build   # outputs to frontend/dist/
```

`docker compose build web` copies `frontend/dist/` into
`/app/static/react/` in the web image. Flask serves it from `/react/*`:

- `GET /react/` → `index.html`
- `GET /react/assets/<hash>.js` → hashed Vite output, immutable-cached

Open `http://localhost:8100/react/` to see the shell after a rebuild.

## Migration status

| PR | Scope | Status |
|----|-------|--------|
| 1 | Scaffold + `<CostChip />` proof-of-pipeline | **this PR** |
| 2 | Alerts page skeleton (feature parity) | pending |
| 3 | Alerts page interactivity (lightbox, nav) | pending |
| 4 | Shared header chips (funnel/cost/resources) | pending |
| 5+ | Baselines, live preview, zone/mask editors | pending |

The old vanilla-JS `_INDEX_HTML` / `_ALERTS_HTML` / `_BASELINES_HTML`
templates stay in `src/web/preview.py` until each is replaced.
`/`, `/alerts`, `/baselines` continue to serve them — the React shell
lives at `/react/` in parallel so the migration can happen without
breaking the working detector UI.
