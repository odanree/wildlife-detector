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
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


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
    ) -> int | None:
        """Insert an alert row. Returns the row ID, or None if the unique
        constraint suppressed it (already exists).
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO alerts
                   (ts, camera_id, species, confidence, description, snapshot,
                    track_id, yolo_conf, is_rodent, historical)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def append_alerts_bulk(self, rows: list[dict]) -> int:
        """Batch insert for backfill. Returns the number of new rows
        actually inserted (existing rows are silently skipped)."""
        if not rows:
            return 0
        for r in rows:
            r.setdefault("camera_id", "yard")
            # Coerce int-shaped booleans (backfill callers still pass 0/1
            # from the legacy SQLite schema) to Python bool so psycopg
            # binds them as the BOOLEAN type postgres expects.
            r["is_rodent"] = bool(r.get("is_rodent", False))
            r["historical"] = bool(r.get("historical", False))
        with self._pool.connection() as conn, conn.cursor() as cur:
            inserted = 0
            for r in rows:
                cur.execute(
                    """INSERT INTO alerts
                       (ts, camera_id, species, confidence, description, snapshot,
                        track_id, yolo_conf, is_rodent, historical)
                       VALUES (%(ts)s, %(camera_id)s, %(species)s, %(confidence)s,
                               %(description)s, %(snapshot)s, %(track_id)s,
                               %(yolo_conf)s, %(is_rodent)s, %(historical)s)
                       ON CONFLICT (ts, species, COALESCE(snapshot, '')) DO NOTHING
                       RETURNING id""",
                    r,
                )
                if cur.fetchone():
                    inserted += 1
            return inserted

    # ── Reads ───────────────────────────────────────────────────────────────

    def list_alerts(
        self,
        limit: int = 200,
        species: str | None = None,
        since_ts: float | None = None,
        camera_id: str | None = None,
        scope: str | None = None,
        label_filter: str | None = None,
    ) -> list[dict]:
        """Return alerts, newest first. Same filter semantics as the
        SQLite version — see the pre-migration docstring for scope /
        label_filter meanings."""
        query = "SELECT * FROM alerts"
        clauses: list[str] = []
        params: list[Any] = []
        if species:
            clauses.append("species = %s")
            params.append(species.lower())
        if since_ts is not None:
            clauses.append("ts >= %s")
            params.append(since_ts)
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
            # Worklist filter: TPs that still need a species tag.
            # Species-tagging is a second pass over the "correct" verdict
            # set — this filter surfaces just the rows still to do so the
            # list drains as the operator labels.
            clauses.append("label_verdict = 'correct' AND label_species IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ts DESC LIMIT %s"
        params.append(int(limit))
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return [self._normalize(row) for row in cur.fetchall()]

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
