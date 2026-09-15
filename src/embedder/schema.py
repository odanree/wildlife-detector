"""alert_embeddings DDL — idempotent, applied at embedder startup.

## Why not Alembic

This repo has no Alembic. Every table (`alerts`, `pre_vlm_drop_labels`)
is created by idempotent `CREATE ... IF NOT EXISTS` DDL in
`StateDB._migrate()` at process start. Introducing Alembic for one table
would mean two sources of schema truth plus a new dependency in every
image. We follow the in-repo pattern instead.

## Why here and not in StateDB._migrate

`CREATE EXTENSION vector` only succeeds on a Postgres that has pgvector
installed (docker/postgres/Dockerfile). If StateDB ran it, every detector
+ the web sidecar would refuse to start against a vanilla postgres — a
5-container blast radius for a feature only the embedder consumes.
Keeping the pgvector DDL inside the embedder's bounded context means a
missing extension fails ONE container, loudly, at startup.

## Table shape (locked in the Phase 2a brief, plus two nullable columns)

    alert_embeddings(
        alert_id      BIGINT PRIMARY KEY REFERENCES alerts(id) ON DELETE CASCADE,
        embedding     vector(512) NOT NULL,   -- L2-normalized CLIP image features
        model_version TEXT NOT NULL,          -- e.g. 'openai/clip-vit-base-patch32'
        crop_bbox     JSONB,                  -- bbox actually embedded (frame coords)
        crop_source   TEXT,                   -- alerts.bbox | red_thumb | red_outline | full_frame
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )

`crop_bbox` + `crop_source` are additive provenance so Phase 2b can
down-weight `full_frame` rows and Phase 2c can draw the crop the
cluster was built from. They don't change the locked contract.

No ANN index yet (HNSW/IVFFlat) — at 30k rows exact scan is ~ms and
Phase 2b hasn't chosen its distance metric; the index choice follows it.
"""
from __future__ import annotations

import logging

import psycopg

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 512

DDL = [
    # alerts.bbox is OWNED by StateDB._migrate() (detector + web startup),
    # but the embedder reads it and may start before any detector has
    # restarted onto the code that adds it — observed on first deploy:
    # `column "bbox" does not exist`, every batch dropped. Issuing the
    # identical idempotent statement here removes the startup-ordering
    # dependency. Both sites MUST stay byte-identical in type (JSONB).
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS bbox JSONB",
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"""
    CREATE TABLE IF NOT EXISTS alert_embeddings (
        alert_id      BIGINT PRIMARY KEY REFERENCES alerts(id) ON DELETE CASCADE,
        embedding     vector({EMBEDDING_DIM}) NOT NULL,
        model_version TEXT NOT NULL,
        crop_bbox     JSONB,
        crop_source   TEXT,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_alert_embeddings_model ON alert_embeddings(model_version)",
    "CREATE INDEX IF NOT EXISTS idx_alert_embeddings_created ON alert_embeddings(created_at DESC)",
]


def ensure_schema(dsn: str) -> None:
    """Apply DDL. Each statement in its own autocommit round-trip so a
    failure logs WHICH statement tripped (same discipline as
    StateDB._migrate's per-index loop)."""
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for stmt in DDL:
            try:
                cur.execute(stmt)
            except psycopg.Error:
                logger.error("alert_embeddings DDL failed: %s", " ".join(stmt.split())[:120])
                raise
    logger.info("alert_embeddings schema ensured (pgvector, dim=%d)", EMBEDDING_DIM)


if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set")
    ensure_schema(dsn)
