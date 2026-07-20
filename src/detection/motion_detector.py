"""Background-subtraction person detector.

Angle-agnostic alternative to YOLO for top-down fisheye cameras where people
look like small overhead blobs that defeat YOLO's person classifier.

Uses MOG2 background subtraction to find moving foreground regions, then
filters by area and aspect ratio to keep human-sized blobs.  Assigns
persistent track IDs via nearest-centroid matching so the ChalkingAnalyzer
can build a height-decrease history across frames.

Track IDs start at 1000 to avoid colliding with YOLO ByteTrack IDs.
"""

from __future__ import annotations

import logging
import os

import cv2
import numpy as np

from src.detection.object_detector import Detection

logger = logging.getLogger(__name__)

_MATCH_DIST_PX = 60     # max centroid displacement between frames for same track
_SYNTHETIC_CONF = 0.50  # confidence value assigned to motion-detected persons


class MotionDetector:
    def __init__(
        self,
        history: int = 500,
        var_threshold: float = 50.0,
        min_area: int = 800,
        max_area: int = 18000,
        edge_margin: int = 80,
        seam_x: int = 0,
        seam_margin: int = 0,
    ) -> None:
        # Env override lets us swap MOG2 → KNN per-camera without a config-schema
        # rewrite. KNN handles textured/foliage backgrounds better because it
        # stores raw samples instead of averaging variance — a slow-moving
        # target through wind-blown brush gets absorbed by MOG2's variance model
        # but stays foreground under KNN.
        _use_knn = os.getenv("MOTION_BACKEND", "mog2").lower() == "knn"
        if _use_knn:
            self._bg = cv2.createBackgroundSubtractorKNN(
                history=history,
                dist2Threshold=float(os.getenv("KNN_DIST2_THRESHOLD", "400")),
                detectShadows=False,
            )
        else:
            self._bg = cv2.createBackgroundSubtractorMOG2(
                history=history,
                varThreshold=var_threshold,
                detectShadows=False,
            )
        self._backend = "KNN" if _use_knn else "MOG2"
        self._min_area = min_area
        self._max_area = max_area
        self._edge_margin = edge_margin
        self._seam_x = seam_x
        self._seam_margin = seam_margin
        self._tracks: dict[int, tuple[int, int]] = {}   # id → last centroid
        self._next_id = 1000
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        logger.info(
            "Motion detector ready — %s history=%d threshold=%.0f area=%d–%d px² edge_margin=%d",
            self._backend, history, var_threshold, min_area, max_area, edge_margin,
        )

    def detect(self, frame: np.ndarray) -> list[Detection]:
        # Black out the dual-camera stitch seam before background subtraction
        # so MOG2 never sees the exposure/timing artifact as motion.
        if self._seam_x and self._seam_margin:
            frame = frame.copy()
            x1 = max(0, self._seam_x - self._seam_margin)
            x2 = min(frame.shape[1], self._seam_x + self._seam_margin)
            frame[:, x1:x2] = 0

        fg = self._bg.apply(frame)

        # Open removes isolated noise pixels; dilate merges nearby fragments
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self._kernel)
        fg = cv2.dilate(fg, self._kernel, iterations=2)

        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        fh, fw = frame.shape[:2]
        frame_area = fh * fw
        detections: list[Detection] = []
        matched: set[int] = set()

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if not (self._min_area <= area <= self._max_area):
                continue

            x, y, w, h = cv2.boundingRect(cnt)
            cx, cy = x + w // 2, y + h // 2

            # Reject blobs near the fisheye edge — distortion there creates noise
            m = self._edge_margin
            if cx < m or cx > fw - m or cy < m or cy > fh - m:
                logger.debug("blob rejected edge cx=%d cy=%d", cx, cy)
                continue

            aspect = h / w if w > 0 else 0

            # People from above are roughly square to slightly tall.
            # Wide flat blobs are fence reflections/car fragments; very tall thin blobs are poles/trees.
            if not (0.65 <= aspect <= 2.2):
                logger.debug("blob rejected aspect=%.2f area=%.0f cx=%d cy=%d", aspect, area, cx, cy)
                continue

            # Solidity: contour area / convex hull area.
            # Wispy tree/shadow blobs score <0.30; fisheye-distorted people ~0.35+.
            hull_area = cv2.contourArea(cv2.convexHull(cnt))
            solidity = area / hull_area if hull_area > 0 else 0
            if solidity < 0.30:
                logger.debug("blob rejected solidity=%.2f area=%.0f cx=%d cy=%d", solidity, area, cx, cy)
                continue

            logger.debug("blob accepted area=%.0f aspect=%.2f solidity=%.2f cx=%d cy=%d", area, aspect, solidity, cx, cy)

            tid = self._match_or_create(cx, cy, matched)
            matched.add(tid)

            detections.append(Detection(
                track_id=tid,
                class_name="person",
                confidence=_SYNTHETIC_CONF,
                bbox=(x, y, x + w, y + h),
                area_fraction=area / frame_area,
                contour=cnt,
            ))

        # Evict tracks that disappeared this frame
        for tid in [t for t in self._tracks if t not in matched]:
            del self._tracks[tid]

        return detections

    def _match_or_create(self, cx: int, cy: int, already_matched: set[int]) -> int:
        best_id, best_dist = None, float("inf")
        for tid, (tx, ty) in self._tracks.items():
            if tid in already_matched:
                continue
            d = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
            if d < best_dist:
                best_dist, best_id = d, tid

        if best_id is not None and best_dist < _MATCH_DIST_PX:
            self._tracks[best_id] = (cx, cy)
            return best_id

        new_id = self._next_id
        self._next_id += 1
        self._tracks[new_id] = (cx, cy)
        return new_id
