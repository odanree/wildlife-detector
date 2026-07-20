from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import cv2
import numpy as np
from ultralytics import YOLO

logger = logging.getLogger(__name__)

# COCO has no rat/mouse; we track larger wildlife (cat, dog, bird) to route
# around obvious non-rodent movers and let motion + VLM classify the small
# stuff. Position-suppression is disabled by default — small movers deserve
# to keep re-firing so we never miss a rodent that returns to the same spot.
_TRACKED_CLASSES: set[str] = {"cat", "dog", "bird"}
_STATIONARY_EXEMPT: set[str] = set()
_POSITION_SUPPRESS_CLASSES: set[str] = set()


@dataclass
class Detection:
    track_id: int
    class_name: str
    confidence: float
    bbox: tuple[int, int, int, int]   # x1, y1, x2, y2
    center: tuple[int, int] = field(init=False)
    area_fraction: float = 0.0
    # Raw MOG2/KNN contour (Nx1x2 int32 in frame pixel space). Present for
    # motion-detector detections; None for YOLO detections. Used by the VLM
    # crop path to overlay a motion outline when the target is camouflaged
    # and only visible via the motion signature.
    contour: object = None

    def __post_init__(self) -> None:
        x1, y1, x2, y2 = self.bbox
        self.center = ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]


