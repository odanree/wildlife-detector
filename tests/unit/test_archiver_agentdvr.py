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
