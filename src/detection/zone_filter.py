from __future__ import annotations

import logging

import cv2
import numpy as np

from src.detection.object_detector import Detection

logger = logging.getLogger(__name__)


class ZoneFilter:
    """Filters detections to those whose center falls inside a named polygon zone.

    Zones are defined as lists of [x, y] pixel vertices in detection.yaml.
    """

    def __init__(self, zones: dict[str, list[list[int]]]) -> None:
        self._polygons: dict[str, np.ndarray] = {}
        for name, vertices in zones.items():
            pts = np.array(vertices, dtype=np.int32)
            self._polygons[name] = pts
            logger.debug("Zone '%s' loaded (%d vertices)", name, len(pts))

    def filter(self, detections: list[Detection], zone_name: str) -> list[Detection]:
        """Return only the detections whose center is inside `zone_name`."""
        poly = self._polygons.get(zone_name)
        if poly is None:
            logger.warning("Unknown zone '%s' — returning all detections", zone_name)
            return detections

        return [d for d in detections if self._inside(d.center, poly)]

    def draw_zones(self, frame: np.ndarray) -> np.ndarray:
        """Overlay zone polygons on a frame for debug/calibration views."""
        out = frame.copy()
        for name, poly in self._polygons.items():
            cv2.polylines(out, [poly], isClosed=True, color=(0, 255, 255), thickness=2)
            label_pt = tuple(poly[0])
            cv2.putText(out, name, label_pt, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        return out

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _inside(point: tuple[int, int], polygon: np.ndarray) -> bool:
        # cv2.pointPolygonTest returns >0 inside, 0 on edge, <0 outside
        return cv2.pointPolygonTest(polygon, (float(point[0]), float(point[1])), False) >= 0
