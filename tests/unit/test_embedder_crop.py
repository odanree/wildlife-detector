"""Unit tests for src/embedder/crop.py — red-outline bbox recovery.

Synthesizes snapshots the same way Notifier._save_snapshot does (2px
BGR(0,0,255) rectangle + red label text, JPEG q85) so the tests exercise
the real JPEG-chroma behaviour rather than a lossless PNG idealisation.
No torch, no DB — runs on a bare CI runner.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from src.embedder.crop import (
    SOURCE_ALERT_BBOX,
    SOURCE_FULL_FRAME,
    SOURCE_RED_OUTLINE,
    SOURCE_RED_THUMB,
    BBox,
    extract_crop,
    recover_red_bbox,
    strict_red_mask,
    thumb_path_for,
)

FW, FH = 640, 360
TRUE = (300, 150, 348, 190)  # x1, y1, x2, y2 — a 48x40 "rat"


def _ir_frame(seed: int = 0) -> np.ndarray:
    """Grayscale-ish IR night frame with texture (so inpaint has something
    to work with) and a dark blob where the animal is."""
    rng = np.random.default_rng(seed)
    gray = rng.integers(60, 140, size=(FH, FW), dtype=np.uint8)
    gray = cv2.GaussianBlur(gray, (7, 7), 0)
    x1, y1, x2, y2 = TRUE
    cv2.ellipse(gray, ((x1 + x2) // 2, (y1 + y2) // 2), (20, 14), 0, 0, 360, 30, -1)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _annotate_like_notifier(frame: np.ndarray, bbox, label="mouse 85%") -> np.ndarray:
    out = frame.copy()
    x1, y1, x2, y2 = bbox
    color = (0, 0, 255)
    cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
    cv2.putText(out, label, (x1, max(y1 - 6, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
    return out


def _write_jpg(path: Path, img: np.ndarray, q: int = 85) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, q])


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    p = tmp_path / "2026-09-15" / "rodent_20260915_120000.jpg"
    _write_jpg(p, _annotate_like_notifier(_ir_frame(), TRUE))
    return p


def test_recover_red_bbox_matches_drawn_rectangle(snapshot: Path):
    img = cv2.imread(str(snapshot))
    bb = recover_red_bbox(img)
    assert bb is not None
    x1, y1, x2, y2 = TRUE
    # Left / bottom edges: rectangle line is 2px thick and drawn centred
    # on the coordinate, so ±3 covers line width + JPEG ringing.
    assert abs(bb.x1 - x1) <= 3
    assert abs(bb.y2 - y2) <= 3
    # Label text ("mouse 85%" @0.6 ≈ 105px) sits above the box and
    # overhangs x2 on boxes narrower than the text — the recovered rect
    # is a superset on the top/right. Same behaviour observed on real
    # snapshots vs their .thumb.jpg truth. Bound the overhang so a
    # regression that unions unrelated red speckle still fails.
    assert x2 - 3 <= bb.x2 <= x2 + 120
    assert bb.y1 < y1
    assert y1 - bb.y1 < 30


def test_extract_crop_red_outline_source_and_no_red_left(snapshot: Path):
    res = extract_crop(snapshot)
    assert res.source == SOURCE_RED_OUTLINE
    assert (res.frame_w, res.frame_h) == (FW, FH)
    # The padded crop must contain the true animal box entirely.
    assert res.crop_box.x1 <= TRUE[0] and res.crop_box.y1 <= TRUE[1]
    assert res.crop_box.x2 >= TRUE[2] and res.crop_box.y2 >= TRUE[3]
    # And the annotation must be gone — this is the whole point.
    assert int(strict_red_mask(res.image).sum()) == 0


def test_thumb_sibling_preferred_when_present(snapshot: Path):
    # Solid-fill thumb variant, exactly as Notifier writes it.
    thumb = _ir_frame().copy()
    x1, y1, x2, y2 = TRUE
    cv2.rectangle(thumb, (x1, y1), (x2, y2), (0, 0, 255), -1)
    _write_jpg(thumb_path_for(snapshot), thumb, q=75)

    res = extract_crop(snapshot)
    assert res.source == SOURCE_RED_THUMB
    # Solid fill has no label text → tight on all four sides.
    assert abs(res.bbox.x1 - x1) <= 2 and abs(res.bbox.y1 - y1) <= 2
    assert abs(res.bbox.x2 - x2) <= 2 and abs(res.bbox.y2 - y2) <= 2
    assert int(strict_red_mask(res.image).sum()) == 0


def test_known_bbox_short_circuits_recovery(snapshot: Path):
    rec = BBox(*TRUE).to_record(FW, FH)
    res = extract_crop(snapshot, known_bbox=rec)
    assert res.source == SOURCE_ALERT_BBOX
    assert res.bbox == BBox(*TRUE)


def test_known_bbox_rescaled_when_frame_size_differs(snapshot: Path):
    # bbox recorded against a 2x frame → must be halved to match the JPEG.
    rec = BBox(600, 300, 696, 380).to_record(FW * 2, FH * 2)
    res = extract_crop(snapshot, known_bbox=rec)
    assert res.bbox == BBox(300, 150, 348, 190)


def test_no_red_falls_back_to_full_frame(tmp_path: Path):
    p = tmp_path / "plain.jpg"
    _write_jpg(p, _ir_frame())
    res = extract_crop(p)
    assert res.source == SOURCE_FULL_FRAME
    assert res.image.shape[:2] == (FH, FW)
    assert res.bbox == BBox(0, 0, FW, FH)


def test_daytime_brick_red_does_not_trigger_recovery(tmp_path: Path):
    # Brick / rust tones are reddish but far from saturated (0,0,255).
    img = np.full((FH, FW, 3), (60, 80, 150), dtype=np.uint8)  # BGR brick
    p = tmp_path / "brick.jpg"
    _write_jpg(p, img)
    assert recover_red_bbox(cv2.imread(str(p))) is None


def test_missing_snapshot_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        extract_crop(tmp_path / "nope.jpg")


def test_bbox_record_roundtrip():
    bb = BBox(10, 20, 30, 40)
    rec = bb.to_record(2048, 928)
    assert rec == {"x1": 10, "y1": 20, "x2": 30, "y2": 40, "frame_w": 2048, "frame_h": 928}
    assert BBox.from_record(rec) == bb


def test_bbox_padded_and_clamped():
    bb = BBox(5, 5, 25, 25)  # 20x20 → pad = max(10, 40) = 40
    padded = bb.padded(0.5, 40).clamp(100, 100)
    assert padded == BBox(0, 0, 65, 65)
