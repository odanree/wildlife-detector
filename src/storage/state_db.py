"""SQLite-backed persistent state for the wildlife detector.

Phase 1 of the three-plane split (see docs/adr/002-three-plane-process-split.md).
This module owns the alerts table today; more tables can land here without a
schema migration story because we use ``CREATE TABLE IF NOT EXISTS`` and only
ever add columns / indexes (never rename or drop in-place).

## Concurrency contract

- **Single writer, multi-reader.** The detector process (and its VLM harvest
  thread) is the only writer. Flask worker threads read via the same
  connection. WAL mode makes reads non-blocking against writes.
- The wrapper serializes writes with a threading.Lock so two detector-side
  callers can't interleave INSERTs. Reads are lock-free (SQLite handles it).
- ``check_same_thread=False`` so Flask worker threads can read from the same
  connection. Safe because we don't share cursors across threads.

## Idempotency

The ``alerts`` table has a UNIQUE (ts, species, snapshot) constraint with
``ON CONFLICT IGNORE``. This makes the disk-backfill idempotent — walking
``snapshots/YYYY-MM-DD/*.jpg`` on every startup and inserting each entry
never produces duplicates, so we no longer need a separate "have I already
loaded this?" check.

Live inserts get a unique ts from time.time() at write moment, so they never
collide with the backfill or each other.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


# Schema version bakes into the pragmas — bump if we ever need to migrate
# with real ALTER TABLE (not just an additive CREATE IF NOT EXISTS).
_SCHEMA_VERSION = 1


class StateDB:
    """Thread-safe SQLite wrapper for the wildlife detector's persistent state.

    Single-writer discipline is enforced by convention (pipeline is the only
    caller of append_alert). Reads are safe from any thread.

    Instantiate once at process start, share the instance across the pipeline
    and the Flask preview server.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None → autocommit; we manage transactions manually
        # if needed (currently we don't, each INSERT auto-commits).
        # check_same_thread=False so Flask worker threads can read.
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        # WAL: readers don't block writers, writers don't block readers.
        # NORMAL sync: durability trades a tiny fsync-per-transaction for
        # ~4x write throughput. Safe unless the OS crashes mid-write.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._write_lock = threading.Lock()
        self._migrate()
        logger.info("StateDB opened at %s (WAL, schema v%d, alerts=%d)",
                    self._path, _SCHEMA_VERSION, self.total_alerts())

    # ── Schema ──────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        """Create tables if missing + additive schema migrations. Never DROPs.

        Migration strategy: introspect existing columns; if a column we need
        is absent, ALTER TABLE ADD COLUMN. New DBs get the full schema via the
        CREATE TABLE below. Old DBs (from before multi-camera) get camera_id
        appended with a 'yard' default so existing 150+ alerts stay attributable.
        """
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL    NOT NULL,
                camera_id    TEXT    NOT NULL DEFAULT 'yard',
                species      TEXT    NOT NULL,
                confidence   REAL,
                description  TEXT,
                snapshot     TEXT,
                track_id     INTEGER,
                yolo_conf    REAL,
                is_rodent    INTEGER NOT NULL DEFAULT 0,
                historical   INTEGER NOT NULL DEFAULT 0,
                created_at   REAL    NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_ts       ON alerts(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_alerts_species  ON alerts(species);
            CREATE INDEX IF NOT EXISTS idx_alerts_snapshot ON alerts(snapshot);

            -- Idempotent-backfill guard: two rows with the same timestamp,
            -- species, and snapshot filename are treated as the same event.
            -- INSERT OR IGNORE skips duplicates silently.
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_alerts_ts_species_snap
                ON alerts(ts, species, COALESCE(snapshot, ''));
        """)
        # In-place upgrade for existing DBs — SQLite ALTER TABLE only supports
        # ADD COLUMN, so old rows get camera_id='yard' via the DEFAULT.
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(alerts)")}
        if "camera_id" not in cols:
            self._conn.execute(
                "ALTER TABLE alerts ADD COLUMN camera_id TEXT NOT NULL DEFAULT 'yard'"
            )
            logger.info("StateDB: added camera_id column (defaulted to 'yard' for existing rows)")
        # Camera-scoped queries need an index once we're multi-camera.
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_camera ON alerts(camera_id)")

        # Human-in-the-loop labeling columns — supervised training data
        # collection for a downstream binary pre-filter or LoRA fine-tune.
        # label_verdict: 'correct' | 'incorrect' | 'unclear' | None (unlabeled)
        # label_species: fine-grained tag ('real_rat', 'real_mouse',
        #   'FP:insect', 'FP:reflection', 'FP:human', 'FP:noise', ...);
        #   nullable — quick-verdict rows don't require the picker.
        # label_notes:   free-form operator context.
        # label_ts:      when the label was applied (for staleness / audit).
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(alerts)")}
        for col, ddl in [
            ("label_verdict", "ALTER TABLE alerts ADD COLUMN label_verdict TEXT"),
            ("label_species", "ALTER TABLE alerts ADD COLUMN label_species TEXT"),
            ("label_notes",   "ALTER TABLE alerts ADD COLUMN label_notes TEXT"),
            ("label_ts",      "ALTER TABLE alerts ADD COLUMN label_ts REAL"),
        ]:
            if col not in cols:
                self._conn.execute(ddl)
                logger.info("StateDB: added %s column", col)
        # Index for the "unlabeled alerts" backlog query on the batch page.
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_label_ts ON alerts(label_ts)")

    # ── Writes (detector-side only) ─────────────────────────────────────────

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
        with self._write_lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO alerts
                   (ts, camera_id, species, confidence, description, snapshot,
                    track_id, yolo_conf, is_rodent, historical)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts if ts is not None else time.time(),
                    camera_id,
                    species,
                    round(confidence, 3) if confidence is not None else None,
                    description,
                    snapshot,
                    track_id,
                    round(yolo_conf, 3) if yolo_conf is not None else None,
                    1 if is_rodent else 0,
                    1 if historical else 0,
                ),
            )
            return cur.lastrowid if cur.rowcount > 0 else None

    def append_alerts_bulk(self, rows: list[dict]) -> int:
        """Batch insert for backfill. Returns the number of new rows inserted
        (existing rows are silently skipped by the unique constraint).

        Each row dict must include camera_id; callers can fall back to 'yard'
        for the historical single-camera path."""
        if not rows:
            return 0
        # Normalize — pre-camera_id backfills omit the field; default it here
        # so the SQL bind never fails on missing key.
        for r in rows:
            r.setdefault("camera_id", "yard")
        with self._write_lock:
            cur = self._conn.executemany(
                """INSERT OR IGNORE INTO alerts
                   (ts, camera_id, species, confidence, description, snapshot,
                    track_id, yolo_conf, is_rodent, historical)
                   VALUES (:ts, :camera_id, :species, :confidence, :description, :snapshot,
                           :track_id, :yolo_conf, :is_rodent, :historical)""",
                rows,
            )
            return cur.rowcount

    # ── Reads (any thread) ──────────────────────────────────────────────────

    def list_alerts(
        self,
        limit: int = 200,
        species: str | None = None,
        since_ts: float | None = None,
        camera_id: str | None = None,
        scope: str | None = None,
        label_filter: str | None = None,
    ) -> list[dict]:
        """Return alerts, newest first. Filters push into SQL, so page size
        doesn't blow up memory. camera_id=None returns rows from ALL cameras
        (unified view); pass a specific id for per-camera pages.

        scope: 'historical' | 'live' | None (all) — pile of interest.
        label_filter: 'unlabeled' | 'labeled' | None (all) — for the
        sifting workflow: 'unlabeled' hides rows already voted on so
        operator can walk the backlog without re-reviewing their own
        work. Composes with scope: scope='historical' + label_filter=
        'unlabeled' = "the un-voted piece of the old pile"."""
        query = "SELECT * FROM alerts"
        clauses: list[str] = []
        params: list = []
        if species:
            clauses.append("species = ?")
            params.append(species.lower())
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(since_ts)
        if camera_id:
            clauses.append("camera_id = ?")
            params.append(camera_id)
        if scope == "historical":
            clauses.append("historical = 1")
        elif scope == "live":
            clauses.append("historical = 0")
        if label_filter == "unlabeled":
            clauses.append("label_ts IS NULL")
        elif label_filter == "labeled":
            clauses.append("label_ts IS NOT NULL")
        elif label_filter in ("correct", "incorrect", "unclear"):
            clauses.append("label_verdict = ?")
            params.append(label_filter)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        cur = self._conn.execute(query, params)
        return [self._row_to_dict(row) for row in cur.fetchall()]

    def latest_alert(self) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM alerts ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def get_alert(self, alert_id: int) -> dict | None:
        """Fetch a single alert by id. Used by the NVR playback endpoint
        to look up ts + camera_id from an alert row."""
        cur = self._conn.execute(
            "SELECT * FROM alerts WHERE id = ? LIMIT 1", (int(alert_id),)
        )
        row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def set_label(
        self,
        alert_id: int,
        verdict: str | None,
        species: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """Apply a human label to an alert row. Returns True if the row
        was found and updated. Pass verdict=None to clear all label fields
        (undo). label_ts is set to now() on any update."""
        import time as _time
        if verdict is None:
            cur = self._conn.execute(
                "UPDATE alerts SET label_verdict=NULL, label_species=NULL, label_notes=NULL, label_ts=NULL WHERE id=?",
                (int(alert_id),),
            )
        else:
            cur = self._conn.execute(
                "UPDATE alerts SET label_verdict=?, label_species=?, label_notes=?, label_ts=? WHERE id=?",
                (verdict, species, notes, _time.time(), int(alert_id)),
            )
        return cur.rowcount > 0

    def set_labels_bulk(
        self,
        alert_ids: list[int],
        verdict: str | None,
        species: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Apply the same label to N alerts in one transaction. Returns
        the count actually updated. Used by the mass-tag UI when operator
        selects a batch of rows and applies one verdict."""
        import time as _time
        if not alert_ids:
            return 0
        placeholders = ",".join("?" * len(alert_ids))
        if verdict is None:
            cur = self._conn.execute(
                f"UPDATE alerts SET label_verdict=NULL, label_species=NULL, label_notes=NULL, label_ts=NULL WHERE id IN ({placeholders})",
                [int(x) for x in alert_ids],
            )
        else:
            params = [verdict, species, notes, _time.time()] + [int(x) for x in alert_ids]
            cur = self._conn.execute(
                f"UPDATE alerts SET label_verdict=?, label_species=?, label_notes=?, label_ts=? WHERE id IN ({placeholders})",
                params,
            )
        return cur.rowcount

    def list_unlabeled(
        self,
        limit: int = 50,
        camera_id: str | None = None,
        scope: str = "historical",
    ) -> list[dict]:
        """Return unlabeled alerts (label_ts IS NULL) — the batch labeling
        page walks this list. scope:
          'historical' (default) → only backfilled/pre-tuning rows
                                   (historical=1). Newest first inside
                                   the historical pile.
          'live'                 → only fresh VLM-fired rows (historical=0).
          'all'                  → both, newest first."""
        query = "SELECT * FROM alerts WHERE label_ts IS NULL"
        params: list = []
        if scope == "historical":
            query += " AND historical = 1"
        elif scope == "live":
            query += " AND historical = 0"
        # scope='all' adds no filter
        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        cur = self._conn.execute(query, params)
        return [self._row_to_dict(row) for row in cur.fetchall()]

    def label_counts(self, include_historical: bool = True) -> dict:
        """Per-verdict counts + total unlabeled across ALL rows (or
        live-only when include_historical=False). Historical rows count
        toward training data now that we allow labeling them."""
        where = "" if include_historical else " WHERE historical = 0"
        rows = self._conn.execute(
            f"SELECT COALESCE(label_verdict, 'unlabeled') AS v, COUNT(*) AS n "
            f"FROM alerts{where} GROUP BY v"
        ).fetchall()
        return {r["v"]: r["n"] for r in rows}

    def total_alerts(self, camera_id: str | None = None) -> int:
        """Total alert count. Optional camera_id filter so the /api/alerts
        response's `total` field matches the filter applied to `items` —
        critical for per-camera unread-badge math (otherwise badge shows
        cross-camera drift even when the caller only wants one camera)."""
        if camera_id:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE camera_id = ?", (camera_id,)
            )
        else:
            cur = self._conn.execute("SELECT COUNT(*) FROM alerts")
        return int(cur.fetchone()[0])

    def snapshots_present(self) -> set[str]:
        """Return the set of snapshot filenames already stored — used by
        backfill to short-circuit before hitting the unique constraint."""
        cur = self._conn.execute(
            "SELECT snapshot FROM alerts WHERE snapshot IS NOT NULL"
        )
        return {row[0] for row in cur.fetchall()}

    # ── Housekeeping ────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._write_lock:
            self._conn.close()

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        # SQLite stores bool as int; convert back for frontend consumers.
        d["is_rodent"] = bool(d.get("is_rodent", 0))
        d["historical"] = bool(d.get("historical", 0))
        return d
