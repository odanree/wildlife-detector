# Embedder — bounded-context service that listens for `embed_queue`
# NOTIFYs, crops the animal out of the alert snapshot, runs CLIP
# ViT-B/32 and writes a 512-dim vector to alert_embeddings (pgvector).
# Phase 2a of the multi-camera rat re-identification project.
#
# Structural twin of docker/archiver/Dockerfile: one Python process on a
# LISTEN loop, no camera bindings, no detector runtime. Differences:
#   - torch (CPU wheel) + transformers for CLIP
#   - opencv-headless for the red-outline bbox recovery on historical
#     snapshots (see src/embedder/crop.py)
#   - CLIP weights baked into the image at build time under /app/hf-cache
#
# Weight caching: HF_HOME=/app/hf-cache is populated during `docker build`
# (~600MB). docker-compose mounts the named volume `clip-cache` at the
# same path — Docker seeds an EMPTY named volume from the image layer on
# first mount, so container restarts (and image rebuilds that don't touch
# the weights) never re-download. HF_HUB_OFFLINE=1 at runtime means a
# missing cache fails loud instead of silently phoning huggingface.co.
#
# Image size: ~1.6GB (torch-cpu ~750MB + CLIP ~600MB + deps).

FROM python:3.11-slim

WORKDIR /app

# libglib2.0-0: opencv-headless shared-object dep. libgomp1: torch's
# OpenMP runtime for intra-op parallelism on the CPU path.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# torch from the CPU-only index — separate layer so a requirements.txt
# edit doesn't invalidate the 750MB torch download.
RUN pip install --no-cache-dir "torch>=2.4,<3" --index-url https://download.pytorch.org/whl/cpu

COPY docker/embedder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV HF_HOME=/app/hf-cache \
    PYTHONUNBUFFERED=1 \
    TOKENIZERS_PARALLELISM=false

# Bake the CLIP weights. CLIP_MODEL_ID here MUST match the runtime
# default in src/embedder/embedder.py — if they drift, the runtime fails
# at startup with "offline mode, model not in cache" (fail-fast, by design).
ARG CLIP_MODEL_ID=openai/clip-vit-base-patch32
RUN python -c "from transformers import CLIPModel, CLIPProcessor; \
    CLIPModel.from_pretrained('${CLIP_MODEL_ID}'); \
    CLIPProcessor.from_pretrained('${CLIP_MODEL_ID}')" \
    && rm -rf /root/.cache/pip

# Only the modules the embedder imports — mirrors the archiver's
# copy-what-you-import discipline so a cv2-heavy src/detection or a
# Flask-heavy src/web never rides along.
COPY src/__init__.py ./src/__init__.py
COPY src/embedder ./src/embedder

RUN mkdir -p snapshots

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

CMD ["python", "-m", "src.embedder.main"]
