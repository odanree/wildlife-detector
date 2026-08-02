"""Postgres LISTEN loop → ClipArchiver.submit adapter.

Pattern: **pub-sub via Postgres LISTEN/NOTIFY** — same shape as the
beacon-mcp watcher. The web service publishes `pg_notify('archive_queue',
str(alert_id))` after a TP label commits; this loop consumes those
events in the archiver container and hands each alert to the archiver's
bounded thread pool for ffmpeg pull.

## Delivery semantics

LISTEN/NOTIFY is **at-most-once**: if no listener is connected when the
NOTIFY fires (archiver container down, DB failover mid-tx, reconnect
race), the event is silently dropped. This is acceptable for us because
the backfill script (scripts/backfill_tp_clips.py) walks
`verdict='correct' AND local clip missing` and catches up any misses —
same eventual-consistency safety net as any pub-sub-plus-reconcile
system. We do NOT add a durable outbox table for MVP; the backfill
covers the failure mode with less code.

## Reconnect

psycopg's connection is not auto-reconnecting. On any exception we
close, sleep a short backoff, and re-LISTEN. This is a **restart-loop
resiliency** pattern rather than a proper connection health check —
fine for a background daemon whose worst case is a few seconds of
missed notifies (which the backfill will recover).

## Alert lookup

The NOTIFY payload is just the alert_id (small, fits Postgres's 8000B
payload cap easily). The listener queries the DB for ts + camera_id
per event — one extra roundtrip per label but keeps the notify payload
tiny and future-proof (we can widen the archive criteria without
changing the publisher).
"""
from __future__ import annotations

import logging
import os
import select
import time
from typing import Optional

import psycopg
from psycopg.rows import dict_row

from src.archiver.clip_archiver import ClipArchiver

logger = logging.getLogger(__name__)

CHANNEL = "archive_queue"
RECONNECT_BACKOFF_SECONDS = 5.0
# psycopg needs a periodic wake to check for stop signals; also gives the
# reconnect loop a chance to notice a silently-dead connection.
POLL_TIMEOUT_SECONDS = 30.0


class ArchiveListener:
    """Blocking LISTEN loop. Call `run()` from a foreground main or a
    thread; call `stop()` to break the loop between poll cycles."""

    def __init__(self, dsn: str, archiver: ClipArchiver) -> None:
        self.dsn = dsn
        self.archiver = archiver
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        while not self._stopped:
            try:
                self._connect_and_listen()
            except Exception:
                logger.exception("Listener: unhandled exception; reconnecting")
                time.sleep(RECONNECT_BACKOFF_SECONDS)

    def _connect_and_listen(self) -> None:
        # autocommit is required for LISTEN — otherwise notifies are held
        # inside the implicit transaction and never delivered.
        conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        try:
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {CHANNEL}")
            logger.info("Listener: LISTEN %s active", CHANNEL)
            while not self._stopped:
                # Wait on the socket for POLL_TIMEOUT_SECONDS. select
                # returns as soon as data is available, so most iterations
                # wake immediately on notify; only idle periods block.
                r, _, _ = select.select([conn], [], [], POLL_TIMEOUT_SECONDS)
                if not r:
                    continue
                # Drain all pending notifies.
                for notify in conn.notifies(stop_after=None, timeout=0):
                    self._dispatch(notify.payload)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _dispatch(self, payload: str) -> None:
        try:
            alert_id = int(payload)
        except ValueError:
            logger.warning("Listener: non-integer payload dropped: %r", payload)
            return
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, ts, camera_id FROM alerts WHERE id = %s", (alert_id,))
            row = cur.fetchone()
        if not row:
            logger.warning("Listener: alert_id=%d not found in DB", alert_id)
            return
        self.archiver.submit(
            alert_id=row["id"],
            alert_ts=float(row["ts"]),
            camera_id=row["camera_id"] or "yard",
        )


def _dsn_from_env() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set — archiver requires a Postgres conninfo")
    return dsn


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from pathlib import Path

    clips_dir = Path(os.environ.get("CLIPS_DIR", "/app/clips"))
    archiver = ClipArchiver(clips_dir=clips_dir)
    logger.info("Archiver service starting: clips_dir=%s", clips_dir)
    ArchiveListener(_dsn_from_env(), archiver).run()


if __name__ == "__main__":
    main()