class ObjectDetector:
    """YOLOv8 tracker wrapper with built-in stationary-mask suppression.

    Stationary masking: if a tracked object hasn't moved more than
    `stationary_px` pixels in `stationary_frames` consecutive frames it is
    excluded from the returned detections so a parked car never re-fires.
    """

    def __init__(
        self,
        model_path: str = "yolov8n.pt",
        threshold: float = 0.65,
        input_size: tuple[int, int] = (1280, 720),
        min_area_fraction: float = 0.02,
        max_area_fraction: float = 0.50,
        stationary_px: int = 15,
        stationary_frames: int = 30,
        tracker_config: str = "config/bytetrack.yaml",
        fp_grid_cell_px: int = 48,
        fp_suppress_seconds: float = 10.0,
        yolo_imgsz: int = 640,   # YOLO's internal inference size — drop to 480 for ~40% CPU speedup at some recall cost
    ) -> None:
        self._model = YOLO(model_path)
        self._model_path = model_path
        # Separate, lazily-loaded model for stateless single-frame localization
        # (see localize_person). Kept distinct from self._model so a one-off
        # predict() never disturbs the persistent ByteTrack/BoT-SORT state that
        # self._model.track(persist=True) maintains across frames.
        self._loc_model: YOLO | None = None
        self._threshold = threshold
        # ByteTrack low threshold: fed to the tracker so it can re-associate
        # partly-occluded tracks without creating new false-positive tracks.
        self._track_low_thresh = max(0.05, threshold * 0.40)
        self._tracker_config = tracker_config
        self._input_size = input_size
        self._min_area = min_area_fraction
        self._max_area = max_area_fraction
        self._stationary_px = stationary_px
        self._stationary_frames = stationary_frames
        self._yolo_imgsz = yolo_imgsz

        # track_id -> deque of recent centers
        self._position_history: dict[int, list[tuple[int, int]]] = {}

        frame_px = input_size[0] * input_size[1]
        self._min_px = min_area_fraction * frame_px
        self._max_px = max_area_fraction * frame_px

        # Position-grid false-positive suppressor.
        # Divides the frame into cells of fp_grid_cell_px × fp_grid_cell_px.
        # If a _POSITION_SUPPRESS_CLASSES detection occupies the same cell
        # continuously for fp_suppress_seconds (time-based, FPS-independent),
        # the cell is permanently blacklisted — fire hydrants / trash cans silenced.
        self._grid_cell_px = fp_grid_cell_px
        self._fp_suppress_seconds = fp_suppress_seconds
        self._grid_first_seen: dict[tuple[int, int], float] = {}  # cell -> monotonic start time
        self._grid_blocked: set[tuple[int, int]] = set()          # permanently suppressed cells

        logger.info(
            "Detector ready — model=%s high=%.2f low=%.2f tracker=%s fp_grid=%dpx suppress_after=%.1fs",
            model_path, threshold, self._track_low_thresh, tracker_config,
            fp_grid_cell_px, fp_suppress_seconds,
        )

    def detect(self, frame: np.ndarray) -> list[Detection]:
        h, w = frame.shape[:2]
        resized = cv2.resize(frame, self._input_size) if (w, h) != self._input_size else frame

        results = self._model.track(
            resized,
            persist=True,
            conf=self._track_low_thresh,
            tracker=self._tracker_config,
            classes=self._class_ids(),
            verbose=False,
            imgsz=self._yolo_imgsz,
        )

        detections: list[Detection] = []
        frame_area = self._input_size[0] * self._input_size[1]

        if results[0].boxes.id is None:
            self._update_grid(set())   # no hits → reset all active streaks
            return detections

        hits_this_frame: set[tuple[int, int]] = set()

        for box, track_id, conf, cls in zip(
            results[0].boxes.xyxy.cpu().numpy(),
            results[0].boxes.id.cpu().numpy().astype(int),
            results[0].boxes.conf.cpu().numpy(),
            results[0].boxes.cls.cpu().numpy().astype(int),
        ):
            class_name = self._model.names[cls]
            if class_name not in _TRACKED_CLASSES:
                continue

            x1, y1, x2, y2 = map(int, box)
            area = (x2 - x1) * (y2 - y1)
            area_frac = area / frame_area

            if not (self._min_px <= area <= self._max_px):
                continue

            # Post-track confidence gate: ByteTrack runs at _track_low_thresh so
            # it can re-associate partly-occluded objects, but downstream stages
            # should only see detections that clear the user-configured threshold.
            # Without this, ~70 % of stage-1 VLM calls were spent rejecting sub-0.30
            # YOLO hits on shadows.
            if float(conf) < self._threshold:
                continue

            det = Detection(
                track_id=int(track_id),
                class_name=class_name,
                confidence=float(conf),
                bbox=(x1, y1, x2, y2),
                area_fraction=area_frac,
            )

            if class_name not in _STATIONARY_EXEMPT and self._is_stationary(det.track_id, det.center):
                continue

            # Position-grid FP suppression: skip permanently blocked cells.
            if class_name in _POSITION_SUPPRESS_CLASSES:
                cell = self._grid_cell(det.center)
                if cell in self._grid_blocked:
                    continue
                hits_this_frame.add(cell)

            detections.append(det)

        self._update_grid(hits_this_frame)
        return detections

    def localize_person(
        self,
        frame: np.ndarray,
        search_bbox: tuple[int, int, int, int] | None = None,
        search_pad: float = 2.0,
        conf: float | None = None,
        imgsz: int = 1280,
    ) -> tuple[int, int, int, int] | None:
        """Stateless single-frame person localization (no tracking/suppression).

        Runs YOLO once and returns a person bbox **in frame-pixel coordinates**,
        or None if no person clears ``conf``.

        When ``search_bbox`` (a coarse seed, in ``frame`` coords) is given, the
        search is restricted to an ROI of that box padded by ``search_pad`` ×
        its size, and predict runs on the native-resolution crop. This is the
        path that matters for hi-res frames: a distant person is only ~tens of
        px tall in a 4K frame, so a full-frame predict at ``imgsz`` downscales
        them away — cropping first keeps them large enough to detect. The
        returned bbox is offset back to full-frame coords, and the candidate
        nearest the seed center is chosen.

        Purpose: a hi-res snapshot is fetched a few hundred ms after the RTSP
        detection frame, so the RTSP bbox (merely scaled up) lags behind a
        moving person. Re-detecting on the snapshot yields a box at the
        snapshot's own instant, so the drawn box and the VLM crop align to the
        pixels actually being analysed.

        Uses a separate model instance (lazily loaded) so the persistent
        tracker state on ``self._model`` is never perturbed.
        """
        if self._loc_model is None:
            self._loc_model = YOLO(self._model_path)
            logger.info("Localizer model loaded (%s) for hi-res re-detection", self._model_path)

        h, w = frame.shape[:2]
        ox, oy = 0, 0
        seed_center: tuple[int, int] | None = None
        roi = frame
        if search_bbox is not None:
            sx1, sy1, sx2, sy2 = search_bbox
            seed_center = ((sx1 + sx2) // 2, (sy1 + sy2) // 2)
            pad_x = int((sx2 - sx1) * search_pad)
            pad_y = int((sy2 - sy1) * search_pad)
            ox, oy = max(0, sx1 - pad_x), max(0, sy1 - pad_y)
            roi = frame[oy : min(h, sy2 + pad_y), ox : min(w, sx2 + pad_x)]
            if roi.size == 0:
                roi, ox, oy = frame, 0, 0

        person_id = next(
            (i for i, n in self._loc_model.names.items() if n == "person"), 0
        )
        results = self._loc_model.predict(
            roi,
            conf=conf if conf is not None else self._threshold,
            classes=[person_id],
            imgsz=imgsz,
            verbose=False,
        )
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return None

        candidates = [
            (int(b[0]) + ox, int(b[1]) + oy, int(b[2]) + ox, int(b[3]) + oy)
            for b in boxes.xyxy.cpu().numpy()
        ]
        if seed_center is None:
            confs = boxes.conf.cpu().numpy()
            return candidates[int(np.argmax(confs))]

        cx, cy = seed_center
        return min(
            candidates,
            key=lambda b: ((b[0] + b[2]) / 2 - cx) ** 2 + ((b[1] + b[3]) / 2 - cy) ** 2,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _class_ids(self) -> list[int]:
        return [
            idx
            for idx, name in self._model.names.items()
            if name in _TRACKED_CLASSES
        ]

    def _grid_cell(self, center: tuple[int, int]) -> tuple[int, int]:
        return (center[0] // self._grid_cell_px, center[1] // self._grid_cell_px)

    def _update_grid(self, hits: set[tuple[int, int]]) -> None:
        now = time.monotonic()
        # Advance clocks for active cells; block those that exceed the threshold.
        for cell in hits:
            if cell not in self._grid_first_seen:
                self._grid_first_seen[cell] = now
            elif now - self._grid_first_seen[cell] >= self._fp_suppress_seconds:
                if cell not in self._grid_blocked:
                    logger.info(
                        "FP grid cell (%d,%d) blocked after %.1fs — likely static object",
                        cell[0], cell[1], now - self._grid_first_seen[cell],
                    )
                self._grid_blocked.add(cell)
        # Reset clock for cells that had no hit this frame (object moved away).
        for cell in list(self._grid_first_seen):
            if cell not in hits and cell not in self._grid_blocked:
                del self._grid_first_seen[cell]

    def clear_fp_grid(self) -> None:
        """Reset all learned FP suppressions (e.g. after camera repositioning)."""
        self._grid_first_seen.clear()
        self._grid_blocked.clear()
        logger.info("FP position grid cleared")

    def _is_stationary(self, track_id: int, center: tuple[int, int]) -> bool:
        history = self._position_history.setdefault(track_id, [])
        history.append(center)
        if len(history) > self._stationary_frames:
            history.pop(0)

        if len(history) < self._stationary_frames:
            return False

        xs = [p[0] for p in history]
        ys = [p[1] for p in history]
        spread = max(max(xs) - min(xs), max(ys) - min(ys))
        return spread <= self._stationary_px
