"""Unit tests for scripts.roi_night_replay.cross_reference.

Pure Python, no DB, no detector — the cross-reference is set-difference
reconciliation between batch verdicts and prod alerts. Wrong bucketing
would either surface false 'live missed' events (wasted operator time)
or hide real misses (defeats the whole tool), so it's worth pinning.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Reach the script under tests/. Same trick main() uses.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.roi_night_replay import cross_reference, DeltaEvent  # noqa: E402


def _v(bbox, ts_offset=0.0, species="rat", conf=0.8):
    """Build a batch verdict dict matching what the pipeline sidecar emits."""
    return {"bbox": bbox, "ts_offset_s": ts_offset, "species": species, "confidence": conf, "description": ""}


def _alert(alert_id, ts, bbox=None, species="rat", frame_w=2048, frame_h=1152):
    """Build a live alert row matching load_live_alerts's return shape."""
    row = {"id": alert_id, "ts": ts, "species": species, "confidence": 0.8, "label_species": None, "crop_bbox": None}
    if bbox is not None:
        row["crop_bbox"] = {"x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3],
                            "frame_w": frame_w, "frame_h": frame_h}
    return row


# ── time-window matching ────────────────────────────────────────────────


def test_match_within_time_and_bbox():
    """Same event: batch at t=100, live at t=102 (2s apart, well within 8s),
    same bbox in normalized space. Should bucket as matched, no delta."""
    # Detector frame default is 2048x1152. Batch bbox (100,100,200,200) normalized
    # → (0.049, 0.087, 0.098, 0.174). Live at same fraction on a 4096x2160
    # source frame → same normalized coords.
    batch = [(100.0, _v([100, 100, 200, 200]), "chunk.mp4", 0.0)]
    live = [_alert(1, 102.0, bbox=[200, 187, 401, 375], frame_w=4096, frame_h=2160)]
    matched, delta, live_only = cross_reference("cam", batch, live, time_window_s=8.0, iou_threshold=0.3,
                                                  det_w=2048, det_h=1152)
    assert len(matched) == 1
    assert len(delta) == 0
    assert len(live_only) == 0
    assert matched[0]["live_alert_id"] == 1
    assert matched[0]["iou"] >= 0.3


def test_no_match_across_time_gap():
    """Batch at t=100, live at t=130 (30s apart, > 8s window). Even with
    identical bbox, the time gap forbids a match — both survive to their
    respective sides."""
    batch = [(100.0, _v([100, 100, 200, 200]), "chunk.mp4", 0.0)]
    live = [_alert(1, 130.0, bbox=[200, 187, 401, 375], frame_w=4096, frame_h=2160)]
    matched, delta, live_only = cross_reference("cam", batch, live, time_window_s=8.0, iou_threshold=0.3,
                                                  det_w=2048, det_h=1152)
    assert len(matched) == 0
    assert len(delta) == 1
    assert len(live_only) == 1


def test_no_match_across_bbox_gap():
    """Batch and live at same ts but on OPPOSITE corners of the frame — IoU=0
    even though time matches. Prevents 'any two motions on this camera near
    each other in time are the same event' false-collapses."""
    batch = [(100.0, _v([100, 100, 200, 200]), "chunk.mp4", 0.0)]
    live = [_alert(1, 100.5, bbox=[3500, 1900, 3800, 2100], frame_w=4096, frame_h=2160)]
    matched, delta, live_only = cross_reference("cam", batch, live, time_window_s=8.0, iou_threshold=0.3,
                                                  det_w=2048, det_h=1152)
    assert len(matched) == 0
    assert len(delta) == 1
    assert len(live_only) == 1


# ── the delta: what live missed ─────────────────────────────────────────


def test_batch_only_delta_is_the_output_of_the_tool():
    """The whole point of the tool: batch sees a rat, live saw nothing.
    Report the rat as batch_only. Two-verdict, one-live-alert scenario:
    the matched one gets bucketed, the missed one becomes the delta."""
    batch = [
        (100.0, _v([100, 100, 200, 200], species="rat"), "chunk.mp4", 0.0),
        (300.0, _v([500, 300, 600, 400], species="rat"), "chunk.mp4", 200.0),
    ]
    live = [_alert(1, 101.0, bbox=[200, 187, 401, 375], frame_w=4096, frame_h=2160)]
    matched, delta, live_only = cross_reference("cam", batch, live, time_window_s=8.0, iou_threshold=0.3,
                                                  det_w=2048, det_h=1152)
    assert len(matched) == 1
    assert len(delta) == 1
    assert isinstance(delta[0], DeltaEvent)
    assert delta[0].species == "rat"
    assert delta[0].batch_ts_epoch == 300.0
    assert delta[0].clip_source == "chunk.mp4"
    assert delta[0].clip_offset_s == 200.0


