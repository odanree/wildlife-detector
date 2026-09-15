"""Locate + extract the animal crop from an alert snapshot.

## The problem this solves

`alerts.snapshot` points at the ANNOTATED whole frame written by
`Notifier._save_snapshot` — a 2px pure-red (BGR 0,0,255) rectangle plus
a red "species NN%" label drawn straight onto the pixels. Feeding that to
CLIP would make the red rectangle a dominant feature (every rat would
look like "a thing in a red box"). We need the animal region, clean.

For alerts written AFTER Phase 2a, `alerts.bbox` (JSONB) holds the exact
detector bbox + frame size, so this module just crops. For the ~29k
historical alerts, the description column only ever stored `WxH` (never
x,y — verified: 0 of 16,963 rows carry a position), so the bbox has to be
RECOVERED from the drawn rectangle itself.

## Recovery: red-outline detection

1. Strict chroma mask: R>150, G<90, B<90, R-max(G,B)>90. The rectangle
   is drawn as saturated pure red; JPEG q85 keeps the line core well
   inside this band. Night-IR frames (the bulk of rodent alerts) are
   grayscale so ANY chroma is annotation; daytime color frames (brick,
   rust) sit far below the saturation gate.
2. Connected components → keep the largest plus any component ≥5% of
   it. That unions the rectangle with the label text (which sits ~18px
   above the box and can be wider than a small box) while dropping
   isolated red speckle.
3. Bounding rect of the kept pixels = recovered bbox. Includes the label
   text band — accepted, because step 5 pads anyway and the text region
   is inpainted.
4. If the sibling `<name>.thumb.jpg` exists (solid red FILL, no text —
   written since 2026-09-12), prefer its bounding rect: exact box, no
   text confound.
5. Pad the bbox by `pad_frac * max(w,h)` (min `min_pad_px`) for context,
   clamp to frame, then INPAINT every reddish pixel in the crop (looser
   mask R-max(G,B)>35, dilated 5x5 to swallow the JPEG chroma halo) with
   Telea so neither the outline nor the label survives into CLIP.

Validated on 9 samples across 5 cameras and 3 frame sizes (1088x612,
2048x928, 2048x1152): recovered box matched the `.thumb.jpg` truth where
available to within the label-text band.

## Fallback

No red pixels (snapshot saved with bbox=None, or a non-detector image):
return the whole frame and label the source `full_frame` so Phase 2b can
weight or exclude those rows.

This module is pure numpy/cv2 — no torch — so it's unit-testable on a
bare runner and importable from the web service later (Phase 2c crop
thumbnails) without dragging CLIP along.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# Crop-source tags persisted to alert_embeddings.crop_source.
SOURCE_ALERT_BBOX = "alerts.bbox"      # exact detector bbox (post-Phase-2a rows)
SOURCE_RED_THUMB = "red_thumb"         # recovered from .thumb.jpg solid fill
SOURCE_RED_OUTLINE = "red_outline"     # recovered from the 2px outline + label
SOURCE_FULL_FRAME = "full_frame"       # nothing recoverable — whole frame

DEFAULT_PAD_FRAC = 0.5
DEFAULT_MIN_PAD_PX = 40
# Below this many strict-red pixels we don't trust the recovery — a 2px
# outline on the smallest bbox the detectors emit (22x22) is ~170px.
_MIN_RED_PIXELS = 40


@dataclass(frozen=True)
class BBox:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def w(self) -> int:
        return self.x2 - self.x1

    @property
    def h(self) -> int:
        return self.y2 - self.y1

    def clamp(self, frame_w: int, frame_h: int) -> "BBox":
        return BBox(
            max(0, min(self.x1, frame_w - 1)),
            max(0, min(self.y1, frame_h - 1)),
            max(1, min(self.x2, frame_w)),
            max(1, min(self.y2, frame_h)),
        )

    def padded(self, pad_frac: float, min_pad_px: int) -> "BBox":
        pad = max(int(pad_frac * max(self.w, self.h)), min_pad_px)
        return BBox(self.x1 - pad, self.y1 - pad, self.x2 + pad, self.y2 + pad)

    def to_record(self, frame_w: int, frame_h: int) -> dict:
        """JSON shape shared with alerts.bbox — see StateDB.append_alert."""
        d = asdict(self)
        d["frame_w"] = int(frame_w)
        d["frame_h"] = int(frame_h)
        return d

    @classmethod
    def from_record(cls, rec: dict) -> "BBox":
        return cls(int(rec["x1"]), int(rec["y1"]), int(rec["x2"]), int(rec["y2"]))


@dataclass(frozen=True)
class CropResult:
    image: np.ndarray            # BGR crop, red annotation removed
    bbox: BBox                   # the bbox used (tight, pre-padding, frame coords)
    crop_box: BBox               # the padded region actually returned
    source: str                  # one of the SOURCE_* tags
    frame_w: int
    frame_h: int


def strict_red_mask(img: np.ndarray) -> np.ndarray:
    """1 where the pixel is the drawn annotation's saturated red core.

    cv2.inRange on uint8 (SIMD, no temporaries) instead of an int16
    split-and-compare — the full-frame mask was ~40ms/alert on 2048x1152
    and dominated the non-inference cost on the first backfill run.
    R≥170 with G,B≤80 implies R-max(G,B) ≥ 90, so the saturation gate
    is preserved. Brick/rust (R~150, G~80) stays excluded."""
    return cv2.inRange(img, (0, 0, 170), (80, 80, 255)) // 255


def loose_red_mask(img: np.ndarray) -> np.ndarray:
    """1 where the pixel has any meaningful red chroma — used for inpaint
    so the JPEG halo around the 2px line is removed too. Only ever runs
    on the small padded crop, so the int16 math is cheap here."""
    b, g, r = cv2.split(img.astype(np.int16))
    return ((r - np.maximum(g, b)) > 35).astype(np.uint8)


def recover_red_bbox(img: np.ndarray) -> Optional[BBox]:
    """Bounding rect of the drawn red annotation, or None if there isn't
    enough red to trust."""
    m = strict_red_mask(img)
    if int(m.sum()) < _MIN_RED_PIXELS:
        return None
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = int(areas.max())
    keep_ids = [i + 1 for i, a in enumerate(areas) if a >= max(15, largest * 0.05)]
    sel = np.isin(labels, keep_ids)
    ys, xs = np.where(sel)
    return BBox(int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def thumb_path_for(snapshot_path: Path) -> Path:
    """`2026-09-12/rodent_X.jpg` → `2026-09-12/rodent_X.thumb.jpg`."""
    return snapshot_path.with_name(snapshot_path.stem + ".thumb.jpg")


def _inpaint_red(crop: np.ndarray) -> np.ndarray:
    mask = cv2.dilate(loose_red_mask(crop), np.ones((5, 5), np.uint8))
    if int(mask.sum()) == 0:
        return crop
    return cv2.inpaint(crop, mask, 3, cv2.INPAINT_TELEA)


def extract_crop(
    snapshot_path: Path,
    known_bbox: Optional[dict] = None,
    pad_frac: float = DEFAULT_PAD_FRAC,
    min_pad_px: int = DEFAULT_MIN_PAD_PX,
) -> CropResult:
    """Load the snapshot, find the animal, return a clean padded crop.

    `known_bbox` is the `alerts.bbox` JSONB record when present (exact
    detector coordinates, in the snapshot frame's pixel space). When
    None we fall back to red-outline recovery.

    Raises FileNotFoundError if the snapshot is missing — callers decide
    whether that's skip-and-log (embedder) or fatal (tests).
    """
    img = cv2.imread(str(snapshot_path))
    if img is None:
        raise FileNotFoundError(snapshot_path)
    fh, fw = img.shape[:2]

    bbox: Optional[BBox] = None
    source = SOURCE_FULL_FRAME

    if known_bbox:
        raw = BBox.from_record(known_bbox)
        source = SOURCE_ALERT_BBOX
        # Defensive: a bbox recorded against a different frame size than
        # the JPEG on disk (INPUT_WIDTH changed between write and read)
        # would crop the wrong region. Rescale BEFORE clamping — clamping
        # first would truncate any coordinate past the JPEG's edge.
        rw, rh = int(known_bbox.get("frame_w", fw)), int(known_bbox.get("frame_h", fh))
        if (rw, rh) != (fw, fh) and rw > 0 and rh > 0:
            sx, sy = fw / rw, fh / rh
            raw = BBox(int(raw.x1 * sx), int(raw.y1 * sy), int(raw.x2 * sx), int(raw.y2 * sy))
        bbox = raw.clamp(fw, fh)

    if bbox is None:
        thumb = thumb_path_for(snapshot_path)
        if thumb.exists():
            timg = cv2.imread(str(thumb))
            if timg is not None and timg.shape[:2] == (fh, fw):
                bbox = recover_red_bbox(timg)
                if bbox is not None:
                    source = SOURCE_RED_THUMB

    if bbox is None:
        bbox = recover_red_bbox(img)
        if bbox is not None:
            source = SOURCE_RED_OUTLINE

    if bbox is None:
        bbox = BBox(0, 0, fw, fh)
        return CropResult(image=img, bbox=bbox, crop_box=bbox, source=SOURCE_FULL_FRAME,
                          frame_w=fw, frame_h=fh)

    crop_box = bbox.padded(pad_frac, min_pad_px).clamp(fw, fh)
    crop = img[crop_box.y1:crop_box.y2, crop_box.x1:crop_box.x2].copy()
    crop = _inpaint_red(crop)
    return CropResult(image=crop, bbox=bbox, crop_box=crop_box, source=source,
                      frame_w=fw, frame_h=fh)
