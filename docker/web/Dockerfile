# Web sidecar image (Phase A of ADR-002 containerization).
#
# No GPU, no OpenCV, no ultralytics — the sidecar only reads SQLite state,
# reads YAML config, and proxies frames from the detector via HTTP. Keeps
# the image tiny (~60 MB vs detector's ~2 GB) and restart-safe.
#
# Bind: 0.0.0.0:8100 (or PREVIEW_HOST/PREVIEW_PORT from env).
# Reaches detector at http://detector:8101 on the compose network.

# ── Stage 1: build the React frontend ──────────────────────────────────────
# Kept in the web Dockerfile (rather than a separate compose service) so
# the bundle is baked into the same image the Flask sidecar serves — one
# `docker compose build web` produces a self-contained artifact. Local dev
# doesn't need this stage; `cd frontend && bun dev` proxies to Flask
# on :8100 directly (see frontend/vite.config.ts).
#
# Bun is faster than npm at install (~10x on cold cache) and ships its own
# JS runtime + package manager + script runner in a single ~50MB image.
FROM oven/bun:1-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/bun.lock* ./
# `bun install --frozen-lockfile` when bun.lock is present (reproducible);
# falls back to unpinned install when the lockfile hasn't been committed
# yet (early-development scenario).
RUN if [ -f bun.lock ]; then bun install --frozen-lockfile; else bun install; fi
COPY frontend/ ./
RUN bun run build

# ── Stage 2: Flask sidecar with the built bundle copied in ─────────────────
FROM python:3.11-slim

WORKDIR /app

# Minimal deps only. Full requirements.txt would pull ultralytics + torch
# for no runtime gain — this file is scoped to what web_service.py imports.
COPY docker/web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source code — same repo layout, different entrypoint. Detector and web
# share this tree via COPY (no volume mount) so image is self-contained.
COPY src ./src
COPY config ./config

# React bundle produced above lands under /app/static/react/ so Flask can
# serve it from /react/* (see web_service.py routes). Bundle references
# /react/assets/... absolute paths because vite.config.ts sets base=/react/.
COPY --from=frontend-build /frontend/dist ./static/react

# State and snapshot dirs are mounted from compose as named volumes; make
# the mount points exist so bind fails loud if compose forgets them.
RUN mkdir -p logs snapshots data

ENV PYTHONUNBUFFERED=1 \
    PREVIEW_HOST=0.0.0.0 \
    PREVIEW_PORT=8100 \
    DETECTOR_INTERNAL_URL=http://detector:8101

EXPOSE 8100

CMD ["python", "-m", "src.web_service"]