def test_insect_and_none_verdicts_are_NOT_delta():
    """Batch verdicts of species='insect' / 'none' aren't real detections —
    they're the pre-filter's non-rodent bucketing. They should never appear
    as 'events live missed' because live would have skipped them too."""
    batch = [
        (100.0, _v([100, 100, 200, 200], species="insect"), "chunk.mp4", 0.0),
        (200.0, _v([100, 100, 200, 200], species="none"), "chunk.mp4", 100.0),
        (300.0, _v([100, 100, 200, 200], species=""), "chunk.mp4", 200.0),
        (400.0, _v([100, 100, 200, 200], species="rat"), "chunk.mp4", 300.0),  # this one IS a miss
    ]
    live: list[dict] = []
    matched, delta, live_only = cross_reference("cam", batch, live, time_window_s=8.0, iou_threshold=0.3,
                                                  det_w=2048, det_h=1152)
    assert len(matched) == 0
    assert len(delta) == 1
    assert delta[0].species == "rat"


def test_time_only_match_when_bbox_missing():
    """Older alerts don't have crop_bbox populated. Falling back to time-only
    matching prevents spurious 'live missed X' when live actually caught X
    but just didn't record its bbox."""
    batch = [(100.0, _v([100, 100, 200, 200]), "chunk.mp4", 0.0)]
    live = [_alert(1, 102.0, bbox=None)]  # no crop_bbox
    matched, delta, live_only = cross_reference("cam", batch, live, time_window_s=8.0, iou_threshold=0.3,
                                                  det_w=2048, det_h=1152)
    assert len(matched) == 1  # time-only match wins
    assert len(delta) == 0


# ── greedy assignment ───────────────────────────────────────────────────


def test_each_live_alert_matches_at_most_one_batch_verdict():
    """If two batch verdicts fire near the same live alert, only ONE gets
    matched (the first with highest IoU). The other becomes delta — a
    genuine second detection the greedy pass keeps separate."""
    # Two verdicts at t=100 and t=101, both overlapping the same live bbox.
    # Only one live alert exists, so one match + one delta expected.
    batch = [
        (100.0, _v([100, 100, 200, 200]), "chunk.mp4", 0.0),
        (101.0, _v([105, 105, 205, 205]), "chunk.mp4", 1.0),
    ]
    live = [_alert(1, 100.5, bbox=[200, 187, 401, 375], frame_w=4096, frame_h=2160)]
    matched, delta, live_only = cross_reference("cam", batch, live, time_window_s=8.0, iou_threshold=0.3,
                                                  det_w=2048, det_h=1152)
    assert len(matched) == 1
    assert len(delta) == 1


def test_multiple_matches_multiple_lives():
    """Two batch verdicts, two live alerts, each pair matches → 0 delta.
    Confirms the greedy pass doesn't collapse independent events."""
    batch = [
        (100.0, _v([100, 100, 200, 200]), "chunk.mp4", 0.0),
        (500.0, _v([1500, 800, 1600, 900]), "chunk.mp4", 400.0),
    ]
    live = [
        _alert(1, 101.0, bbox=[200, 187, 401, 375], frame_w=4096, frame_h=2160),
        _alert(2, 501.0, bbox=[3000, 1500, 3200, 1687], frame_w=4096, frame_h=2160),
    ]
    matched, delta, live_only = cross_reference("cam", batch, live, time_window_s=8.0, iou_threshold=0.3,
                                                  det_w=2048, det_h=1152)
    assert len(matched) == 2
    assert len(delta) == 0
    assert len(live_only) == 0


# ── live_only bucket ────────────────────────────────────────────────────


def test_live_only_when_batch_missed_something_live_caught():
    """Uncommon but possible: batch stricter/tighter zone than live, so it
    missed an event live caught. Report as live_only for completeness."""
    batch: list = []
    live = [_alert(1, 100.0, bbox=[200, 187, 401, 375], frame_w=4096, frame_h=2160)]
    matched, delta, live_only = cross_reference("cam", batch, live, time_window_s=8.0, iou_threshold=0.3,
                                                  det_w=2048, det_h=1152)
    assert len(matched) == 0
    assert len(delta) == 0
    assert len(live_only) == 1
    assert live_only[0]["alert_id"] == 1
