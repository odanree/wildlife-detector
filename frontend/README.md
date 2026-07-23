# wildlife-detector frontend

React + TypeScript operator UI. The vanilla-JS templates it replaced are gone —
`/preview`, `/alerts`, `/baselines`, and `/status` all serve the React app
at `/react/*`; the un-prefixed routes 302-redirect to it.

## Stack

- **Bun 1.x** — install + script runner (replaces npm; ~10× faster cold install)
- **Vite 5** — bundler / dev server
- **React 18** — UI (StrictMode + `why-did-you-render` in dev)
- **TypeScript 5.6, strict** — no `any`, no `unknown` without narrowing
- **Biome 1.9** — formatter + core linter in one Rust binary (replaces ESLint + Prettier for the bulk of rules)
- **ESLint 9 (narrow)** — one plugin only, `eslint-plugin-react-you-might-not-need-an-effect`, running as a separate CI job. Biome doesn't have equivalent rules for the sync-via-effect / derived-state family; this plugin closes that gap. See `eslint.config.js` for the rationale + the 4 anti-pattern → rule mapping.

Biome config lives in `biome.json`. Strict rules that catch real bugs in this codebase:
`useExhaustiveDependencies`, `useHookAtTopLevel`, `noExplicitAny`, `useImportType`.

## Capabilities (current)

- **Live preview** — dual-pane camera view with promote-swap, zone-polygon + OSD-mask editors, secondary-pane opt-in
- **Alerts** — table + lightbox modal with keyboard voting (`Y`/`N`/`U` + auto-advance), bulk-select-all + label-in-batch, per-verdict filter, historical-vs-live scope, replay-in-VLC via NVR playback URL
- **Status dashboard** — per-camera StatusBar (CameraBadge · ResourceChip [cpu / mem / up / fps] · GateFunnelChip · CostChip hidden on local backends)
- **Labeling workflow** — supervised training-data collection (`/api/labels/export.jsonl`), optimistic UI with rollback, cross-tab sync via `storage` events + same-tab sync via `CustomEvent`
- **Counts push via SSE** — header unread badge subscribes to `/api/alerts/events`; server-owned poller fans out to N tabs (no per-tab polling)

## Layout

```
frontend/
  index.html          # Vite entry — mounts <App /> into #root
  vite.config.ts      # proxy → Flask :8100; base=/react/ for prod
  tsconfig.json       # strict, react-jsx, ES2022
  biome.json          # formatter + core lints
  eslint.config.js    # single-plugin narrow ESLint config
  src/
    main.tsx          # React root + StrictMode + wdyr dev import
    wdyr.ts           # why-did-you-render — dev-only render probe
    App.tsx           # Router: /preview, /alerts, /baselines, /status
    api/
      alerts.ts       # /api/alerts + label endpoints
      masks.ts        # OSD masks CRUD
      status.ts       # /status
      zone.ts         # detector zone polygon CRUD
    hooks/
      # Data-source hooks (fetch + poll)
      useAlerts.ts          useCameras.ts    useMasks.ts
      useBaselineMeta.ts    useStatus.ts     useZone.ts
      useUnreadAlerts.ts    # header badge — subscribes to SSE
      useDetectionSize.ts   # per-camera aspect cache
      useZoom.ts            # wheel/pan zoom with per-camera localStorage
      # Extracted feature-hooks (see PRs #42, #43)
      useAlertsFilters.ts   useAlertsSelection.ts
      useAlertsWatermark.ts useLabelOverlay.ts
      useZoneEditor.ts      useMaskEditor.ts
      useSecondaryPane.ts
    pages/
      AlertsPage.tsx         # ~180 LOC — glue between 4 hooks + table + modal
      BaselinesPage.tsx
      LivePreviewPage.tsx    # ~250 LOC — layout + 3 hooks + <CameraPane> x2
      StatusDashboard.tsx    # per-camera <StatusBar />
    components/
      # Alerts UI
      AlertLightbox.tsx      LabelPicker.tsx      BulkLabelBar.tsx
      ReplayButton.tsx       AlertsNavLink.tsx
      # Live-preview UI
      CameraPane.tsx         ZoneOverlay.tsx      MaskOverlay.tsx
      BaselineControls.tsx
      # Status chips
      Chip.tsx (base)  CameraBadge.tsx  ResourceChip.tsx
      GateFunnelChip.tsx  CostChip.tsx  StatusBar.tsx
      # Global
      GlobalHeader.tsx  # nav + alerts unread badge
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
proxied to Flask on :8100 by the Vite dev server. The React DevTools
extension + `wdyr.ts` help catch unnecessary re-renders during dev.

## Lint / typecheck / format

Local:
```bash
bun run lint             # biome check — lint + format check, non-mutating
bun run lint:fix         # biome check --write — apply autofixes
bun run lint:antipatterns # ESLint tier-1 — the anti-pattern plugin
bun run typecheck        # tsc -b, strict mode
bun run format           # biome format --write — format only
```

CI runs on every PR touching `frontend/` or the web Dockerfile — see
`.github/workflows/frontend.yml`. Three separate jobs so a lint fail
doesn't hide a typecheck fail:

- `biome + typecheck` — the fast gate
- `react anti-pattern delta` — ESLint plugin; **strict on delta from base**, so a PR that raises the anti-pattern count fails even if the raw count would otherwise be tolerable
- `vite build` — catches TS type errors that lint won't (implicit type-narrowing failures)
- `docker web (multi-stage)` — the container that actually ships

## Production build

The web container's Dockerfile does this automatically via a multi-stage
build (see `docker/web/Dockerfile`). Manual:

```bash
cd frontend
bun run build   # outputs to frontend/dist/
```

`docker compose build web` copies `frontend/dist/` into
`/app/static/react/` in the web image. Flask serves it from `/react/*`:

- `GET /react/` → `index.html`
- `GET /react/assets/<hash>.js` → hashed Vite output, immutable-cached

## Design conventions

Two we've been holding to consistently since the audit (2026-07-22):

1. **Focused hooks over god components.** Any page > 200 LOC with more than a couple of `useState` gets its state ownership lifted into named hooks. Enforced culturally, not by CI. See `useAlertsFilters` / `useZoneEditor` / etc. for the shape.

2. **No sync-via-effect / derived-state-via-effect.** The `eslint-plugin-react-you-might-not-need-an-effect` gate catches most cases; the ones it misses show up in code review. Replacements: **adjust-state-during-rendering** for prop-derived state, **single-writer wrapper** for handler-owned side effects, **fully-controlled component** for state that belongs to a parent. See PR #29 for the canonical fix shape.
