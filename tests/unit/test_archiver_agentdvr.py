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


def test_stale_chunk_no_successor_marks_permanent_failure(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """AgentDVR wrote a chunk, closed it hours ago, and never came back.
    Covering chunk is stale, no successor exists. Alert's desired end
    lies past close time → permanent gap, tombstone written."""
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
    assert "latest in the directory" in tomb.read_text()
    assert archiver.is_permanent_failure(1234, alert_ts)


def test_boundary_seam_uses_next_chunk_no_tombstone(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """Alert lands seconds after chunk A closes and chunk B started ~1s
    later — a normal file-rotation seam, not a gap. Footage lives in B.
    The archiver must fall through to B and NOT tombstone. Regression
    guard from Fable's review of the first draft — the seam case would
    have permanently mislabeled ~6% of crawlspace alerts."""
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")
    d = tmp_path / "agentdvr"
    d.mkdir()

    # Chunk A: started 3 hours ago local, closed 15 min later.
    chunk_a = _touch_chunk(d, "4_2026-09-09_14-00-00_000.mkv")
    close_a_local = datetime(2026, 9, 9, 14, 14, 35, tzinfo=LA)
    close_a_mtime = close_a_local.timestamp()
    os.utime(chunk_a, (close_a_mtime, close_a_mtime))

    # Chunk B: started 1s after A closed — normal AgentDVR seam.
    chunk_b = _touch_chunk(d, "4_2026-09-09_14-14-36_500.mkv")
    close_b_local = datetime(2026, 9, 9, 14, 29, 36, tzinfo=LA)
    close_b_mtime = close_b_local.timestamp()
    os.utime(chunk_b, (close_b_mtime, close_b_mtime))

    # Alert 3s after chunk A closed — inside the seam window. Old code
    # picked A, saw desired_end > A.mtime, tombstoned as "gap". New
    # code detects the seam and slices B instead.
    alert_local = datetime(2026, 9, 9, 14, 14, 38, tzinfo=LA)
    alert_ts = alert_local.timestamp()

    out_path = archiver.clip_path(2222, alert_ts)

    # ffmpeg isn't installed in the test env; monkeypatch _slice_agentdvr_chunk
    # to record which chunk was picked instead of running the binary.
    called_with = {}
    def fake_slice(alert_id, alert_ts_, camera_id, *, chunk_path, chunk_start_local, window_start_local, out_path):
        called_with["chunk_path"] = chunk_path
        called_with["chunk_start_local"] = chunk_start_local
        return chunk_path  # Simulate success

    monkeypatch.setattr(archiver, "_slice_agentdvr_chunk", fake_slice)

    result = archiver._pull_from_agentdvr(
        alert_id=2222,
        alert_ts=alert_ts,
        camera_id="crawlspace",
        agentdvr_dir=str(d),
        out_path=out_path,
    )

    # Sliced chunk B, not A. No tombstone.
    assert result == chunk_b
    assert called_with["chunk_path"] == chunk_b
    assert not archiver.failure_path(2222, alert_ts).exists()


def test_real_gap_between_chunks_marks_permanent_failure(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """Chunk A closes, chunk B starts 15 min later — real AgentDVR
    outage (RTSP disconnect + reconnect cycle). Alert in the middle
    of that window has no footage in either chunk. Tombstone."""
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")
    d = tmp_path / "agentdvr"
    d.mkdir()

    # Chunk A: closes at 14:14:35.
    chunk_a = _touch_chunk(d, "4_2026-09-09_14-00-00_000.mkv")
    close_a_mtime = datetime(2026, 9, 9, 14, 14, 35, tzinfo=LA).timestamp()
    os.utime(chunk_a, (close_a_mtime, close_a_mtime))

    # Chunk B: starts 15 min after A's close — real gap.
    chunk_b = _touch_chunk(d, "4_2026-09-09_14-29-37_000.mkv")
    close_b_mtime = datetime(2026, 9, 9, 14, 44, 37, tzinfo=LA).timestamp()
    os.utime(chunk_b, (close_b_mtime, close_b_mtime))

    # Alert in the middle of the 15-min gap.
    alert_ts = datetime(2026, 9, 9, 14, 22, 0, tzinfo=LA).timestamp()
    out_path = archiver.clip_path(3333, alert_ts)

    result = archiver._pull_from_agentdvr(
        alert_id=3333,
        alert_ts=alert_ts,
        camera_id="crawlspace",
        agentdvr_dir=str(d),
        out_path=out_path,
    )

    assert result is None
    tomb = archiver.failure_path(3333, alert_ts)
    assert tomb.exists()
    body = tomb.read_text()
    assert "agentdvr_gap" in body
    # Reason must call out both chunks' names for forensic traceability.
    assert chunk_a.name in body
    assert chunk_b.name in body


def test_alert_inside_stale_chunk_near_seam_slices_A_not_B(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """Alert lands INSIDE chunk A but 35s before A closes. Chunk B
    exists 1s after A's close (normal seam). Old (Fable's 2nd review)
    code redirected to B and sliced from B.start — 36s after the
    alert. Wrong footage marked as success.

    New code: alert_ts <= A.mtime means alert is inside A. Slice A
    even though desired_end extends past A's close — truncated is
    correct; the wrong-chunk redirect would silently mislabel the
    clip.
    """
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")
    d = tmp_path / "agentdvr"
    d.mkdir()

    # A: closes at 14:14:35; alert at 14:14:00 (35s before close).
    chunk_a = _touch_chunk(d, "4_2026-09-09_14-00-00_000.mkv")
    close_a_mtime = datetime(2026, 9, 9, 14, 14, 35, tzinfo=LA).timestamp()
    os.utime(chunk_a, (close_a_mtime, close_a_mtime))

    # B: normal seam 1s after A's close.
    chunk_b = _touch_chunk(d, "4_2026-09-09_14-14-36_000.mkv")
    close_b_mtime = datetime(2026, 9, 9, 14, 29, 36, tzinfo=LA).timestamp()
    os.utime(chunk_b, (close_b_mtime, close_b_mtime))

    alert_ts = datetime(2026, 9, 9, 14, 14, 0, tzinfo=LA).timestamp()
    assert alert_ts < close_a_mtime, "test premise: alert inside A"
    out_path = archiver.clip_path(5555, alert_ts)

    # Patch _run_ffmpeg so we can inspect the argv without needing
    # ffmpeg installed. Assert on -i <chunk>: chunk A is the correct
    # source; chunk B would be the wrong-chunk bug from the 2nd review.
    captured: dict = {}
    def fake_run(cmd, alert_id, camera_id, out_path_, source):
        captured["cmd"] = cmd
        return out_path_

    monkeypatch.setattr(archiver, "_run_ffmpeg", fake_run)

    result = archiver._pull_from_agentdvr(
        alert_id=5555,
        alert_ts=alert_ts,
        camera_id="crawlspace",
        agentdvr_dir=str(d),
        out_path=out_path,
    )

    # Slice happened, no tombstone, and ffmpeg was invoked with A.
    assert result == out_path
    assert not archiver.failure_path(5555, alert_ts).exists()
    assert "-i" in captured["cmd"]
    input_arg = captured["cmd"][captured["cmd"].index("-i") + 1]
    assert input_arg == str(chunk_a), (
        f"expected A ({chunk_a.name}) but got {Path(input_arg).name} — "
        f"the alert_ts <= file_mtime guard failed to prevent the "
        f"wrong-chunk redirect."
    )


def test_seam_to_still_open_next_chunk_retries_later(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """Alert crosses the A→B seam, but B is the currently-open chunk
    (fresh mtime) and desired_end is past B's flushed content. Retry-
    later applies here too — must NOT slice a truncated open B and
    write it as success."""
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")
    d = tmp_path / "agentdvr"
    d.mkdir()

    now = time.time()
    # A: closed 40s ago (stale).
    chunk_a = _touch_chunk(d, "4_2026-09-09_14-00-00_000.mkv")
    close_a_mtime = now - 40.0
    os.utime(chunk_a, (close_a_mtime, close_a_mtime))

    # B: started 1s after A closed, currently being written (fresh mtime,
    # but not enough flushed for our desired_end).
    b_start = datetime.fromtimestamp(close_a_mtime + 1.0, tz=LA).replace(microsecond=0)
    chunk_b_name = f"4_{b_start.strftime('%Y-%m-%d_%H-%M-%S')}_000.mkv"
    chunk_b = _touch_chunk(d, chunk_b_name)
    os.utime(chunk_b, (now - 3.0, now - 3.0))  # fresh — being written

    # Alert 5s after A closed — inside seam, but tail past B's flushed
    # content.
    alert_ts = close_a_mtime + 5.0
    out_path = archiver.clip_path(6666, alert_ts)

    called = {"ffmpeg": False}
    def fake_run(*args, **kwargs):
        called["ffmpeg"] = True
        return out_path
    monkeypatch.setattr(archiver, "_run_ffmpeg", fake_run)

    result = archiver._pull_from_agentdvr(
        alert_id=6666,
        alert_ts=alert_ts,
        camera_id="crawlspace",
        agentdvr_dir=str(d),
        out_path=out_path,
    )

    # Retry-later: no clip, no tombstone, ffmpeg never called.
    assert result is None
    assert not called["ffmpeg"]
    assert not archiver.failure_path(6666, alert_ts).exists()


def test_empty_directory_does_not_tombstone(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """Empty AgentDVR dir (bind mount lost its backing store, or
    first-boot before any recording) must NOT tombstone. Alert stays
    retry-able so a fixed mount can recover it on next backfill."""
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")
    d = tmp_path / "agentdvr"
    d.mkdir()  # empty

    alert_ts = datetime(2026, 9, 9, 14, 15, 0, tzinfo=LA).timestamp()
    out_path = archiver.clip_path(4444, alert_ts)

    result = archiver._pull_from_agentdvr(
        alert_id=4444,
        alert_ts=alert_ts,
        camera_id="crawlspace",
        agentdvr_dir=str(d),
        out_path=out_path,
    )

    assert result is None
    assert not archiver.failure_path(4444, alert_ts).exists()
    assert not archiver.is_permanent_failure(4444, alert_ts)


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


def test_alert_predates_history_marks_permanent_failure(
    archiver: ClipArchiver, tmp_path: Path, monkeypatch,
) -> None:
    """Chunks exist but all start AFTER the alert timestamp. Alert
    predates AgentDVR onboarding for this camera (or retention rotated
    the covering chunk off). No future run will invent footage — tombstone."""
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
    assert "predates_history" in tomb.read_text()


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
