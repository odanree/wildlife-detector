"""Postgres-backed persistent state for the wildlife detector.

Migrated from SQLite in the postgres-migration PR. Motivation: SQLite +
Docker Desktop 9P bind-mount deadlocks (3 outages in ~10 days) — any
host-side Python opening the DB left the 9P layer with cached lock
state, causing container `unable to open database file` on next start.
Postgres is TCP-only, no filesystem lock contract to violate.

## Concurrency contract

- **Connection pool via psycopg_pool.ConnectionPool.** Autocommit off;
  each `with pool.connection()` block is a transaction that commits on
  clean exit, rolls back on exception. Pool serializes acquisition so
  we don't need a Python-level write lock.
- **Multi-writer safe.** Postgres MVCC handles concurrent writes across
  the two detector containers + web sidecar. No more single-writer
  discipline needed (the old SQLite `_write_lock` is gone).
- **Row factory: dict_row.** Cursors return `dict` per row so consumers
  can `row["id"]` without column-index bookkeeping.

## Idempotency

The `alerts` table has a UNIQUE (ts, species, COALESCE(snapshot, ''))
constraint. `INSERT ... ON CONFLICT DO NOTHING` makes the disk-backfill
idempotent — walking `snapshots/YYYY-MM-DD/*.jpg` on every startup and
inserting each entry never produces duplicates.

## Migration

See `scripts/migrate_sqlite_to_postgres.py` for the one-shot import
from the old SQLite `state.db`. Preserves alert IDs so downstream
references (snapshot filenames, external caches) don't break.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

# LISTEN/NOTIFY channel the embedder service consumes (src/embedder/main.py).
# Payload is the stringified alert id. Kept here — not in src/embedder —
# because the detector image doesn't ship that package.
EMBED_QUEUE_CHANNEL = "embed_queue"


class StateDB:
    """Postgres wrapper for the wildlife detector's persistent state.

    Instantiate once at process start, share the instance across the
    pipeline and the Flask web sidecar. Pool holds 2-10 connections;
    scale `max_size` up if the sidecar becomes a bottleneck under
    heavy SSE traffic.
    """

    def __init__(self, conninfo: str | None = None) -> None:
        # DATABASE_URL is the standard convention (12-factor, etc.) —
        # accept explicit `conninfo` arg for tests, fall back to env.
        self._conninfo = conninfo or os.environ.get("DATABASE_URL")
        if not self._conninfo:
            raise ValueError(
                "DATABASE_URL not set — postgres migration requires it. "
                "See docker-compose.yml and .env.example."
            )
        # Pool is opened lazily on first use; explicit `open()` here so
        # a bad conninfo fails at StateDB construction rather than at
        # first query. `open=True` also means the pool blocks until the
        # min_size is ready — combined with docker-compose's
        # `depends_on: postgres { condition: service_healthy }`, we
        # never hit "connection refused" at startup.
        self._pool = ConnectionPool(
            conninfo=self._conninfo,
            min_size=2,
            max_size=10,
            open=True,
            timeout=30.0,
        )
        self._migrate()
        logger.info(
            "StateDB opened via psycopg pool (schema v%d, alerts=%d)",
            _SCHEMA_VERSION,
            self.total_alerts(),
        )

    # ── Schema ──────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        """Create tables + indexes if missing. Postgres supports
        `ADD COLUMN IF NOT EXISTS` natively so we skip SQLite's
        introspect-then-add dance. Never DROPs.
        """
        # BIGSERIAL for id so we can preserve migrated IDs via `setval` —
        # the migration script sets the sequence to MAX(id)+1 after
        # importing SQLite rows, so new inserts don't collide.
        self._exec("""
            CREATE TABLE IF NOT EXISTS alerts (
                id           BIGSERIAL PRIMARY KEY,
                ts           DOUBLE PRECISION NOT NULL,
                camera_id    TEXT NOT NULL DEFAULT 'yard',
                species      TEXT NOT NULL,
                confidence   DOUBLE PRECISION,
                description  TEXT,
                snapshot     TEXT,
                track_id     INTEGER,
                yolo_conf    DOUBLE PRECISION,
                is_rodent    BOOLEAN NOT NULL DEFAULT FALSE,
                historical   BOOLEAN NOT NULL DEFAULT FALSE,
                created_at   DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())
            )
        """)
        # Additive columns for the human-in-the-loop labeling workflow.
        # Postgres 9.6+: ADD COLUMN IF NOT EXISTS is native, no
        # introspection needed.
        for col_ddl in [
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS label_verdict TEXT",
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS label_species TEXT",
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS label_notes   TEXT",
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS label_ts      DOUBLE PRECISION",
            # Rat re-id Phase 2a: detector bbox in snapshot-frame pixel
            # coords, `{"x1","y1","x2","y2","frame_w","frame_h"}`. The
            # snapshot JPEG is the ANNOTATED frame (red outline burned
            # in); the embedder needs the raw region to crop, and the
            # description column only ever stored WxH. NULL for every
            # row written before this column existed — the embedder
            # falls back to red-outline recovery for those
            # (src/embedder/crop.py).
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS bbox          JSONB",
            # Rat re-id Phase 2b: stable individual id assigned by the
            # nightly clusterer (src/clusterer). Nullable, NOT an FK to
            # `rats` — retire/merge must never cascade into alerts. The
            # web sidecar reads it (/api/rats), so the column is co-owned
            # here and in src/clusterer/schema.py (byte-identical, same
            # startup-ordering rationale as bbox). The `rats` table itself
            # needs pgvector and lives ONLY in the clusterer's schema.
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS rat_id        BIGINT",
        ]:
            self._exec(col_ddl)
        # Indexes — one DDL statement each so a partial failure logs
        # which index tripped.
        for idx_ddl in [
            "CREATE INDEX IF NOT EXISTS idx_alerts_ts       ON alerts(ts DESC)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_species  ON alerts(species)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_snapshot ON alerts(snapshot)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_camera   ON alerts(camera_id)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_label_ts ON alerts(label_ts)",
            # Idempotent-backfill guard — matches the SQLite unique
            # index (ts, species, snapshot-or-empty). ON CONFLICT
            # targets this constraint.
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_alerts_ts_species_snap "
            "  ON alerts(ts, species, COALESCE(snapshot, ''))",
        ]:
            self._exec(idx_ddl)

        # ── Pre-VLM drop labels (project #137 Phase 3, follow-up) ────
        # Insect pre-filter drops don't get alert rows (by design —
        # they never reach the operator via the VLM path). But we
        # sample them into logs/pre_vlm_drops.jsonl + crops on disk,
        # and this table stores hand-labels for training a pre-VLM
        # classifier that can eventually retire the brightness-
        # threshold treadmill (`NIGHT_INSECT_BRIGHTNESS_MIN`).
        #
        # `drop_id` = the crop's relative path under logs/pre_vlm_drops
        # (e.g. `yard/2026-08-25/1724567890_track1234.jpg`). Same as the
        # `snapshot` field in the JSONL row — stable, deterministic,
        # doesn't require a separate ID column in the JSONL.
        self._exec("""
            CREATE TABLE IF NOT EXISTS pre_vlm_drop_labels (
                drop_id       TEXT PRIMARY KEY,
                label         TEXT NOT NULL,
                label_species TEXT,
                label_notes   TEXT,
                labeled_at    DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
                labeled_by    TEXT NOT NULL DEFAULT 'operator'
            )
        """)
        for idx_ddl in [
            "CREATE INDEX IF NOT EXISTS idx_pre_vlm_drop_labels_label ON pre_vlm_drop_labels(label)",
            "CREATE INDEX IF NOT EXISTS idx_pre_vlm_drop_labels_labeled_at ON pre_vlm_drop_labels(labeled_at DESC)",
        ]:
            self._exec(idx_ddl)

    # ── Writes ──────────────────────────────────────────────────────────────

    def append_alert(
        self,
        species: str,
        ts: float | None = None,
        confidence: float | None = None,
        description: str | None = None,
        snapshot: str | None = None,
        track_id: int | None = None,
        yolo_conf: float | None = None,
        is_rodent: bool = False,
        historical: bool = False,
        camera_id: str = "yard",
        bbox: tuple[int, int, int, int] | None = None,
        frame_size: tuple[int, int] | None = None,
    ) -> int | None:
        """Insert an alert row. Returns the row ID, or None if the unique
        constraint suppressed it (already exists).

        `bbox` is the detector's (x1, y1, x2, y2) in the same pixel space
        as the saved snapshot; `frame_size` is that frame's (w, h). Both
        are persisted together as JSONB so a later INPUT_WIDTH change
        can't silently misplace the crop (the embedder rescales when the
        recorded frame size disagrees with the JPEG on disk).

        ## embed_queue publisher (rat re-id Phase 2a)

        After a LIVE rodent row lands (is_rodent AND NOT historical —
        disk-backfilled rows are re-driven by scripts/backfill_embeddings.py
        instead) we `pg_notify('embed_queue', id)` so the embedder
        container picks it up. Same shape as web_service's
        `archive_queue` publish. The notify rides in the SAME transaction
        as the INSERT: Postgres only delivers NOTIFY on commit, so a
        listener can never observe an id that isn't visible yet, and a
        rolled-back insert publishes nothing. A notify failure must not
        fail the alert — the row is the source of truth, the embedding is
        derived state the backfill can rebuild.
        """
        bbox_json = None
        if bbox is not None:
            x1, y1, x2, y2 = (int(v) for v in bbox)
            bbox_json = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
            if frame_size is not None:
                bbox_json["frame_w"] = int(frame_size[0])
                bbox_json["frame_h"] = int(frame_size[1])
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO alerts
                   (ts, camera_id, species, confidence, description, snapshot,
                    track_id, yolo_conf, is_rodent, historical, bbox)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (ts, species, COALESCE(snapshot, '')) DO NOTHING
                   RETURNING id""",
                (
                    ts if ts is not None else time.time(),
                    camera_id,
                    species,
                    round(confidence, 3) if confidence is not None else None,
                    description,
                    snapshot,
                    track_id,
                    round(yolo_conf, 3) if yolo_conf is not None else None,
                    is_rodent,
                    historical,
                    Jsonb(bbox_json) if bbox_json is not None else None,
                ),
            )
            row = cur.fetchone()
            alert_id = int(row[0]) if row else None
            if alert_id is not None and is_rodent and not historical and snapshot:
                try:
                    cur.execute("SELECT pg_notify(%s, %s)", (EMBED_QUEUE_CHANNEL, str(alert_id)))
                except Exception:
                    # Never let derived-state plumbing fail the alert.
                    logger.exception("embed_queue NOTIFY failed for alert=%d", alert_id)
            return alert_id

    def append_alerts_bulk(self, rows: list[dict]) -> int:
        """Batch insert for backfill. Returns the number of new rows
        actually inserted (existing rows are silently skipped).

        Uses a single multi-VALUES INSERT so a 5k-row backfill pays one
        round-trip to Postgres instead of 5k. Chunks at 500 rows to keep
        the parameter count comfortably under Postgres's 65535 cap
        (500 * 10 cols = 5000 params per statement).
        """
        if not rows:
            return 0
        for r in rows:
            r.setdefault("camera_id", "yard")
            # Coerce int-shaped booleans (backfill callers still pass 0/1
            # from the legacy SQLite schema) to Python bool so psycopg
            # binds them as the BOOLEAN type postgres expects.
            r["is_rodent"] = bool(r.get("is_rodent", False))
            r["historical"] = bool(r.get("historical", False))

        cols = (
            "ts", "camera_id", "species", "confidence", "description",
            "snapshot", "track_id", "yolo_conf", "is_rodent", "historical",
        )
        row_placeholder = "(" + ", ".join(["%s"] * len(cols)) + ")"
        chunk_size = 500

        inserted = 0
        with self._pool.connection() as conn, conn.cursor() as cur:
            for chunk_start in range(0, len(rows), chunk_size):
                chunk = rows[chunk_start:chunk_start + chunk_size]
                params: list[Any] = []
                for r in chunk:
                    params.extend(r.get(c) for c in cols)
                values_sql = ", ".join([row_placeholder] * len(chunk))
                cur.execute(
                    f"INSERT INTO alerts "  # noqa: S608 -- cols are compile-time constants
                    f"({', '.join(cols)}) "
                    f"VALUES {values_sql} "
                    f"ON CONFLICT (ts, species, COALESCE(snapshot, '')) DO NOTHING "
                    f"RETURNING id",
                    params,
                )
                inserted += len(cur.fetchall())
        return inserted

    # ── Reads ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_alerts_filter(
        species: str | None,
        since_ts: float | None,
        until_ts: float | None,
        camera_id: str | None,
        scope: str | None,
        label_filter: str | None,
        label_species: str | None,
    ) -> tuple[str, list[Any]]:
        """Shared WHERE-clause builder used by list_alerts + count_alerts_filtered.
        Returns (where_sql, params). where_sql is empty string when no
        filters are active — caller appends verbatim (no leading space)."""
        clauses: list[str] = []
        params: list[Any] = []
        if species:
            clauses.append("species = %s")
            params.append(species.lower())
        if since_ts is not None:
            clauses.append("ts >= %s")
            params.append(since_ts)
        if until_ts is not None:
            clauses.append("ts <= %s")
            params.append(until_ts)
        if camera_id:
            clauses.append("camera_id = %s")
            params.append(camera_id)
        if scope == "historical":
            clauses.append("historical = TRUE")
        elif scope == "live":
            clauses.append("historical = FALSE")
        if label_filter == "unlabeled":
            clauses.append("label_ts IS NULL")
        elif label_filter == "labeled":
            clauses.append("label_ts IS NOT NULL")
        elif label_filter in ("correct", "incorrect", "unclear"):
            clauses.append("label_verdict = %s")
            params.append(label_filter)
        elif label_filter == "needs-species":
            clauses.append("label_verdict = 'correct' AND label_species IS NULL")
        if label_species:
            clauses.append("label_species = %s")
            params.append(label_species)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where, params

    def list_alerts(
        self,
        limit: int = 200,
        species: str | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
        camera_id: str | None = None,
        scope: str | None = None,
        label_filter: str | None = None,
        label_species: str | None = None,
    ) -> list[dict]:
        """Return alerts, newest first. Same filter semantics as the
        SQLite version — see the pre-migration docstring for scope /
        label_filter meanings."""
        where, params = self._build_alerts_filter(
            species, since_ts, until_ts, camera_id, scope, label_filter, label_species,
        )
        query = f"SELECT * FROM alerts{where} ORDER BY ts DESC LIMIT %s"
        params.append(int(limit))
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return [self._normalize(row) for row in cur.fetchall()]

    def count_alerts_filtered(
        self,
        species: str | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
        camera_id: str | None = None,
        scope: str | None = None,
        label_filter: str | None = None,
        label_species: str | None = None,
    ) -> int:
        """Count of alerts matching the SAME filter set as list_alerts
        (minus limit). Used by /api/alerts to report a filter-aware
        `total` in the header instead of the system-wide total_alerts
        which was misleading — filtering by date / label / species
        would still show `total 31745` regardless of what the operator
        was actually looking at."""
        where, params = self._build_alerts_filter(
            species, since_ts, until_ts, camera_id, scope, label_filter, label_species,
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM alerts{where}", params)
            return int(cur.fetchone()[0])

    def list_rats(
        self,
        camera_id: str | None = None,
        since_hours: float | None = None,
        include_retired: bool = False,
    ) -> list[dict]:
        """Rats (Phase 2b catalog) with at least one member alert matching
        the filter — "how many distinct rats crossed <camera> in the last
        N hours" straight from SQL, no ML at query time.

        Per rat: catalog columns (first_seen / last_seen / alert_count /
        primary_camera are GLOBAL, across all cameras and all time) plus
        `window_alert_count` / `window_last_ts` scoped to the filter, and
        `sample_snapshot` = the rat's FIRST alert's snapshot path (smart
        picking is a Phase 2c concern).

        Raises psycopg.errors.UndefinedTable if the clusterer has never
        run (the `rats` table is created by src/clusterer/schema.py, not
        here) — callers translate that into a 503.
        """
        clauses = ["a.rat_id IS NOT NULL"]
        params: list = []
        if camera_id:
            clauses.append("a.camera_id = %s")
            params.append(camera_id)
        if since_hours is not None:
            clauses.append("a.ts >= %s")
            params.append(time.time() - float(since_hours) * 3600.0)
        if not include_retired:
            clauses.append("r.retired_at IS NULL")
        sql = f"""
            SELECT r.id, r.first_seen, r.last_seen, r.alert_count, r.primary_camera, r.retired_at,
                   r.notes,
                   COUNT(*)  AS window_alert_count,
                   MAX(a.ts) AS window_last_ts,
                   (SELECT s.snapshot FROM alerts s
                     WHERE s.rat_id = r.id AND s.snapshot IS NOT NULL
                     ORDER BY s.ts ASC LIMIT 1) AS sample_snapshot
            FROM rats r
            JOIN alerts a ON a.rat_id = r.id
            WHERE {' AND '.join(clauses)}
            GROUP BY r.id
            ORDER BY window_last_ts DESC, r.id
        """  # noqa: S608 -- clauses are compile-time constants
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    # Columns the operator UI needs from a `rats` row. `centroid` is
    # deliberately excluded — 512 floats the browser never reads, and the
    # web image doesn't register the pgvector adapter.
    _RAT_COLS = "id, first_seen, last_seen, alert_count, primary_camera, retired_at, notes"
    # `r.`-qualified variant for statements with a FROM-subquery whose
    # aliases collide (merge_rats's `agg.primary_camera` made a bare
    # RETURNING primary_camera ambiguous → 500 on the first live merge).
    _RAT_COLS_R = ", ".join(f"r.{c}" for c in _RAT_COLS.split(", "))

    def get_rat(self, rat_id: int, alerts_limit: int = 500) -> dict | None:
        """One rat + its per-camera alert distribution + newest-first
        member alerts (Phase 2c timeline). `camera_frequency` is a
        percentage over ALL member alerts, not just the capped page, so
        the histogram stays honest for a 1,000-alert rat viewed at 500.

        Returns None when the rat does not exist. Raises
        psycopg.errors.UndefinedTable before the clusterer's first run
        (callers map that to 503, same as list_rats).
        """
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {self._RAT_COLS} FROM rats WHERE id = %s", (int(rat_id),))
            rat = cur.fetchone()
            if rat is None:
                return None
            cur.execute(
                "SELECT camera_id, COUNT(*) AS n FROM alerts WHERE rat_id = %s "
                "GROUP BY camera_id ORDER BY n DESC, camera_id",
                (int(rat_id),),
            )
            per_cam = [(r["camera_id"], int(r["n"])) for r in cur.fetchall()]
            cur.execute(
                "SELECT id, ts, camera_id, species, confidence, is_rodent, historical, "
                "description, snapshot, track_id, label_verdict, label_species "
                "FROM alerts WHERE rat_id = %s ORDER BY ts DESC, id DESC LIMIT %s",
                (int(rat_id), int(alerts_limit)),
            )
            alerts = [self._normalize(r) for r in cur.fetchall()]
        total = sum(n for _, n in per_cam)
        out = dict(rat)
        out["camera_frequency"] = {
            cam: round(100.0 * n / total, 1) for cam, n in per_cam
        } if total else {}
        out["alerts"] = alerts
        out["alerts_total"] = total
        return out

    def rename_rat(self, rat_id: int, name: str | None) -> dict | None:
        """Set the operator-facing name (stored in `rats.notes` — the one
        free-text column the clusterer never recomputes). Empty/whitespace
        → NULL so the UI falls back to "Rat #<id>". Returns the updated
        row, or None if the rat does not exist."""
        clean = (name or "").strip() or None
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE rats SET notes = %s, updated_at = now() WHERE id = %s "
                f"RETURNING {self._RAT_COLS}",
                (clean, int(rat_id)),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def merge_rats(self, source_id: int, target_id: int) -> dict:
        """Operator escape hatch for clusterer over-splits: fold `source`
        into `target`. ONE transaction (the pool's connection context —
        commit on clean exit, rollback on any exception), so a crash
        mid-merge leaves both rats exactly as they were:

            SELECT ... FOR UPDATE         both rows, ascending id — a fixed
                                          lock order so two concurrent
                                          merges can't deadlock
            UPDATE alerts SET rat_id      source → target
            UPDATE rats (source)          retired_at = now(), alert_count = 0
                                          (derived state; truth is 0 members)
            UPDATE rats (target)          first/last_seen, alert_count,
                                          primary_camera RECOMPUTED from the
                                          merged member set — same "derived,
                                          never incremented" rule as the
                                          clusterer's refresh_rat_stats
            INSERT rat_cluster_runs       audit row, algo='operator_merge'

        Durability across the nightly re-cluster: every moved alert now
        carries rat_id = target, and the linker's member vote (step 0)
        inherits the majority prior id — so the next run re-links that
        cluster to `target`, not back to the retired `source`. The
        target's `centroid` is NOT touched here (no pgvector adapter in
        the web image); the nightly refresh recomputes it from members.

        Idempotency: a retired source (already merged) → ValueError, so a
        double-submitted merge is a 400, not a silent no-op that moves
        zero rows and re-stamps retired_at. Returns
        {source_id, target_id, moved, target: <row>}.

        Raises LookupError (404) if either rat is missing, ValueError
        (400) for source == target, retired source, or retired target.
        """
        source_id, target_id = int(source_id), int(target_id)
        if source_id == target_id:
            raise ValueError("cannot merge a rat into itself")
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {self._RAT_COLS} FROM rats WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                ([source_id, target_id],),
            )
            rows = {int(r["id"]): dict(r) for r in cur.fetchall()}
            if source_id not in rows:
                raise LookupError(f"source rat {source_id} not found")
            if target_id not in rows:
                raise LookupError(f"target rat {target_id} not found")
            if rows[source_id]["retired_at"] is not None:
                raise ValueError(f"rat {source_id} is already retired — was it merged already?")
            if rows[target_id]["retired_at"] is not None:
                raise ValueError(f"target rat {target_id} is retired; merge into an active rat")

            cur.execute("UPDATE alerts SET rat_id = %s WHERE rat_id = %s", (target_id, source_id))
            moved = cur.rowcount
            cur.execute(
                "UPDATE rats SET retired_at = now(), alert_count = 0, updated_at = now() WHERE id = %s",
                (source_id,),
            )
            cur.execute(
                f"""
                UPDATE rats r SET
                    first_seen     = to_timestamp(agg.first_ts),
                    last_seen      = to_timestamp(agg.last_ts),
                    alert_count    = agg.n,
                    primary_camera = agg.primary_camera,
                    updated_at     = now()
                FROM (
                    SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS n,
                           MODE() WITHIN GROUP (ORDER BY camera_id) AS primary_camera
                    FROM alerts WHERE rat_id = %s
                ) agg
                WHERE r.id = %s
                RETURNING {self._RAT_COLS_R}
                """,
                (target_id, target_id),
            )
            target = cur.fetchone()
            cur.execute(
                """INSERT INTO rat_cluster_runs
                     (algo, params, window_start, window_end, rats_active, rats_new, rats_retired, notes)
                   VALUES ('operator_merge', %s, now(), now(),
                           (SELECT COUNT(*) FROM rats WHERE retired_at IS NULL), 0, 1, %s)""",
                (
                    Jsonb({"source_rat_id": source_id, "target_rat_id": target_id}),
                    f"operator merge: rat {source_id} → rat {target_id}, {moved} alerts moved"
                    + (f" (source name: {rows[source_id]['notes']!r})" if rows[source_id]["notes"] else ""),
                ),
            )
        return {
            "source_id": source_id,
            "target_id": target_id,
            "moved": int(moved),
            "target": dict(target) if target else None,
        }

    def latest_alert(self) -> dict | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM alerts ORDER BY ts DESC LIMIT 1")
            row = cur.fetchone()
            return self._normalize(row) if row else None

    def get_alert(self, alert_id: int) -> dict | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM alerts WHERE id = %s LIMIT 1", (int(alert_id),))
            row = cur.fetchone()
            return self._normalize(row) if row else None

    def set_label(
        self,
        alert_id: int,
        verdict: str | None,
        species: str | None = None,
        notes: str | None = None,
    ) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            if verdict is None:
                cur.execute(
                    "UPDATE alerts SET label_verdict=NULL, label_species=NULL, "
                    "label_notes=NULL, label_ts=NULL WHERE id=%s",
                    (int(alert_id),),
                )
            else:
                cur.execute(
                    "UPDATE alerts SET label_verdict=%s, label_species=%s, "
                    "label_notes=%s, label_ts=%s WHERE id=%s",
                    (verdict, species, notes, time.time(), int(alert_id)),
                )
            return cur.rowcount > 0

    def notify(self, channel: str, payload: str) -> None:
        """Publish on a Postgres LISTEN/NOTIFY channel. Used by the
        clip archiver (`archive_queue`) — see src/archiver/listener.py.
        Payload must be ≤8000 bytes (Postgres NOTIFY limit); we
        currently only send stringified alert IDs so we're nowhere near.

        Uses the pg_notify() function rather than the NOTIFY statement
        because the SQL statement form requires literal payload (no
        parameter binding), while the function accepts parameters. Same
        semantics, safe with untrusted payload strings.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            # Channel name still can't be parameterized — pg_notify's
            # first arg is text but Postgres syntax doesn't let us bind
            # an identifier. Restrict to safe chars.
            if not channel.replace("_", "").isalnum():
                raise ValueError(f"invalid channel name: {channel!r}")
            cur.execute("SELECT pg_notify(%s, %s)", (channel, payload))

    def set_labels_bulk(
        self,
        alert_ids: list[int],
        verdict: str | None,
        species: str | None = None,
        notes: str | None = None,
    ) -> int:
        if not alert_ids:
            return 0
        ids_tuple = tuple(int(x) for x in alert_ids)
        with self._pool.connection() as conn, conn.cursor() as cur:
            if verdict is None:
                cur.execute(
                    "UPDATE alerts SET label_verdict=NULL, label_species=NULL, "
                    "label_notes=NULL, label_ts=NULL WHERE id = ANY(%s)",
                    (list(ids_tuple),),
                )
            else:
                cur.execute(
                    "UPDATE alerts SET label_verdict=%s, label_species=%s, "
                    "label_notes=%s, label_ts=%s WHERE id = ANY(%s)",
                    (verdict, species, notes, time.time(), list(ids_tuple)),
                )
            return cur.rowcount

    def list_unlabeled(
        self,
        limit: int = 50,
        camera_id: str | None = None,
        scope: str = "historical",
    ) -> list[dict]:
        query = "SELECT * FROM alerts WHERE label_ts IS NULL"
        params: list[Any] = []
        if scope == "historical":
            query += " AND historical = TRUE"
        elif scope == "live":
            query += " AND historical = FALSE"
        if camera_id:
            query += " AND camera_id = %s"
            params.append(camera_id)
        query += " ORDER BY ts DESC LIMIT %s"
        params.append(int(limit))
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return [self._normalize(row) for row in cur.fetchall()]

    def list_labeled_for_export(
        self,
        include_species: list[str] | None = None,
        exclude_species: list[str] | None = None,
        include_unclear: bool = False,
        camera_id: str | None = None,
        verdict: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM alerts WHERE label_ts IS NOT NULL"
        params: list[Any] = []
        if not include_unclear:
            query += " AND (label_verdict != 'unclear' OR label_verdict IS NULL)"
        if verdict:
            query += " AND label_verdict = %s"
            params.append(verdict)
        if camera_id:
            query += " AND camera_id = %s"
            params.append(camera_id)
        if include_species:
            query += " AND species = ANY(%s)"
            params.append(list(include_species))
        if exclude_species:
            query += " AND species != ALL(%s)"
            params.append(list(exclude_species))
        query += " ORDER BY ts ASC"
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return [self._normalize(row) for row in cur.fetchall()]

    def label_counts(self, include_historical: bool = True) -> dict:
        where = "" if include_historical else " WHERE historical = FALSE"
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT COALESCE(label_verdict, 'unlabeled') AS v, COUNT(*) AS n "
                f"FROM alerts{where} GROUP BY v"
            )
            return {r[0]: r[1] for r in cur.fetchall()}

    def total_alerts(self, camera_id: str | None = None) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            if camera_id:
                cur.execute("SELECT COUNT(*) FROM alerts WHERE camera_id = %s", (camera_id,))
            else:
                cur.execute("SELECT COUNT(*) FROM alerts")
            return int(cur.fetchone()[0])

    def unlabeled_alerts(self, camera_id: str | None = None) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            if camera_id:
                cur.execute(
                    "SELECT COUNT(*) FROM alerts WHERE camera_id = %s AND label_verdict IS NULL",
                    (camera_id,),
                )
            else:
                cur.execute("SELECT COUNT(*) FROM alerts WHERE label_verdict IS NULL")
            return int(cur.fetchone()[0])

    def snapshots_present(self) -> set[str]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT snapshot FROM alerts WHERE snapshot IS NOT NULL")
            return {row[0] for row in cur.fetchall()}

    # ── Pre-VLM drop labels (Phase 3 follow-up) ─────────────────────────
    # These are hand-labels on rows the insect pre-filter dropped BEFORE
    # the VLM saw them. Feed into a future pre-VLM binary classifier
    # that can retire the brightness-threshold treadmill.

    _DROP_LABELS_ALLOWED = {"moth", "real_animal", "unclear"}

    def set_drop_label(
        self,
        drop_id: str,
        label: str | None,
        label_species: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """Upsert a label on a pre-VLM drop. `label=None` deletes the
        row (undo). Returns True on any state change."""
        if label is not None and label not in self._DROP_LABELS_ALLOWED:
            raise ValueError(
                f"label must be one of {sorted(self._DROP_LABELS_ALLOWED)} or None"
            )
        with self._pool.connection() as conn, conn.cursor() as cur:
            if label is None:
                cur.execute("DELETE FROM pre_vlm_drop_labels WHERE drop_id = %s", (drop_id,))
                return cur.rowcount > 0
            cur.execute(
                """INSERT INTO pre_vlm_drop_labels
                     (drop_id, label, label_species, label_notes, labeled_at)
                   VALUES (%s, %s, %s, %s, EXTRACT(EPOCH FROM NOW()))
                   ON CONFLICT (drop_id) DO UPDATE SET
                     label         = EXCLUDED.label,
                     label_species = EXCLUDED.label_species,
                     label_notes   = EXCLUDED.label_notes,
                     labeled_at    = EXTRACT(EPOCH FROM NOW())""",
                (drop_id, label, label_species, notes),
            )
            return True

    def get_drop_labels(self, drop_ids: list[str]) -> dict[str, dict]:
        """Bulk fetch labels for a set of drop_ids. Returns a dict keyed
        by drop_id — missing keys mean unlabeled. Used by /api/drops so
        the frontend can render existing labels on paginated pages."""
        if not drop_ids:
            return {}
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT drop_id, label, label_species, label_notes, labeled_at
                   FROM pre_vlm_drop_labels WHERE drop_id = ANY(%s)""",
                (list(drop_ids),),
            )
            return {
                r[0]: {
                    "label": r[1],
                    "label_species": r[2],
                    "label_notes": r[3],
                    "labeled_at": float(r[4]) if r[4] is not None else None,
                }
                for r in cur.fetchall()
            }

    def drop_label_counts(self) -> dict[str, int]:
        """Histogram of labels — used by the /api/drops list to show
        progress in the UI header (`labeled: N / M`)."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT label, COUNT(*) FROM pre_vlm_drop_labels GROUP BY label")
            return {r[0]: int(r[1]) for r in cur.fetchall()}

    # ── Housekeeping ────────────────────────────────────────────────────────

    def close(self) -> None:
        self._pool.close()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _exec(self, sql: str, params: tuple | None = None) -> None:
        """Fire-and-forget for DDL and migrations. Autocommit via
        `with pool.connection()` context manager."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params or ())

    @staticmethod
    def _normalize(row: dict | None) -> dict | None:
        """Postgres returns BOOLEAN as Python bool already; SQLite returned
        integers. Normalize just in case any consumer relies on 0/1 vs
        True/False semantics — return native bool consistently."""
        if row is None:
            return None
        d = dict(row)
        if "is_rodent" in d:
            d["is_rodent"] = bool(d["is_rodent"])
        if "historical" in d:
            d["historical"] = bool(d["historical"])
        return d
