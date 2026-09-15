"""embed_queue live-first ordering — payload wire format + dequeue order.

Pure stdlib module under test (src/embedder/priority.py); no torch,
numpy or Postgres.
"""
from __future__ import annotations

import queue
import threading
import time

import pytest

from src.embedder.priority import (
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PriorityQueues,
    format_payload,
    parse_payload,
)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ("4242", (4242, PRIORITY_HIGH)),          # Phase 2a bare id → high (back-compat)
        (" 4242 ", (4242, PRIORITY_HIGH)),
        ("4242:low", (4242, PRIORITY_LOW)),
        ("4242:LOW", (4242, PRIORITY_LOW)),
        ("4242:high", (4242, PRIORITY_HIGH)),
    ],
)
def test_parse_payload(payload, expected):
    assert parse_payload(payload) == expected


@pytest.mark.parametrize("payload", ["", "abc", "12:urgent", ":low", "1.5"])
def test_parse_payload_rejects_junk(payload):
    with pytest.raises(ValueError):
        parse_payload(payload)


def test_format_payload_round_trips():
    for aid, prio in [(1, PRIORITY_HIGH), (99, PRIORITY_LOW)]:
        assert parse_payload(format_payload(aid, prio)) == (aid, prio)
    assert format_payload(7) == "7"  # bare id for high — old listeners still parse it


def test_collect_drains_high_before_low():
    q = PriorityQueues(high_max=100, low_max=100)
    for i in range(20):
        q.put_nowait(1000 + i, PRIORITY_LOW)
    q.put_nowait(1, PRIORITY_HIGH)
    q.put_nowait(2, PRIORITY_HIGH)
    batch = q.collect(batch_size=8, batch_wait_s=0.05)
    assert batch[:2] == [1, 2]
    assert batch[2:] == [1000, 1001, 1002, 1003, 1004, 1005]
    assert q.sizes() == (0, 14)


def test_collect_only_low_preserves_fifo():
    q = PriorityQueues(10, 10)
    for i in range(5):
        q.put_nowait(i, PRIORITY_LOW)
    assert q.collect(batch_size=16, batch_wait_s=0.05) == [0, 1, 2, 3, 4]


def test_collect_returns_empty_on_timeout():
    q = PriorityQueues(10, 10)
    t = time.monotonic()
    assert q.collect(batch_size=16, batch_wait_s=0.05, first_timeout=0.1) == []
    assert time.monotonic() - t < 1.0


def test_high_arrival_mid_window_is_promoted_into_current_batch():
    q = PriorityQueues(100, 100)
    for i in range(3):
        q.put_nowait(1000 + i, PRIORITY_LOW)

    def late_high():
        time.sleep(0.05)
        q.put_nowait(7, PRIORITY_HIGH)

    threading.Thread(target=late_high, daemon=True).start()
    # Window long enough for the high item to land; batch big enough to hold it.
    batch = q.collect(batch_size=16, batch_wait_s=0.5)
    assert 7 in batch
    assert set(batch) == {1000, 1001, 1002, 7}


def test_full_low_queue_never_blocks_high():
    q = PriorityQueues(high_max=2, low_max=1)
    q.put_nowait(100, PRIORITY_LOW)
    with pytest.raises(queue.Full):
        q.put_nowait(101, PRIORITY_LOW)
    q.put_nowait(1, PRIORITY_HIGH)  # bulkhead: low being full is irrelevant
    assert q.sizes() == (1, 1)
