"""Live-first ordering for the embed_queue (Phase 2a review follow-up).

Phase 2a's single FIFO made a live rodent alert wait behind whatever the
backfill had already enqueued — up to EMBEDDER_QUEUE_MAX (10k) notifies,
~10 minutes at 18 alerts/s. The operator looks at live alerts NOW; the
backfill is nobody's deadline.

## Wire format

    pg_notify('embed_queue', '4242')        → alert 4242, priority high
    pg_notify('embed_queue', '4242:low')    → alert 4242, priority low

Bare id = high keeps every existing publisher (StateDB.append_alert,
older backfill scripts) correct without a change. Only
scripts/backfill_embeddings.py opts into `:low`.

## Two bounded queues, strict priority at dequeue

    LISTEN thread ──► high: queue.Queue(maxsize=N) ──┐
                  ──► low:  queue.Queue(maxsize=N) ──┴──► collect() → worker

`collect()` builds one micro-batch: drain everything already waiting in
`high`, THEN spend the coalescing window on `low`, re-checking `high`
between polls so a live alert that lands mid-window still rides this
batch. A live alert therefore waits for at most the CLIP call already in
flight (~1 s for a batch of 16), never for the backlog.

Separate maxsizes (not one PriorityQueue) so a full low queue can never
cause a high notify to be dropped — **bulkhead between the two traffic
classes**. Drop semantics are unchanged from 2a: at-most-once, a Full
raises to the listener, which logs and lets the backfill reconcile.

Kept free of numpy / torch / psycopg imports so tests can exercise it
on a bare runner.
"""
from __future__ import annotations

import queue
import time

PRIORITY_HIGH = "high"
PRIORITY_LOW = "low"
_PRIORITIES = (PRIORITY_HIGH, PRIORITY_LOW)

# Poll granularity while waiting on `low` so a `high` arrival is noticed
# quickly; 50 ms is small next to the 250 ms coalescing window and the
# ~1 s CLIP batch, and costs ~nothing when idle.
_POLL_S = 0.05


def parse_payload(payload: str) -> tuple[int, str]:
    """'4242' → (4242, 'high'); '4242:low' → (4242, 'low').
    Raises ValueError for anything else (listener logs + drops)."""
    body, _, prio = payload.strip().partition(":")
    alert_id = int(body)  # ValueError on junk, same as Phase 2a
    prio = prio.strip().lower() or PRIORITY_HIGH
    if prio not in _PRIORITIES:
        raise ValueError(f"unknown embed_queue priority {prio!r} in payload {payload!r}")
    return alert_id, prio


def format_payload(alert_id: int, priority: str = PRIORITY_HIGH) -> str:
    """Inverse of parse_payload; publishers use this so the two never drift."""
    if priority not in _PRIORITIES:
        raise ValueError(f"unknown priority {priority!r}")
    return str(int(alert_id)) if priority == PRIORITY_HIGH else f"{int(alert_id)}:{priority}"


class PriorityQueues:
    def __init__(self, high_max: int, low_max: int) -> None:
        self.high: "queue.Queue[int]" = queue.Queue(maxsize=high_max)
        self.low: "queue.Queue[int]" = queue.Queue(maxsize=low_max)

    # ── producer side ─────────────────────────────────────────────────────

    def put_nowait(self, alert_id: int, priority: str = PRIORITY_HIGH) -> None:
        """Raises queue.Full — caller decides how to report the drop."""
        (self.high if priority == PRIORITY_HIGH else self.low).put_nowait(alert_id)

    def qsize(self) -> int:
        return self.high.qsize() + self.low.qsize()

    def sizes(self) -> tuple[int, int]:
        return self.high.qsize(), self.low.qsize()

    # ── consumer side ─────────────────────────────────────────────────────

    def _get_first(self, timeout: float) -> int | None:
        """Block up to `timeout` for ANY item, high preferred."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.high.get_nowait()
            except queue.Empty:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return self.low.get(timeout=min(remaining, _POLL_S))
            except queue.Empty:
                continue

    def collect(self, batch_size: int, batch_wait_s: float, first_timeout: float = 1.0) -> list[int]:
        """One micro-batch: high items already queued first, then fill
        from low for up to `batch_wait_s`, promoting any high arrival."""
        first = self._get_first(first_timeout)
        if first is None:
            return []
        batch = [first]
        # Everything already waiting in high rides now — no waiting.
        while len(batch) < batch_size:
            try:
                batch.append(self.high.get_nowait())
            except queue.Empty:
                break
        deadline = time.monotonic() + batch_wait_s
        while len(batch) < batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(self.high.get_nowait())
                continue
            except queue.Empty:
                pass
            try:
                batch.append(self.low.get(timeout=min(remaining, _POLL_S)))
            except queue.Empty:
                continue
        return batch
