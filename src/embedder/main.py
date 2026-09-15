"""Embedder service entrypoint: Postgres LISTEN loop → micro-batched CLIP.

Pattern: **pub-sub via Postgres LISTEN/NOTIFY**, same shape as
src/archiver/listener.py. StateDB.append_alert publishes
`pg_notify('embed_queue', str(alert_id))` after every live rodent insert;
scripts/backfill_embeddings.py re-drives history on the same channel.

## Two threads, one bounded queue

    LISTEN thread ──push alert_id──► queue.Queue(maxsize=N) ──► worker thread
                                                                 (collects ≤ BATCH_SIZE
                                                                  or waits BATCH_WAIT_S,
                                                                  then embed_batch)

The archiver dispatches each notify straight into its thread pool
because ffmpeg pulls are independent. CLIP is the opposite: a batch of
16 on CPU costs ~1.3x a batch of 1, so coalescing notifies into
micro-batches is the whole throughput story for the 29k backfill. This
is a **debounce coalescer**: drain whatever arrived within BATCH_WAIT_S,
cap at BATCH_SIZE, run once.

## Delivery semantics

At-most-once, identical to the archiver: a notify that lands while the
container is down, or while the queue is full, is dropped. The backfill
script is the reconcile pass (it walks alert_embeddings for gaps), so we
skip a durable outbox for MVP. Queue-full drops log at WARNING with the
count so a backfill that outruns the worker is visible, not silent.

## Reconnect

psycopg doesn't auto-reconnect. Any exception in the LISTEN loop →
close, sleep RECONNECT_BACKOFF_SECONDS, re-LISTEN. Restart-loop
resiliency, not health checking — fine for a background daemon whose
worst case is a few seconds of missed notifies.
"""
from __future__ import annotations

import logging
import os
import queue
import select
import threading
import time
from pathlib import Path

import psycopg

from src.embedder.embedder import DEFAULT_MODEL_ID, AlertEmbedder, BatchStats
from src.embedder.schema import ensure_schema

logger = logging.getLogger(__name__)

CHANNEL = "embed_queue"
RECONNECT_BACKOFF_SECONDS = 5.0
POLL_TIMEOUT_SECONDS = 30.0


def _env_int(name: str, default: int) -> int:
    # `or default` not `getenv(name, default)`: compose env_file passes
    # set-but-empty strings, and int("") raises.
    return int(os.environ.get(name) or default)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


class EmbedWorker(threading.Thread):
    """Drains the queue in micro-batches and calls embed_batch."""

    def __init__(
        self,
        embedder: AlertEmbedder,
        q: "queue.Queue[int]",
        batch_size: int,
        batch_wait_s: float,
    ) -> None:
        super().__init__(name="embed-worker", daemon=True)
        self.embedder = embedder
        self.q = q
        self.batch_size = batch_size
        self.batch_wait_s = batch_wait_s
        self._stopped = threading.Event()
        self.totals = BatchStats()

    def stop(self) -> None:
        self._stopped.set()

    def _collect(self) -> list[int]:
        """Block for the first item, then sweep up to batch_size more
        that arrive within batch_wait_s."""
        try:
            first = self.q.get(timeout=1.0)
        except queue.Empty:
            return []
        batch = [first]
        deadline = time.monotonic() + self.batch_wait_s
        while len(batch) < self.batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(self.q.get(timeout=remaining))
            except queue.Empty:
                break
        return batch

    def run(self) -> None:
        batches = 0
        while not self._stopped.is_set():
            batch = self._collect()
            if not batch:
                continue
            try:
                stats = self.embedder.embed_batch(batch)
            except Exception:
                logger.exception("embed-worker: batch of %d failed; dropping", len(batch))
                continue
            self.totals.merge(stats)
            batches += 1
            per = stats.infer_ms / stats.embedded if stats.embedded else 0.0
            logger.info(
                "embedded %d (skip=%d nf=%d nosnap=%d fail=%d) infer=%.0fms (%.0fms/alert) "
                "total=%.0fms queue=%d src=%s",
                stats.embedded, stats.skipped_existing, stats.not_found,
                stats.missing_snapshot, stats.failed, stats.infer_ms, per,
                stats.total_ms, self.q.qsize(), stats.sources or "-",
            )
            if batches % 50 == 0:
                t = self.totals
                logger.info(
                    "cumulative: embedded=%d skipped=%d missing=%d failed=%d avg_infer=%.0fms/alert",
                    t.embedded, t.skipped_existing, t.missing_snapshot + t.not_found, t.failed,
                    (t.infer_ms / t.embedded) if t.embedded else 0.0,
                )


class EmbedListener:
    """Blocking LISTEN loop; pushes alert_ids onto the worker queue."""

    def __init__(self, dsn: str, q: "queue.Queue[int]") -> None:
        self.dsn = dsn
        self.q = q
        self._stopped = False
        self._dropped = 0

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
        # autocommit is required for LISTEN — see archiver/listener.py.
        conn = psycopg.connect(self.dsn, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {CHANNEL}")
            logger.info("Listener: LISTEN %s active", CHANNEL)
            while not self._stopped:
                r, _, _ = select.select([conn], [], [], POLL_TIMEOUT_SECONDS)
                if not r:
                    continue
                for notify in conn.notifies(stop_after=None, timeout=0):
                    self._dispatch(notify.payload)
        finally:
            try:
                conn.close()
            except (OSError, psycopg.Error) as e:
                logger.warning("Listener: conn.close() failed at teardown: %s", e)

    def _dispatch(self, payload: str) -> None:
        try:
            alert_id = int(payload)
        except ValueError:
            logger.warning("Listener: non-integer payload dropped: %r", payload)
            return
        try:
            self.q.put_nowait(alert_id)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 500 == 0:
                logger.warning(
                    "Listener: queue full (%d), dropped alert_id=%d (dropped so far: %d). "
                    "Backfill will reconcile; slow the publisher (--sleep-ms) if this persists.",
                    self.q.maxsize, alert_id, self._dropped,
                )


def _dsn_from_env() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set — embedder requires a Postgres conninfo")
    return dsn


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL") or "INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    dsn = _dsn_from_env()
    snapshots_dir = Path(os.environ.get("SNAPSHOT_DIR") or "/app/snapshots")
    model_id = os.environ.get("CLIP_MODEL_ID") or DEFAULT_MODEL_ID
    batch_size = _env_int("EMBEDDER_BATCH_SIZE", 16)
    batch_wait_s = _env_float("EMBEDDER_BATCH_WAIT_S", 0.25)
    queue_max = _env_int("EMBEDDER_QUEUE_MAX", 10000)

    # Fail-fast at the trust boundary: no pgvector → no service. Better
    # a crash-looping container than a listener that swallows every
    # notify and writes nothing.
    ensure_schema(dsn)

    embedder = AlertEmbedder(dsn=dsn, snapshots_dir=snapshots_dir, model_id=model_id)
    q: "queue.Queue[int]" = queue.Queue(maxsize=queue_max)
    worker = EmbedWorker(embedder, q, batch_size=batch_size, batch_wait_s=batch_wait_s)
    worker.start()
    logger.info(
        "Embedder service starting: snapshots=%s batch=%d wait=%.2fs queue_max=%d",
        snapshots_dir, batch_size, batch_wait_s, queue_max,
    )
    EmbedListener(dsn, q).run()


if __name__ == "__main__":
    main()
