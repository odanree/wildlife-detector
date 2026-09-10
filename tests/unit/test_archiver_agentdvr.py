"""Unit tests for the AgentDVR chunk picker in ClipArchiver.

Covers the path-translation adapter: filename → local wall-clock start,
"pick the latest chunk with start ≤ target" invariant, and the
open-chunk race guard that skips a pull when the desired end hasn't
been flushed to disk yet.

Ffmpeg itself is not exercised — the tests only touch the chunk-
selection logic that decides which file to feed ffmpeg and at what
offset. Wiring is a separate integration concern.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.archiver.clip_archiver import ClipArchiver


LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture()
def archiver(tmp_path: Path) -> ClipArchiver:
    return ClipArchiver(clips_dir=tmp_path / "clips")


def _touch_chunk(dirpath: Path, name: str, mtime: float | None = None) -> Path:
    p = dirpath / name
    p.write_bytes(b"")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_picks_latest_chunk_before_target(archiver: ClipArchiver, tmp_path: Path) -> None:
    """Given three consecutive chunks, the picker returns the one whose
    start ≤ target and is the newest such — not the chunk that starts
    AFTER the target, even if it's the closest in absolute distance."""
    d = tmp_path / "agentdvr"
    d.mkdir()
    _touch_chunk(d, "4_2026-09-09_21-57-01_000.mkv")
    expected = _touch_chunk(d, "4_2026-09-09_22-12-02_000.mkv")
    _touch_chunk(d, "4_2026-09-09_22-27-03_000.mkv")

    # Target: 22:20:00 local — inside the middle chunk.
    target = datetime(2026, 9, 9, 22, 20, 0, tzinfo=LA)
    got = archiver._pick_agentdvr_chunk(d, target)

    assert got is not None
    assert got[0] == expected
    assert got[1] == datetime(2026, 9, 9, 22, 12, 2, tzinfo=LA)


def test_returns_none_when_all_chunks_are_future(
    archiver: ClipArchiver, tmp_path: Path,
) -> None:
    d = tmp_path / "agentdvr"
    d.mkdir()
    _touch_chunk(d, "4_2026-09-09_22-12-02_000.mkv")
    _touch_chunk(d, "4_2026-09-09_22-27-03_000.mkv")

    # Target predates every chunk — nothing to return.
    target = datetime(2026, 9, 9, 21, 0, 0, tzinfo=LA)
    assert archiver._pick_agentdvr_chunk(d, target) is None


def test_ignores_unrelated_files(archiver: ClipArchiver, tmp_path: Path) -> None:
    """Thumbnails, xml sidecars, subdirectories, and files whose regex
    doesn't match should be silently skipped, not raise or bias the
    selection."""
    d = tmp_path / "agentdvr"
    d.mkdir()
    (d / "thumbnails").mkdir()
    _touch_chunk(d, "4_2026-09-09_22-12-02_000.mkv")
    _touch_chunk(d, "4_2026-09-09_22-12-02_000.mkv.xml")   # sidecar
    _touch_chunk(d, "not-an-agentdvr-file.mkv")
    _touch_chunk(d, "9_notadate_foo.mkv")

    target = datetime(2026, 9, 9, 22, 30, 0, tzinfo=LA)
    got = archiver._pick_agentdvr_chunk(d, target)
    assert got is not None
    assert got[0].name == "4_2026-09-09_22-12-02_000.mkv"


def test_alert_ts_utc_maps_to_pacific(archiver: ClipArchiver, monkeypatch) -> None:
    """Alert timestamps are unix UTC; AgentDVR filenames are local
    wall-clock. The converter must apply NVR_TZ (default Pacific)
    or the picker will silently point at the wrong chunk."""
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")

    # 2026-09-10 05:31:58 UTC == 2026-09-09 22:31:58 PDT (UTC-7 in summer).
    alert_ts = datetime(2026, 9, 10, 5, 31, 58, tzinfo=ZoneInfo("UTC")).timestamp()
    got = archiver._alert_ts_to_local(alert_ts)

    assert got.year == 2026 and got.month == 9 and got.day == 9
    assert got.hour == 22 and got.minute == 31 and got.second == 58


# ── Gap vs open-chunk discrimination (issue #183) ──────────────────────


