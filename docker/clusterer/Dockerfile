# Clusterer — bounded-context batch service that turns alert_embeddings
# (Phase 2a, CLIP vectors) into a stable `alerts.rat_id` + `rats` catalog
# via HDBSCAN + a centroid-matching linker. Phase 2b of the multi-camera
# rat re-identification project.
#
# Structural twin of docker/archiver/Dockerfile and docker/embedder/
# Dockerfile: one Python process, no camera bindings, no detector runtime.
# Unlike the embedder it is NOT a LISTEN daemon — it runs as a nightly
# batch (`--every-hours 24`, see docker-compose.yml `clusterer-timer`) or
# as a one-shot (`docker compose run --rm clusterer ...`).
#
# No torch, no CLIP weights — this image never sees an image file, it
# only reads vectors the embedder already wrote. ~350 MB (sklearn is the
# bulk; hdbscan itself is small).

FROM python:3.11-slim

WORKDIR /app

# libgomp1: OpenMP runtime for hdbscan's / sklearn's parallel core-
# distance computation.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY docker/clusterer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the modules the clusterer imports — mirrors the archiver/embedder
# copy-what-you-import discipline. scripts/ is bind-mounted by compose so
# backfill_rat_ids.py can be edited on the host and re-run without a
# rebuild (same convention as the web image + backfill_tp_clips.py).
COPY src/__init__.py ./src/__init__.py
COPY src/clusterer ./src/clusterer

# PYTHONPATH so `python scripts/backfill_rat_ids.py` (bind-mounted) can
# import src.clusterer without the script's own sys.path bootstrap.
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

CMD ["python", "-m", "src.clusterer.main"]
