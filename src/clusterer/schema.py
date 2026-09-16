"""rats / rat_cluster_runs / alerts.rat_id / HNSW DDL — idempotent, applied
at clusterer startup.

Follows the Phase 2a split exactly (see src/embedder/schema.py):

* `alerts.rat_id` is a plain nullable BIGINT with NO FK to rats. The web
  sidecar reads it, so StateDB._migrate issues the identical ADD COLUMN;
  both sites MUST stay byte-identical in type. No FK because retire /
  merge of a rat must never cascade into the alerts table — alerts are
  the source of truth, rat identity is derived state layered on top.
* Everything that mentions `vector` (rats.centroid, the HNSW index) lives
  HERE, inside the clusterer's bounded context, so detectors + web keep
  working against a Postgres without pgvector. A missing extension fails
  ONE container, loudly, at startup.

## HNSW index on alert_embeddings.embedding

    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 128)

HNSW over IVFFlat: the corpus is ~17k rows and grows by a few hundred a
day, so build cost is seconds either way, and HNSW has no "lists" knob
that goes stale as the table grows (IVFFlat needs periodic REINDEX to
re-center). HNSW also gives better recall at the same latency for the
ANN queries the clusterer issues repeatedly (per-cluster neighbour
lookups against already-assigned alerts, see clusterer.py).

  m = 16              pgvector default. Graph degree; recall vs. index
                      size. At 512 dims and 17k rows the default is
                      already ~99% recall for top-10 with ef_search=100;
                      raising m buys nothing measurable here and inflates
                      the index (~linear in m).
  ef_construction=128 2x the default (64). Controls how many candidates
                      are examined while inserting each row — higher
                      means a better-connected graph and higher recall at
                      query time for a strictly one-off build cost. On
                      17k rows the build is < 30 s at 128; the query-time
                      recall gain is the whole reason to pay it.

The index uses `vector_cosine_ops` because Phase 2b's metric is cosine.
Embeddings are unit-norm so `<=>` (cosine distance) and `<#>` (negative
inner product) rank identically, but naming the metric on the index
keeps `ORDER BY embedding <=> $q` planner-eligible without a cast dance.

Query-time knobs are set per session in clusterer.py:
  SET hnsw.ef_search = 100        -- candidate list size (recall/latency)
  SET hnsw.iterative_scan = relaxed_order
      -- pgvector 0.8: keep walking the graph until LIMIT is satisfied
      -- AFTER the WHERE filter. Our neighbour queries filter on
      -- `alerts.rat_id IS NOT NULL`, which excludes most rows early in a
      -- backfill; without iterative scan a filtered HNSW query can
      -- return fewer rows than LIMIT (post-filter starvation).

## Tables

    rats(
        id             BIGSERIAL PRIMARY KEY,
        first_seen     TIMESTAMPTZ NOT NULL,   -- min(alerts.ts) over members
        last_seen      TIMESTAMPTZ NOT NULL,   -- max(alerts.ts) over members
        alert_count    INT NOT NULL,           -- count(*) over members
        primary_camera TEXT,                   -- mode(camera_id) over members
        centroid       vector(512) NOT NULL,   -- unit-norm mean of member embeddings
        retired_at     TIMESTAMPTZ,            -- soft-retire after retire_days without a match
        notes          TEXT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )

Every column except id/notes/retired_at is RECOMPUTED from alerts +
alert_embeddings after each run (`refresh_rat_stats`), never incremented.
That is what makes re-running a window idempotent: the same members
produce the same aggregates no matter how many times the run repeats.

    rat_cluster_runs(
        id, ran_at, algo, params JSONB, window_start, window_end,
        rats_active INT, rats_new INT, rats_retired INT, notes TEXT
    )

One row per run, written in the same transaction as the rat_id UPDATEs.
The backfill uses (window_start, window_end, algo) to skip windows it has
already covered; a bad run is auditable via params + notes.
"""
from __future__ import annotations

import logging

import psycopg

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 512

HNSW_M = 16
HNSW_EF_CONSTRUCTION = 128
HNSW_INDEX_NAME = "idx_alert_embeddings_hnsw_cos"