def test_stale_chunk_with_alert_past_close_marks_permanent_failure(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """AgentDVR was down at the alert's timestamp — the covering chunk
    was closed hours ago and mtime freezes at close time. The alert's
    desired end lies past that close time, meaning no footage exists.
    Verdict: permanent failure, tombstone written, no future retry."""
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")
    d = tmp_path / "agentdvr"
    d.mkdir()

    # Chunk started 8 hours ago local time and closed 15 min after.
    chunk = _touch_chunk(d, "4_2026-09-09_14-00-00_000.mkv")
    close_local = datetime(2026, 9, 9, 14, 14, 35, tzinfo=LA)
    close_mtime = close_local.timestamp()
    os.utime(chunk, (close_mtime, close_mtime))

    # Alert 3 minutes AFTER close — falls in the gap between this chunk
    # and whatever came next (nothing, in this test's setup).
    alert_local = datetime(2026, 9, 9, 14, 17, 0, tzinfo=LA)
    alert_ts = alert_local.timestamp()

    out_path = archiver.clip_path(1234, alert_ts)
    result = archiver._pull_from_agentdvr(
        alert_id=1234,
        alert_ts=alert_ts,
        camera_id="crawlspace",
        agentdvr_dir=str(d),
        out_path=out_path,
    )

    assert result is None
    # Tombstone written — future backfill runs will skip.
    tomb = archiver.failure_path(1234, alert_ts)
    assert tomb.exists()
    assert "agentdvr_gap" in tomb.read_text()
    assert archiver.is_permanent_failure(1234, alert_ts)


def test_fresh_chunk_still_open_does_not_tombstone(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """AgentDVR is currently writing the covering chunk. Desired end
    is past current mtime because content hasn't been flushed yet.
    Verdict: retry-later — NO tombstone, backfill will pick it up
    once the chunk closes."""
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")
    d = tmp_path / "agentdvr"
    d.mkdir()

    # Chunk started 5 min ago and is still being written — mtime is now.
    now = time.time()
    chunk_start_local = datetime.fromtimestamp(now - 300, tz=LA).replace(microsecond=0)
    chunk_name = f"4_{chunk_start_local.strftime('%Y-%m-%d_%H-%M-%S')}_000.mkv"
    chunk = _touch_chunk(d, chunk_name)
    os.utime(chunk, (now, now))   # freshly-written

    # Alert 10 seconds ago — desired end (alert_ts+45) is slightly ahead
    # of what's on disk. Classic open-chunk race.
    alert_ts = now - 10.0

    out_path = archiver.clip_path(5678, alert_ts)
    result = archiver._pull_from_agentdvr(
        alert_id=5678,
        alert_ts=alert_ts,
        camera_id="crawlspace",
        agentdvr_dir=str(d),
        out_path=out_path,
    )

    assert result is None
    # No tombstone — backfill should retry after chunk closes.
    assert not archiver.failure_path(5678, alert_ts).exists()
    assert not archiver.is_permanent_failure(5678, alert_ts)


def test_no_covering_chunk_marks_permanent_failure(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """Picker returns no chunk (alert predates AgentDVR history or the
    covering file rotated off). Permanent failure — no future run will
    invent footage. Tombstone written."""
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")
    d = tmp_path / "agentdvr"
    d.mkdir()
    _touch_chunk(d, "4_2026-09-09_22-00-00_000.mkv")  # only chunk

    # Alert timestamp is well before any chunk.
    alert_ts = datetime(2026, 9, 9, 18, 0, 0, tzinfo=LA).timestamp()
    out_path = archiver.clip_path(9999, alert_ts)
    result = archiver._pull_from_agentdvr(
        alert_id=9999,
        alert_ts=alert_ts,
        camera_id="crawlspace",
        agentdvr_dir=str(d),
        out_path=out_path,
    )

    assert result is None
    tomb = archiver.failure_path(9999, alert_ts)
    assert tomb.exists()
    assert "no_agentdvr_chunk" in tomb.read_text()


def test_submit_skips_permanent_failure(
    archiver: ClipArchiver, tmp_path: Path,
) -> None:
    """A tombstoned alert should never enter the pool — even a fresh
    submit() call is a fast no-op, not another pull attempt."""
    alert_id = 4242
    alert_ts = time.time() - 3600  # arbitrary past alert
    archiver._mark_permanent_failure(alert_id, alert_ts, reason="test_gap")

    calls: list[tuple[int, object]] = []
    archiver.submit(alert_id, alert_ts, camera_id="crawlspace",
                    on_done=lambda aid, path: calls.append((aid, path)))

    # on_done should fire immediately with path=None; pool should never
    # have been asked to do work.
    assert calls == [(alert_id, None)]
