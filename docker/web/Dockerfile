# Web sidecar image (Phase A of ADR-002 containerization).
#
# No GPU, no OpenCV, no ultralytics — the sidecar only reads SQLite state,
# reads YAML config, and proxies frames from the detector via HTTP. Keeps
# the image tiny (~60 MB vs detector's ~2 GB) and restart-safe.
#
# Bind: 0.0.0.0:8100 (or PREVIEW_HOST/PREVIEW_PORT from env).
# Reaches detector at http://detector:8101 on the compose network.
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

# State and snapshot dirs are mounted from compose as named volumes; make
# the mount points exist so bind fails loud if compose forgets them.
RUN mkdir -p logs snapshots data

ENV PYTHONUNBUFFERED=1 \
    PREVIEW_HOST=0.0.0.0 \
    PREVIEW_PORT=8100 \
    DETECTOR_INTERNAL_URL=http://detector:8101

EXPOSE 8100

CMD ["python", "-m", "src.web_service"]