# alerts.rat_id is co-owned with StateDB._migrate() (detector + web
# startup) — same startup-ordering rationale as alerts.bbox in Phase 2a.
# Both sites MUST stay byte-identical in type.
ALERTS_RAT_ID_DDL = "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS rat_id BIGINT"

DDL = [
    ALERTS_RAT_ID_DDL,
    # Partial index: the web's /api/rats and the clusterer's neighbour
    # queries only ever look at assigned rows; NULLs stay out of the index.
    "CREATE INDEX IF NOT EXISTS idx_alerts_rat_id ON alerts(rat_id) WHERE rat_id IS NOT NULL",
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"""
    CREATE TABLE IF NOT EXISTS rats (
        id             BIGSERIAL PRIMARY KEY,
        first_seen     TIMESTAMPTZ NOT NULL,
        last_seen      TIMESTAMPTZ NOT NULL,
        alert_count    INT NOT NULL DEFAULT 0,
        primary_camera TEXT,
        centroid       vector({EMBEDDING_DIM}) NOT NULL,
        retired_at     TIMESTAMPTZ,
        notes          TEXT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Body-size proxy — median (bbox_area / frame_area) over member alerts
    # on primary_camera. Cross-camera bbox comparison is meaningless (a big
    # rat 15 ft up looks smaller than a juvenile 4 ft under the yard cam),
    # so callers bucket adult/juvenile per-camera. Nullable: rats without a
    # primary_camera or without bbox-carrying embeddings stay NULL.
    "ALTER TABLE rats ADD COLUMN IF NOT EXISTS body_size_frac_median DOUBLE PRECISION",
    "CREATE INDEX IF NOT EXISTS idx_rats_active ON rats(last_seen DESC) WHERE retired_at IS NULL",
    """
    CREATE TABLE IF NOT EXISTS rat_cluster_runs (
        id           BIGSERIAL PRIMARY KEY,
        ran_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        algo         TEXT NOT NULL,
        params       JSONB NOT NULL,
        window_start TIMESTAMPTZ NOT NULL,
        window_end   TIMESTAMPTZ NOT NULL,
        rats_active  INT NOT NULL,
        rats_new     INT NOT NULL,
        rats_retired INT NOT NULL,
        notes        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rat_cluster_runs_window ON rat_cluster_runs(window_start, window_end)",
]

# Issued separately so we can raise maintenance_work_mem for the build.
# The HNSW graph for 17k x 512-dim rows is ~45 MB in memory; the default
# 64 MB maintenance_work_mem is borderline and pgvector falls back to a
# much slower on-disk build (with a NOTICE) when it overflows. Session-
# scoped SET, so nothing leaks to other connections.
HNSW_DDL = (
    f"CREATE INDEX IF NOT EXISTS {HNSW_INDEX_NAME} ON alert_embeddings "
    f"USING hnsw (embedding vector_cosine_ops) "
    f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
)


def ensure_schema(dsn: str) -> None:
    """Apply DDL. Each statement in its own autocommit round-trip so a
    failure logs WHICH statement tripped (same discipline as
    StateDB._migrate and the embedder's ensure_schema)."""
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        # Fail-fast at the trust boundary: no alert_embeddings table means
        # the embedder has never started. Nothing below makes sense then.
        cur.execute("SELECT to_regclass('alert_embeddings')")
        if cur.fetchone()[0] is None:
            raise RuntimeError(
                "alert_embeddings does not exist — start the embedder "
                "(docker compose up -d embedder) so Phase 2a's schema is applied first"
            )
        for stmt in DDL:
            try:
                cur.execute(stmt)
            except psycopg.Error:
                logger.error("clusterer DDL failed: %s", " ".join(stmt.split())[:120])
                raise
        cur.execute("SET maintenance_work_mem = '256MB'")
        try:
            cur.execute(HNSW_DDL)
        except psycopg.Error:
            logger.error("HNSW index DDL failed: %s", HNSW_DDL)
            raise
    logger.info(
        "clusterer schema ensured (rats, rat_cluster_runs, alerts.rat_id, %s m=%d ef_construction=%d)",
        HNSW_INDEX_NAME, HNSW_M, HNSW_EF_CONSTRUCTION,
    )


if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set")
    ensure_schema(dsn)
