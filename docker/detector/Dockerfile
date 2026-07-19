# Detector image (Phase B of ADR-002 containerization).
#
# Runs the full pipeline: RTSP ingest → MOG2 + YOLO → cascade VLM.
# Heavy image (~2 GB) because it carries opencv + ultralytics + torch.
# Binds:
#   - loopback-only internal HTTP on 8101 (bearer-gated; talks to web sidecar)
#   - pulls YOLO model on first run (auto-cached under models/ volume)
#
# Network access needed:
#   - RTSP camera on LAN (bridge network; Docker Desktop's default reaches LAN)
#   - Ollama on host at 11434 → via host.docker.internal (mapped by compose)
#   - Anthropic API → public internet, no special setup
FROM python:3.11-slim

# System libs opencv needs. libgomp for ultralytics parallelism.
# ffmpeg is optional but useful for the video-file replay path (--video).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgl1 libgomp1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config ./config

# Mount points for compose volumes. Named so a missing mount is loud, not silent.
RUN mkdir -p logs snapshots data models clips

# YOLO downloads weights to /root/.config/Ultralytics by default; nudge it to
# the models volume so weight re-download doesn't happen on every rebuild.
ENV YOLO_CONFIG_DIR=/app/models \
    PYTHONUNBUFFERED=1 \
    DETECTOR_INTERNAL_HOST=0.0.0.0 \
    DETECTOR_INTERNAL_PORT=8101

# 8101 is the internal HTTP surface. Only exposed to the compose network —
# do NOT publish to the host in docker-compose.yml.
EXPOSE 8101

CMD ["python", "-m", "src.detector_service"]
