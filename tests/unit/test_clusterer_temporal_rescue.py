"""Noise-rescue via spatiotemporal-locality prior — pure numpy tests."""
from __future__ import annotations

import numpy as np
import pytest

from src.clusterer.temporal_rescue import rescue_bursts


def _labels(*xs: int) -> np.ndarray:
    return np.asarray(xs, dtype=np.int64)


def _ts(*xs: float) -> np.ndarray:
    return np.asarray(xs, dtype=np.float64)


def test_empty_input_is_a_noop():
    lbl, n = rescue_bursts(_labels(), [], _ts())
    assert lbl.shape == (0,)
    assert n == 0


def test_existing_clusters_are_never_renumbered():
    lbl_in = _labels(0, 0, 1, 1, -1, -1, -1)
    lbl_out, n = rescue_bursts(
        lbl_in, ["a"] * 4 + ["b"] * 3, _ts(0, 1, 2, 3, 100, 200, 300),
        burst_window_s=300, burst_min_size=3,
    )
    # Real HDBSCAN labels are preserved verbatim; only the noise run
    # gets a fresh label above the current max.
    assert (lbl_out[:4] == lbl_in[:4]).all()
    assert n == 3
    assert (lbl_out[4:] == 2).all()   # max was 1 → next id is 2


def test_burst_needs_all_neighbours_within_window():
    # Camera A: three hits at 0/100/200s (all ≤ 300s apart) → rescued
    # Camera B: three hits at 0/500/1000s (gaps > 300s) → stay noise
    lbl = _labels(-1, -1, -1, -1, -1, -1)
    cams = ["A", "A", "A", "B", "B", "B"]
    ts = _ts(0, 100, 200, 0, 500, 1000)
    out, n = rescue_bursts(lbl, cams, ts, burst_window_s=300, burst_min_size=3)
    assert n == 3
    # A's three noise indices land on the same new label; B's stay -1.
    assert out[0] == out[1] == out[2]
    assert out[0] >= 0
    assert (out[3:] == -1).all()


def test_bursts_are_camera_scoped():
    # Interleave two cameras in time; a burst must not straddle cameras
    # even if all points are within the window.
    lbl = _labels(-1, -1, -1, -1)
    cams = ["A", "B", "A", "B"]
    ts = _ts(0, 30, 60, 90)  # all ≤ 300s apart
    out, n = rescue_bursts(lbl, cams, ts, burst_window_s=300, burst_min_size=3)
    # Each camera has only 2 same-cam noise points → below min_size.
    assert n == 0
    assert (out == -1).all()


def test_multiple_runs_on_one_camera_get_distinct_labels():
    # Two 5-min windows separated by 10 min → two distinct bursts.
    lbl = _labels(-1, -1, -1, -1, -1, -1)
    cams = ["A"] * 6
    ts = _ts(0, 60, 120, 900, 960, 1020)
    out, n = rescue_bursts(lbl, cams, ts, burst_window_s=300, burst_min_size=3)
    assert n == 6
    assert out[0] == out[1] == out[2]
    assert out[3] == out[4] == out[5]
    assert out[0] != out[3]   # distinct clusters


def test_sub_min_size_burst_stays_noise():
    lbl = _labels(-1, -1, -1)
    out, n = rescue_bursts(lbl, ["A", "A", "A"], _ts(0, 1, 2), burst_min_size=5)
    assert n == 0
    assert (out == -1).all()


def test_zero_min_size_disables_the_rescue():
    lbl_in = _labels(-1, -1, -1, 0, 0)
    out, n = rescue_bursts(
        lbl_in, ["A"] * 5, _ts(0, 1, 2, 3, 4),
        burst_window_s=300, burst_min_size=1,
    )
    assert n == 0
    assert (out == lbl_in).all()


def test_rescue_is_deterministic_across_camera_key_orders():
    # dict order was insertion order in early Python; assert we still
    # get identical labels regardless of the input row order for the
    # same (camera, ts) set.
    cams1 = ["A", "B", "A", "B", "A", "B"]
    cams2 = ["B", "A", "B", "A", "B", "A"]
    ts = _ts(0, 0, 60, 60, 120, 120)
    lbl1, _ = rescue_bursts(_labels(-1, -1, -1, -1, -1, -1), cams1, ts,
                             burst_window_s=300, burst_min_size=3)
    lbl2, _ = rescue_bursts(_labels(-1, -1, -1, -1, -1, -1), cams2, ts,
                             burst_window_s=300, burst_min_size=3)
    # Each camera should get one cluster; the id assigned to camera A
    # must be independent of whether A or B was listed first.
    a1 = {int(lbl1[i]) for i, c in enumerate(cams1) if c == "A"}
    a2 = {int(lbl2[i]) for i, c in enumerate(cams2) if c == "A"}
    assert a1 == a2


def test_mixed_real_cluster_and_noise_burst_same_camera():
    # HDBSCAN found a real cluster on camera A (label 0) between two
    # noise runs. Both runs should coalesce into fresh labels; the real
    # cluster stays label 0.
    lbl_in = _labels(-1, -1, -1, 0, 0, 0, -1, -1, -1)
    cams = ["A"] * 9
    ts = _ts(0, 60, 120, 200, 260, 320, 1000, 1060, 1120)
    out, n = rescue_bursts(lbl_in, cams, ts, burst_window_s=300, burst_min_size=3)
    assert n == 6
    assert (out[:3] == out[0]).all() and out[0] > 0
    assert (out[3:6] == 0).all()
    assert (out[6:] == out[6]).all() and out[6] > 0
    assert out[0] != out[6]


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        rescue_bursts(_labels(-1, -1), ["A"], _ts(0, 1))
