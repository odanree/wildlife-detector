"""AlertEmbedder — CLIP ViT-B/32 image features per alert, stored in pgvector.

Loads the model ONCE per process (~600MB weights, ~2s on CPU) and exposes:

    embed_alert(alert_id) -> np.ndarray | None     # single, convenience
    embed_batch(alert_ids) -> BatchStats           # the real work path

Per alert: (a) fetch the row, (b) locate the crop (alerts.bbox if the
row has it, else red-outline recovery — see crop.py), (c) run CLIP,
(d) upsert into alert_embeddings.

## Idempotency

`alert_embeddings.alert_id` is the PK. A row that already exists for
this (alert_id, model_version) is skipped before any image I/O — that's
what makes the backfill script + LISTEN path safe to overlap and safe to
re-run. A row that exists under a DIFFERENT model_version is overwritten
(`ON CONFLICT DO UPDATE`): bumping CLIP_MODEL_ID and re-running the
backfill is the re-embed path, no manual DELETE needed.

## Vector convention

Features are L2-normalized before storage, so cosine similarity ==
dot product == 1 - (pgvector `<=>` distance)/… — Phase 2b can use
either `<=>` (cosine) or `<#>` (neg inner product) interchangeably.

## CPU budget

torch intra-op threads default to 8 (EMBEDDER_TORCH_THREADS). Five
detector containers share this 24-core box; the embedder is a bulkhead
— it must never starve live detection. Measured ViT-B/32 on 8 threads:
~40-60ms/image batched at 16.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.embedder.crop import CropResult, extract_crop
from src.embedder.schema import EMBEDDING_DIM

logger = logging.getLogger(__name__)

# MUST match the ARG default in docker/embedder/Dockerfile — the image bakes
# exactly this model into HF_HOME and runs with HF_HUB_OFFLINE=1.
DEFAULT_MODEL_ID = "openai/clip-vit-base-patch32"

# Sentinel for "snapshot not on disk" so the pool map can return a value
# instead of raising (exceptions in map() surface lazily and would abort
# the whole batch on the first missing file).
_MISSING = object()


def _safe_extract(item: tuple[int, Path, Optional[dict]]):
    """Thread-pool body: CropResult | _MISSING | Exception."""
    _, path, known_bbox = item
    try:
        return extract_crop(path, known_bbox=known_bbox)
    except FileNotFoundError:
        return _MISSING
    except Exception as e:  # noqa: BLE001 -- reported per-alert by the caller
        return e


@dataclass
class BatchStats:
    requested: int = 0
    embedded: int = 0
    skipped_existing: int = 0
    not_found: int = 0
    missing_snapshot: int = 0
    failed: int = 0
    infer_ms: float = 0.0
    total_ms: float = 0.0
    sources: dict = field(default_factory=dict)

    def merge(self, other: "BatchStats") -> None:
        for k in ("requested", "embedded", "skipped_existing", "not_found",
                  "missing_snapshot", "failed", "infer_ms", "total_ms"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        for k, v in other.sources.items():
            self.sources[k] = self.sources.get(k, 0) + v


class AlertEmbedder:
    def __init__(
        self,
        dsn: str,
        snapshots_dir: Path,
        model_id: str = DEFAULT_MODEL_ID,
        torch_threads: Optional[int] = None,
        device: str = "cpu",
    ) -> None:
        self.dsn = dsn
        self.snapshots_dir = Path(snapshots_dir)
        self.model_version = model_id
        self.device = device

        # Import torch lazily so crop.py stays importable without it and
        # the unit tests don't pay the torch import on a bare runner.
        import torch
        from transformers import CLIPModel, CLIPProcessor

        self._torch = torch
        threads = torch_threads or int(os.environ.get("EMBEDDER_TORCH_THREADS") or "8")
        torch.set_num_threads(threads)
        # Crop-stage I/O pool — sized to the batch so a full batch's reads
        # all overlap. Independent of torch threads: the two stages run
        # sequentially per batch, so they never compete for the CPU cap.
        crop_workers = int(os.environ.get("EMBEDDER_CROP_WORKERS") or "16")
        self._crop_pool = ThreadPoolExecutor(max_workers=crop_workers, thread_name_prefix="crop")

        t0 = time.perf_counter()
        self._model = CLIPModel.from_pretrained(model_id).to(device).eval()
        self._processor = CLIPProcessor.from_pretrained(model_id)
        dim = int(self._model.config.projection_dim)
        if dim != EMBEDDING_DIM:
            raise RuntimeError(
                f"{model_id} projects to {dim} dims but alert_embeddings is vector({EMBEDDING_DIM})"
            )
        logger.info(
            "CLIP loaded: %s dim=%d device=%s threads=%d in %.1fs",
            model_id, dim, device, threads, time.perf_counter() - t0,
        )

    # ── Inference ─────────────────────────────────────────────────────────

    def embed_images(self, images_bgr: list[np.ndarray]) -> np.ndarray:
        """(N, 512) float32, L2-normalized. Input is a list of BGR crops
        (cv2 convention) of arbitrary sizes — the processor resizes to
        224 and center-crops."""
        from PIL import Image

        pil = [Image.fromarray(im[:, :, ::-1]) for im in images_bgr]  # BGR → RGB
        inputs = self._processor(images=pil, return_tensors="pt")
        with self._torch.inference_mode():
            feats = self._model.get_image_features(
                pixel_values=inputs["pixel_values"].to(self.device)
            )
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.cpu().numpy().astype(np.float32)

    # ── DB helpers ────────────────────────────────────────────────────────

    def _connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        register_vector(conn)
        return conn

    def fetch_alerts(self, conn: psycopg.Connection, alert_ids: Iterable[int]) -> dict[int, dict]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, snapshot, bbox, camera_id FROM alerts WHERE id = ANY(%s)",
                (list(alert_ids),),
            )
            return {int(r["id"]): r for r in cur.fetchall()}

    def already_embedded(self, conn: psycopg.Connection, alert_ids: Iterable[int]) -> set[int]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT alert_id FROM alert_embeddings "
                "WHERE alert_id = ANY(%s) AND model_version = %s",
                (list(alert_ids), self.model_version),
            )
            return {int(r["alert_id"]) for r in cur.fetchall()}

    def _upsert(self, conn: psycopg.Connection, rows: list[tuple[int, np.ndarray, CropResult]]) -> None:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO alert_embeddings
                     (alert_id, embedding, model_version, crop_bbox, crop_source, created_at)
                   VALUES (%s, %s, %s, %s, %s, now())
                   ON CONFLICT (alert_id) DO UPDATE SET
                     embedding     = EXCLUDED.embedding,
                     model_version = EXCLUDED.model_version,
                     crop_bbox     = EXCLUDED.crop_bbox,
                     crop_source   = EXCLUDED.crop_source,
                     created_at    = now()""",
                [
                    (
                        alert_id,
                        vec,
                        self.model_version,
                        Jsonb(crop.bbox.to_record(crop.frame_w, crop.frame_h)),
                        crop.source,
                    )
                    for alert_id, vec, crop in rows
                ],
            )
        conn.commit()

    # ── Public API ────────────────────────────────────────────────────────

    def embed_batch(self, alert_ids: list[int]) -> BatchStats:
        t_start = time.perf_counter()
        stats = BatchStats(requested=len(alert_ids))
        if not alert_ids:
            return stats
        ids = list(dict.fromkeys(int(a) for a in alert_ids))  # dedupe, keep order

        with self._connect() as conn:
            existing = self.already_embedded(conn, ids)
            stats.skipped_existing = len(existing)
            todo = [a for a in ids if a not in existing]
            if not todo:
                stats.total_ms = (time.perf_counter() - t_start) * 1000
                return stats

            rows = self.fetch_alerts(conn, todo)
            work: list[tuple[int, Path, Optional[dict]]] = []
            for alert_id in todo:
                row = rows.get(alert_id)
                if row is None:
                    stats.not_found += 1
                    logger.warning("embed: alert_id=%d not found", alert_id)
                    continue
                if not row["snapshot"]:
                    stats.missing_snapshot += 1
                    continue
                work.append((alert_id, self.snapshots_dir / row["snapshot"], row.get("bbox")))

            # Crop stage runs in a thread pool: each alert is 1-2 JPEG reads
            # over the Docker Desktop bind mount (slow, high-latency 9P/FUSE
            # I/O) + cv2 work that releases the GIL. Serial, this was
            # ~155ms/alert and 5x the CLIP cost; overlapped, the reads hide
            # behind each other. Order is preserved via executor.map.
            crops: list[tuple[int, CropResult]] = []
            for (alert_id, path, _), outcome in zip(
                work, self._crop_pool.map(_safe_extract, work)
            ):
                if isinstance(outcome, CropResult):
                    crops.append((alert_id, outcome))
                elif outcome is _MISSING:
                    stats.missing_snapshot += 1
                    logger.info("embed: alert_id=%d snapshot missing on disk: %s", alert_id, path)
                else:
                    stats.failed += 1
                    logger.error("embed: alert_id=%d crop failed: %r", alert_id, outcome)

            if not crops:
                stats.total_ms = (time.perf_counter() - t_start) * 1000
                return stats

            t_inf = time.perf_counter()
            vecs = self.embed_images([c.image for _, c in crops])
            stats.infer_ms = (time.perf_counter() - t_inf) * 1000

            self._upsert(conn, [(a, vecs[i], c) for i, (a, c) in enumerate(crops)])
            stats.embedded = len(crops)
            for _, c in crops:
                stats.sources[c.source] = stats.sources.get(c.source, 0) + 1

        stats.total_ms = (time.perf_counter() - t_start) * 1000
        return stats

    def embed_alert(self, alert_id: int) -> Optional[np.ndarray]:
        """Single-alert convenience. Returns the stored vector (fresh or
        pre-existing), or None if the alert couldn't be embedded."""
        self.embed_batch([alert_id])
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT embedding FROM alert_embeddings WHERE alert_id = %s AND model_version = %s",
                (int(alert_id), self.model_version),
            )
            row = cur.fetchone()
        return np.asarray(row["embedding"], dtype=np.float32) if row else None
