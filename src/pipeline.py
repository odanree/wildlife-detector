"""Wildlife detection pipeline — lean headless loop.

Flow:
  RTSP / video file
        │
        ▼
  YOLO tracker (COCO cat/dog/bird, advisory only)
        │  +
  MOG2 motion supplement (the load-bearing signal — COCO has no rat/mouse)
        │
        ▼
  Zone filter (yard polygon)
        │
        ▼
  Rate-limited VLM classify ("is this a rodent? what species?")
        │
        ├── if rodent → Notifier (snapshot + HA + generic webhook)
        │               + Slew secondary PTZ camera to zone preset
        │
        └── else → nothing (no cooldown state, no RAG, no dedup)

Blocks until KeyboardInterrupt.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import NamedTuple

import cv2
import numpy as np
import yaml
from dotenv import load_dotenv

from src.config_bootstrap import ensure_detection_config

# Copy detection.yaml.example → detection.yaml on first startup. The
# file is gitignored (runtime-mutable via zone editor + slew preset
# editor); ships as .example. No-op after first boot on any host.
ensure_detection_config()

load_dotenv()

from src.alerts.notifier import Notifier
from src.detection.motion_detector import MotionDetector
from src.detection.object_detector import Detection, ObjectDetector
from src.detection.zone_filter import ZoneFilter
from src.stream.rtsp_handler import RTSPHandler
from src.stream.slew import maybe_slew
from src.stream.self_slew import (
    is_in_transition as _self_slew_in_transition,
    maybe_self_slew,
)
from src.stream.video_file_handler import VideoFileHandler
from src.vlm.analyzer import VLMAnalyzer

# Preview server hooks — no-ops when preview isn't started (Flask missing).
try:
    from src.web.preview import (
        publish_frame as _publish_preview_frame,
        publish_raw_frame as _publish_raw_frame,
        stats as _preview_stats,
        get_zones as _preview_get_zones,
        init_zones as _preview_init_zones,
        init_alert_log as _preview_init_alert_log,
        init_baseline as _preview_init_baseline,
        get_baseline as _preview_get_baseline,
        init_masks as _preview_init_masks,
    )
except ImportError:
    def _publish_preview_frame(_jpeg: bytes) -> None:
        pass
    def _publish_raw_frame(_jpeg: bytes) -> None:
        pass
    class _NoStats:
        def record_frame(self): pass
        def record_alert(self, *a, **kw): pass
        def set_backend(self, _b): pass
        def set_camera(self, _c): pass
        def set_detection_size(self, _w, _h): pass
    _preview_stats = _NoStats()
    def _preview_get_zones(): return None
    def _preview_init_zones(_p, _k, det_w=None, det_h=None): return None
    def _preview_init_alert_log(_d, capacity: int = 500): return None
    def _preview_init_baseline(_p): return None
    def _preview_get_baseline(): return None
    def _preview_init_masks(_p, det_w=None, det_h=None): return None

logger = logging.getLogger(__name__)


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _crop_wide_bytes(
    frame: np.ndarray,
    bbox: tuple[int, int, int, int],
    osd_masks: list[tuple[int, int, int, int]] | None = None,
) -> bytes:
    """Close-up crop around the detection — VLM input.

    Padding = max(bbox_dim * MULT, MIN_PAD_PX) per axis. Bumping either dial
    gives the VLM more scene context — helpful for small bboxes where the
    model needs surroundings to reason about scale ("is this on the floor or
    on a wall?"). Env: VLM_CROP_PAD_MULT (default 3), VLM_CROP_MIN_PAD (default 160).

    osd_masks: OSD/timestamp regions (in frame coords) blanked to black on the
    frame copy BEFORE cropping. Without this, mismatched timestamps between
    baseline and current frame become the dominant visual diff — Sonnet
    correctly identifies "only the timestamp changed" and rejects real
    detections. Was already applied to baseline pixel-diff calc; needs to
    apply to the VLM crop too or the model is misled.
    """
    if osd_masks:
        frame = frame.copy()
        fh, fw = frame.shape[:2]
        for mx1, my1, mx2, my2 in osd_masks:
            mx1 = max(0, min(fw, mx1)); mx2 = max(0, min(fw, mx2))
            my1 = max(0, min(fh, my1)); my2 = max(0, min(fh, my2))
            if mx2 > mx1 and my2 > my1:
                frame[my1:my2, mx1:mx2] = 0
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = max(1, x2 - x1), max(1, y2 - y1)
    pad_mult = float(os.getenv("VLM_CROP_PAD_MULT", "3"))
    pad_min = int(os.getenv("VLM_CROP_MIN_PAD", "160"))
    pad_x = max(int(bw * pad_mult), pad_min)
    pad_y = max(int(bh * pad_mult), pad_min)
    crop = frame[max(0, y1 - pad_y): min(h, y2 + pad_y),
                 max(0, x1 - pad_x): min(w, x2 + pad_x)]
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else b""


def _crop_wide_from_ndarray(
    frame: np.ndarray,
    bbox: tuple[int, int, int, int],
    osd_masks: list[tuple[int, int, int, int]] | None = None,
) -> bytes:
    """Same crop geometry as _crop_wide_bytes — separated so the baseline
    (already an ndarray in memory) can be sliced without a round-trip to bytes."""
    return _crop_wide_bytes(frame, bbox, osd_masks=osd_masks)


def _crop_wide_bytes_with_motion_overlay(
    frame: np.ndarray,
    bbox: tuple[int, int, int, int],
    contour,
) -> bytes:
    """Same geometry as _crop_wide_bytes, but draws the motion contour on a
    frame copy before cropping so the VLM sees an explicit outline of what
    moved. Encodes the temporal signal (which frames alone can't convey)
    into the spatial signal the model actually consumes.

    Fallback: if contour is None, degrades to _crop_wide_bytes."""
    if contour is None:
        return _crop_wide_bytes(frame, bbox)
    annotated = frame.copy()
    # Bright green outline — high contrast against both IR-grey nighttime
    # and daytime scenes. Filled at low alpha to hint the interior region
    # without occluding animal texture the model needs to classify species.
    overlay = annotated.copy()
    cv2.drawContours(overlay, [contour], -1, (0, 255, 0), thickness=cv2.FILLED)
    cv2.addWeighted(overlay, 0.25, annotated, 0.75, 0, annotated)
    cv2.drawContours(annotated, [contour], -1, (0, 255, 0), thickness=2)
    return _crop_wide_bytes(annotated, bbox)


def _decode_baseline(jpeg: bytes, expected_w: int, expected_h: int) -> "np.ndarray | None":
    """Decode the persisted baseline JPEG and normalize to the detection
    resolution so bbox coords line up 1:1 with the current frame."""
    if not jpeg:
        return None
    try:
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        if (w, h) != (expected_w, expected_h):
            img = cv2.resize(img, (expected_w, expected_h))
        logger.info("Baseline decoded: %dx%d (normalized to %dx%d)", w, h, expected_w, expected_h)
        return img
    except Exception:
        logger.exception("Baseline decode failed")
        return None


def _diff_score(baseline: np.ndarray, current: np.ndarray,
                bbox: tuple[int, int, int, int],
                osd_masks: list[tuple[int, int, int, int]] | None = None) -> float:
    """Mean grayscale absdiff normalized to 0..1 within the bbox region.

    0.0 = identical, 1.0 = maximum contrast change. Values below ~0.05 usually
    mean lighting jitter or IR noise; a real animal moving into the frame
    typically pushes the region to 0.10+ within a bbox that is sized to it.

    OSD masks are pixel regions where the camera burns in timestamp / watermark
    text that changes every frame. If the bbox intersects any mask, the mask
    region is zeroed in both baseline and current before absdiff so the OSD
    churn doesn't dominate the score.
    """
    x1, y1, x2, y2 = bbox
    h, w = current.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return 1.0  # degenerate bbox → don't over-filter
    if baseline.shape[:2] != current.shape[:2]:
        return 1.0  # size mismatch → skip the pre-filter, let VLM decide
    a = cv2.cvtColor(baseline[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY).copy()
    b = cv2.cvtColor(current[y1:y2, x1:x2],  cv2.COLOR_BGR2GRAY).copy()
    # Zero out any OSD-mask region that overlaps the bbox crop.
    if osd_masks:
        for mx1, my1, mx2, my2 in osd_masks:
            ox1 = max(0, mx1 - x1); oy1 = max(0, my1 - y1)
            ox2 = min(x2 - x1, mx2 - x1); oy2 = min(y2 - y1, my2 - y1)
            if ox2 > ox1 and oy2 > oy1:
                a[oy1:oy2, ox1:ox2] = 0
                b[oy1:oy2, ox1:ox2] = 0
    # Normalize by mean brightness — subtracting each crop's mean before diff
    # kills global lighting shifts (sun/cloud shadow drift) that would otherwise
    # inflate the score for the whole frame equally. Real object appearance
    # changes the local pattern *relative to* the mean, so the diff survives.
    a_f = a.astype(np.int16) - int(a.mean())
    b_f = b.astype(np.int16) - int(b.mean())
    return float(np.abs(a_f - b_f).mean()) / 255.0


def _in_any_mask(point: tuple[int, int],
                 masks: list[tuple[int, int, int, int]]) -> bool:
    """Return True if (x, y) falls inside any [x1, y1, x2, y2] mask."""
    x, y = point
    for mx1, my1, mx2, my2 in masks:
        if mx1 <= x < mx2 and my1 <= y < my2:
            return True
    return False


def _is_normalized_polygon(pts: list) -> bool:
    """Heuristic: coords are normalized (0..1 floats) if ALL values are <= 1.5.
    Anything larger is treated as absolute pixel coordinates."""
    for pair in pts:
        for v in pair:
            if v > 1.5:
                return False
    return True


def _scale_polygon(pts: list, det_w: int, det_h: int) -> list[tuple[int, int]]:
    """Convert polygon coords (either normalized floats or absolute pixels) to
    absolute pixel coords at the current detection resolution. If input is
    already pixels, returns unchanged (assumed to match det_w×det_h)."""
    if not pts:
        return []
    if _is_normalized_polygon(pts):
        return [(int(round(x * det_w)), int(round(y * det_h))) for x, y in pts]
    return [(int(x), int(y)) for x, y in pts]


def _scale_masks(masks: list, det_w: int, det_h: int) -> list[tuple[int, int, int, int]]:
    """Same as _scale_polygon but for [x1,y1,x2,y2] rectangles."""
    scaled: list[tuple[int, int, int, int]] = []
    for m in masks:
        if len(m) != 4:
            continue
        if all(v <= 1.5 for v in m):   # normalized
            scaled.append((
                int(round(m[0] * det_w)), int(round(m[1] * det_h)),
                int(round(m[2] * det_w)), int(round(m[3] * det_h)),
            ))
        else:   # pixel
            scaled.append(tuple(int(v) for v in m))
    return scaled


class _BboxStats(NamedTuple):
    """Brightness/geometry signature for a bbox — the load-bearing feature
    triplet used by (1) the insect pre-filter (gate on mean/max/AR),
    (2) DECISION log lines (diagnostic), and (3) alert card descriptions
    (feature labeling analysis). Previously computed inline at 3 sites in
    pipeline.py with subtle divergence risk; centralized here."""
    w: int
    h: int
    area: int
    mean: float
    max: float
    aspect_ratio: float


def _bbox_signature(frame: np.ndarray, bbox: tuple[int, int, int, int]) -> _BboxStats:
    """Compute the brightness/geometry signature of a bbox region within a frame.
    Safe against zero-area bboxes (returns 0.0 for mean/max)."""
    x1, y1, x2, y2 = bbox
    w = max(0, x2 - x1)
    h = max(0, y2 - y1)
    area = w * h
    crop = frame[max(0, y1):y2, max(0, x1):x2]
    if crop.size > 0:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        mean = float(gray.mean())
        max_ = float(gray.max())
    else:
        mean = max_ = 0.0
    aspect_ratio = max(w, h) / max(1, min(w, h))
    return _BboxStats(w=w, h=h, area=area, mean=mean, max=max_, aspect_ratio=aspect_ratio)


def _annotate(
    frame: np.ndarray,
    all_dets: list[Detection],
    zone_dets: list[Detection],
    alert_ids: set[int],
    zone_polygon: list[tuple[int, int]] | None,
) -> np.ndarray:
    """Draw YOLO boxes (green) + motion boxes (yellow) + zone polygon (cyan) +
    alert overlays (red) onto a copy of the frame. Used for the preview stream.

    Pan-storm guard: when the detection count blows past
    PREVIEW_ANNOTATE_MAX_DETS (default 40), the camera is almost certainly
    mid-pan — MOG fires everywhere as the scene shifts, YOLO labels many
    tall-thin blobs as `person 50%`, and the resulting overlay is dense
    visual noise that doesn't inform anyone. In that case we skip
    drawing individual det boxes (still draw the zone polygon so
    orientation is preserved) and stamp the frame with a hint.
    """
    out = frame.copy()
    zone_ids = {d.track_id for d in zone_dets}

    if zone_polygon:
        pts = np.array(zone_polygon, dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(out, [pts], isClosed=True, color=(255, 200, 0), thickness=2)

    _max_dets = int(os.getenv("PREVIEW_ANNOTATE_MAX_DETS", "40"))
    if len(all_dets) > _max_dets:
        # Pan storm — skip individual boxes; stamp a hint.
        label = f"pan-storm: {len(all_dets)} dets (annotation suppressed)"
        cv2.putText(out, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 200, 255), 2, cv2.LINE_AA)
        return out

    for det in all_dets:
        x1, y1, x2, y2 = det.bbox
        in_zone = det.track_id in zone_ids
        alerted = det.track_id in alert_ids
        # MOG2 motion blobs get track_id ≥ 1000 by convention in object_detector.py
        is_motion = det.track_id >= 1000

        if alerted:
            color, thickness = (0, 0, 255), 3           # red — alert
        elif is_motion and in_zone:
            color, thickness = (0, 220, 220), 2         # yellow — motion in zone
        elif in_zone:
            color, thickness = (0, 255, 100), 2         # green — YOLO in zone
        elif is_motion:
            color, thickness = (0, 120, 120), 1         # dim yellow — motion outside zone
        else:
            color, thickness = (60, 60, 60), 1          # grey — YOLO outside zone

        cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)
        if in_zone or alerted:
            source = "MOG2" if is_motion else "YOLO"
            label = f"[{source}] {det.class_name} #{det.track_id} {det.confidence:.0%}"
            cv2.putText(out, label, (x1, max(y1 - 6, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    return out


def _save_reject_crop(crop_bytes: bytes, tid: int, bbox: tuple,
                      bw: int, bh: int, description: str) -> None:
    """Write the exact JPEG the VLM was shown into snapshots/rejected/<date>/.

    Naming: vlmreject_<HHMMSS>_track<tid>_<W>x<H>.jpg. Description is stashed
    in a same-name .txt sidecar so you can see what VLM said about the crop.
    Guarded by SAVE_REJECTED_CROPS=1 — diagnostic mode, disk-hungry over time.
    """
    from datetime import datetime
    from pathlib import Path
    snap_root = Path(os.getenv("SNAPSHOT_DIR", "snapshots"))
    camera_id = os.getenv("CAMERA_ID", "yard")
    now = datetime.now()
    # Camera-scoped subdir so multi-detector deploys don't mix each other's
    # rejection corpora in the same folder. Also filename prefix so an
    # ls without cd immediately says which camera the crop came from.
    day = snap_root / "rejected" / camera_id / now.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    stem = f"{camera_id}_vlmreject_{now.strftime('%H%M%S')}_track{tid}_{bw}x{bh}"
    (day / f"{stem}.jpg").write_bytes(crop_bytes)
    if description:
        (day / f"{stem}.txt").write_text(
            f"camera={camera_id}\nbbox={bbox}\ndims={bw}x{bh}\nvlm_description={description}\n",
            encoding="utf-8",
        )


def _overlaps(bbox_a: tuple, bbox_b: tuple, pad: int = 5, min_frac: float = 0.5) -> bool:
    ax1, ay1, ax2, ay2 = bbox_a
    bx1, by1, bx2, by2 = bbox_b
    ix1 = max(ax1, bx1 - pad)
    iy1 = max(ay1, by1 - pad)
    ix2 = min(ax2, bx2 + pad)
    iy2 = min(ay2, by2 + pad)
    if ix2 <= ix1 or iy2 <= iy1:
        return False
    a_area = max(1, (ax2 - ax1) * (ay2 - ay1))
    return (ix2 - ix1) * (iy2 - iy1) / a_area >= min_frac


def run(stream_url: str | None = None, video_path: str | None = None,
        shutdown_event=None) -> None:
    cfg_det = _load_yaml("config/detection.yaml")
    cfg_alerts = _load_yaml("config/alerts.yaml")

    det_cfg = cfg_det["detector"]
    mot_cfg = cfg_det.get("motion_detector", {})
    # ZONE_KEY env takes precedence over YAML so each camera in the multi-cam
    # docker deploy can point at its own polygon (yard_zone, rooftop_zone, …)
    # without needing to swap detection.yaml per container.
    zone_key = os.getenv("ZONE_KEY") or cfg_det.get("zone_key", "yard_zone")
    _zones_section = cfg_det.get("zones", {})
    if zone_key not in _zones_section:
        # Cameras added before their zone is drawn get an empty-polygon default
        # — pipeline runs full-frame until the operator saves a real one via UI.
        logger.warning("ZONE_KEY '%s' not found in config/detection.yaml — using empty polygon "
                       "(full-frame detection until UI zone save)", zone_key)
        _raw_polygon = []
    else:
        # Load polygon → auto-detect if it's normalized floats (new format) or
        # absolute pixels (legacy). See _is_normalized_polygon() heuristic below.
        _raw_polygon = _zones_section[zone_key].get("polygon", [])

    video_path = video_path or os.getenv("VIDEO_PATH", "") if stream_url is None else ""
    if video_path:
        stream = VideoFileHandler(
            path=video_path,
            loop=os.getenv("VIDEO_LOOP", "true").lower() != "false",
            speed=float(os.getenv("VIDEO_SPEED", "1.0")),
        )
    else:
        stream = RTSPHandler(url=stream_url or os.environ["RTSP_URL"])

    det_w = int(os.getenv("INPUT_WIDTH", det_cfg["input_width"]))
    det_h = int(os.getenv("INPUT_HEIGHT", det_cfg["input_height"]))

    detector = ObjectDetector(
        model_path=os.getenv("YOLO_MODEL", det_cfg["model"]),
        threshold=float(os.getenv("INFERENCE_THRESHOLD", det_cfg["threshold"])),
        input_size=(det_w, det_h),
        min_area_fraction=det_cfg["min_area_fraction"],
        max_area_fraction=det_cfg["max_area_fraction"],
        stationary_px=cfg_det.get("stationary_mask", {}).get("pixel_threshold", 8),
        stationary_frames=cfg_det.get("stationary_mask", {}).get("frames", 60),
        tracker_config=os.getenv("TRACKER_CONFIG", "config/botsort.yaml"),
        yolo_imgsz=int(os.getenv("YOLO_IMGSZ", "640")),
    )

    motion = MotionDetector(
        history=int(os.getenv("MOTION_HISTORY", mot_cfg.get("history", 400))),
        var_threshold=float(os.getenv("MOTION_VAR_THRESHOLD", mot_cfg.get("var_threshold", 18))),
        min_area=int(os.getenv("MOTION_MIN_AREA", mot_cfg.get("min_area", 80))),
        max_area=int(os.getenv("MOTION_MAX_AREA", mot_cfg.get("max_area", 4000))),
        edge_margin=int(mot_cfg.get("edge_margin", 20)),
        seam_x=int(mot_cfg.get("seam_x", 0)),
        seam_margin=int(mot_cfg.get("seam_margin", 0)),
    )

    # Now that det_w/det_h are known, scale polygon (works for either format).
    zone_polygon = _scale_polygon(_raw_polygon, det_w, det_h)
    zone_filter = ZoneFilter(zones={zone_key: zone_polygon})
    # Log the loaded polygon at startup so it's visible in detector.log and
    # any "detections outside the zone" complaint can be cross-checked.
    _poly_bounds = (
        (min(p[0] for p in zone_polygon), min(p[1] for p in zone_polygon),
         max(p[0] for p in zone_polygon), max(p[1] for p in zone_polygon))
        if zone_polygon else None
    )
    logger.info("Zone '%s' loaded: %d vertices, bounds=%s (detection frame is %dx%d)",
                zone_key, len(zone_polygon), _poly_bounds, det_w, det_h)

    # OSD masks — regions of the video feed with burned-in text (timestamp,
    # camera model) that change every second. Bboxes centered in a mask are
    # rejected; the baseline diff pre-filter zeros out mask pixels before
    # computing absdiff so OSD churn doesn't fool the pipeline into a VLM call.
    # Same normalize-or-pixel detection as the polygon.
    # Camera-scoped OSD masks (dict form) with backwards-compat for legacy
    # flat list. Same shape as osd_masks handling in MaskHolder.
    _osd_cfg = cfg_det.get("osd_masks", []) or []
    _camera_id_env = os.getenv("CAMERA_ID", "yard")
    if isinstance(_osd_cfg, dict):
        _osd_raw = _osd_cfg.get(_camera_id_env, []) or []
    else:
        _osd_raw = _osd_cfg if _camera_id_env == "yard" else []
    osd_masks = _scale_masks(_osd_raw, det_w, det_h)
    if osd_masks:
        logger.info("OSD masks (%s): %d region(s) %s", _camera_id_env, len(osd_masks), osd_masks)

    # Hot-reload for OSD masks (mirrors the zone hot-reload). The preview UI
    # bumps the mask version on save; the pipeline picks it up next iteration.
    mask_holder = _preview_init_masks("config/detection.yaml", det_w=det_w, det_h=det_h)
    _mask_version = 0
    if mask_holder is not None:
        _, _mask_version = mask_holder.snapshot()

    # Preview zone-editor hot-reload: preview.ZoneHolder loads from the same
    # yaml, publishes a version on every save. Pipeline polls it each iteration
    # and rebuilds ZoneFilter when the version changes.
    # det_w/det_h passed so the holder normalizes polygon coords when persisting.
    zone_holder = _preview_init_zones("config/detection.yaml", zone_key, det_w=det_w, det_h=det_h)
    _zone_version = 0
    if zone_holder is not None:
        _, _zone_version = zone_holder.snapshot()

    # Env-driven factory: single-backend or cascade (VLM_BACKEND=cascade).
    from src.vlm.analyzer import build_vlm_analyzer_from_env
    vlm = build_vlm_analyzer_from_env()

    # Publish stats source metadata for the /status endpoint.
    _preview_stats.set_backend(vlm._backend if hasattr(vlm, "_backend") else "unknown")
    try:
        # RTSPHandler stores URL as ._url; VideoFileHandler as ._path.
        _cam_label = (getattr(stream, "_url", None)
                      or getattr(stream, "_path", None)
                      or "unknown")
        # Strip user:pass so we don't leak creds through the status endpoint.
        if isinstance(_cam_label, str) and "@" in _cam_label:
            _cam_label = _cam_label.split("@", 1)[1]
        _preview_stats.set_camera(str(_cam_label))
    except Exception:
        _preview_stats.set_camera("unknown")
    _preview_stats.set_detection_size(det_w, det_h)

    ha_base = os.getenv("HA_WEBHOOK_URL", "").rsplit("/api/", 1)[0]
    notifier = Notifier(
        config=cfg_alerts["alerts"],
        ha_webhook_base=ha_base,
        ha_token=os.getenv("HA_TOKEN", ""),
    )

    # Preview alert-log source of truth — points /snapshots/<name> at the same
    # directory Notifier writes JPEGs into.
    _preview_init_alert_log(cfg_alerts["alerts"].get("snapshot_dir", "snapshots"))

    # Baseline holder — persisted at data/baseline.jpg; loaded on startup if it
    # exists. When populated, the pipeline runs a pixel-diff pre-filter within
    # each motion bbox and sends the reference alongside the current frame to
    # the VLM. See _diff_score() and _decode_jpeg() below.
    _baseline_holder = _preview_init_baseline(os.getenv("BASELINE_PATH", "data/baseline.jpg"))
    # (version, mode) → decoded ndarray. Cache invalidates when either changes.
    _baseline_cache: tuple[tuple, "np.ndarray | None"] = ((-1, ""), None)
    # Night IR baseline is stable; a 6% mean-diff crossing motion clearly.
    # Daytime shadows produce ~0.05-0.10 diffs on a per-second basis; use a
    # higher threshold when the day baseline is active to keep the VLM budget
    # sane during shadow-heavy afternoon hours.
    _baseline_diff_threshold_night = float(os.getenv("BASELINE_DIFF_THRESHOLD", "0.06"))
    _baseline_diff_threshold_day = float(os.getenv("DAY_BASELINE_DIFF_THRESHOLD", "0.12"))
    # Filter tiny bboxes at the motion stage — real rats measured 24x22 minimum
    # in our production data. Anything smaller is bugs, dust, or shadow-edge noise.
    _min_motion_bbox_px = int(os.getenv("MIN_MOTION_BBOX_PX", "22"))

    # Load-shedding at the VLM ingress: bounded in-flight cap + drop-oldest
    # eviction. Without this, sustained motion (real rodent flood OR
    # human-in-frame FP storm) buffers hundreds of jobs in the executor
    # queue, each pinning a frame.copy() (~2MB) and delaying alert.ts by
    # minutes. Empirically measured queue reaching 670+ jobs / 64s lag
    # before this fix.
    _VLM_WORKERS = int(os.getenv("VLM_WORKERS", "4"))
    _VLM_MAX_INFLIGHT = int(os.getenv("VLM_MAX_INFLIGHT", "4"))
    _VLM_MAX_ALERT_AGE_S = float(os.getenv("VLM_MAX_ALERT_AGE_S", "10"))
    # Human presence heartbeat — fire one alert every N seconds when a
    # person is detected in frame. Whole-frame human-exclusion suppresses
    # all rodent detections silently; this restores visibility of "human
    # was here" without spamming (one per interval, not per frame).
    _HUMAN_ALERT_INTERVAL_S = float(os.getenv("HUMAN_ALERT_INTERVAL_S", "300"))
    _last_human_alert_ts = 0.0
    # Night insect brightness threshold — bbox mean grayscale above this
    # value is classified as insect (moth wings reflect IR) rather than
    # sent to VLM. 130/255 catches obvious wing-reflect FPs without
    # touching low-conf mammal crops that are already dark. Bump higher
    # if legit rodents get mis-flagged; lower if moths still slip through.
    _NIGHT_INSECT_BRIGHTNESS_MIN = float(os.getenv("NIGHT_INSECT_BRIGHTNESS_MIN", "130"))
    # Peak-brightness gate: a moth streak against dark ground averages
    # ~40-60 mean but pixel max hits 255. Rodent fur under IR maxes
    # around 180. Any small bbox whose brightest pixel is at or above
    # this threshold gets classified as insect. Catches the thin-fuzzy-
    # streak failure mode the mean-brightness gate misses. 220/255 is
    # tight enough to avoid mammal false-positives while catching moth
    # wing IR reflections.
    _NIGHT_INSECT_MAX_BRIGHTNESS_MIN = float(os.getenv("NIGHT_INSECT_MAX_BRIGHTNESS_MIN", "220"))
    # Elongation gate: moth wings/streaks are aspect-ratio > 2.5 (thin
    # bright line). Rodents in overhead crops are roughly square (AR
    # ~1.0-1.6). Small bbox + high AR + bright pixel = insect regardless
    # of mean brightness. Guards against blur-elongated moth streaks
    # that avoid both the mean and max gates by having lots of dark
    # background pixels padding one axis.
    _NIGHT_INSECT_ELONGATION_MIN = float(os.getenv("NIGHT_INSECT_ELONGATION_MIN", "2.5"))
    # Insect pre-filter is a moth-specific hard rail. Moths are
    # physically small (usually ~15-40 px per axis, sometimes up to
    # ~50 px with wing-motion blur). Cats, raccoons, opossums at
    # overhead angle project as 45-100+ px blobs — they inherit the
    # "bright IR reflection" property of moths (fur/pale patches
    # against IR) but they're an order of magnitude larger. Bounding
    # the insect filter to bbox area < INSECT_FILTER_MAX_AREA_PX keeps
    # the moth-suppression benefit without falsely rejecting mammal-
    # sized crops. Default 2000 = 45x45 blob; anything bigger goes to
    # the VLM regardless of brightness.
    _INSECT_FILTER_MAX_AREA_PX = int(os.getenv("INSECT_FILTER_MAX_AREA_PX", "2000"))
    # Daytime detection skip — hard-off during daylight hours when shadow
    # FPs dominate signal on some cameras. Uses same brightness threshold
    # as the baseline picker (DAY_NIGHT_BRIGHTNESS_THRESHOLD). When the
    # frame's mean grayscale >= threshold, skip the whole detection path;
    # preview still publishes with a "daytime paused" banner so operator
    # sees the camera isn't dead. Set 0/false to disable (default).
    _SKIP_DAYTIME_DETECTION = os.getenv("SKIP_DAYTIME_DETECTION", "0") == "1"
    # Two-signal daytime detection — fires when EITHER trigger is active.
    # Rationale: brightness alone is fragile (Reolink auto-exposure sits
    # yard at grayscale 108 at 3PM despite being clearly daylight); time-
    # of-day alone doesn't adapt to overcast dawn/dusk edges.
    _DAYTIME_SKIP_BRIGHTNESS_THRESHOLD = int(os.getenv("DAYTIME_SKIP_BRIGHTNESS_THRESHOLD",
                                                        os.getenv("DAY_NIGHT_BRIGHTNESS_THRESHOLD", "100")))
    _DAYTIME_SKIP_START_HOUR = int(os.getenv("SKIP_DAYTIME_START_HOUR", "7"))
    _DAYTIME_SKIP_END_HOUR = int(os.getenv("SKIP_DAYTIME_END_HOUR", "19"))
    # DAYTIME_SKIP_TRIGGER_MODE controls how the two signals combine:
    #   "or"  (default): skip fires if EITHER brightness OR time hit.
    #                    Original behavior — dusk brightness can extend
    #                    skip past end_hour.
    #   "and":           both must fire. Predictable resume at end_hour
    #                    sharp but daytime storms with dipped brightness
    #                    won't skip.
    #   "time_only":     ignore brightness signal, use time only.
    #                    Backyard uses this (user wants exact 7 PM start).
    #   "brightness_only": ignore time signal, use brightness only.
    _DAYTIME_SKIP_TRIGGER_MODE = os.getenv("DAYTIME_SKIP_TRIGGER_MODE", "or").lower()
    # Operator pause flag — file-sentinel at /app/config/pause_all.flag.
    # When present, all detectors skip the entire detection path with an
    # "operator paused" banner. Toggled via web UI (POST /api/pause).
    # File-based instead of env so it's runtime-toggleable without a
    # restart; each detector's config/ is bind-mounted from the same host
    # dir so a single touch/rm reaches all three.
    _PAUSE_FLAG_PATH = os.getenv("OPERATOR_PAUSE_FLAG_PATH", "/app/config/pause_all.flag")
    # Scene-chaos bulkhead — two orthogonal signals that gate the per-det
    # inner loop when the frame isn't worth processing. Isolates chaotic
    # frames from the stable detection path so one bad frame doesn't drag
    # loop latency (and downstream VLM queue pressure) for the next 30.
    #
    # (a) frame-diff scene-change: >X fraction of pixels changed vs
    #     previous frame. Catches manual PTZ (via camera app), zoom,
    #     PIR light kick-on, brief loss-of-sync — any whole-scene
    #     shift MOG would misread as motion. 0.4 = 40% of the 160x90
    #     downsampled frame differing by ≥30 grayscale levels.
    # (b) bbox-flood: too many raw dets in one frame means MOG stormed
    #     (foliage wind, IR flicker on textured surfaces, first frames
    #     post-restart before MOG warmup). Skip the whole per-det inner
    #     loop rather than pay ~5ms/det on garbage. Complements the
    #     softer PREVIEW_ANNOTATE_MAX_DETS annotation guard: annotation
    #     suppression at 40, full processing skip at 60.
    # Lower defaults after live observation 08/10 02:09: 0.4 + delta=30
    # missed slow/blurry pans where per-pixel delta was 15-25 on most
    # pixels. 0.2 + delta=15 catches those without triggering on
    # rodent-scale motion (a rat is <1% of the 160x90 downsample).
    # Raised 0.2 → 0.5 after 08/10 10:46 cat-crossing incident: a cat
    # crossing the yard generates whole-frame secondary motion (branches,
    # ground debris, cat contour itself) that hit ~60% pixel change and
    # falsely tripped scene-change gate. Cat's frames got skipped, only
    # eyeshine moment survived (misclassified as mouse). 0.5 lets normal
    # large-animal crossings through while still catching real pans
    # (typically 70-90% pixel change).
    _SCENE_CHANGE_SKIP_FRACTION = float(os.getenv("SCENE_CHANGE_SKIP_FRACTION", "0.5"))
    _SCENE_CHANGE_PIXEL_DELTA = int(os.getenv("SCENE_CHANGE_PIXEL_DELTA", "15"))
    # Large-bbox exemption: chaos gates (scene-change + bbox-flood) should
    # NOT gate when a single large bbox is present — that's a big animal
    # (cat, opossum, raccoon, dog) whose crossing looks like a pan to the
    # whole-frame signals. 15000 px = ~123x123 blob = definitely-not-noise.
    # Cats project as 300-500 px per side at yard's geometry so 15000 is
    # a conservative floor. Set 0 to disable exemption.
    _CHAOS_EXEMPT_MIN_BBOX_AREA = int(os.getenv("CHAOS_EXEMPT_MIN_BBOX_AREA", "15000"))
    # Cooldown-after-fire (hysteresis): a pan is 10-30 frames long.
    # Single-frame gate leaks 1-3 frames through, and any single leaked
    # frame that alerts sticks on the preview for ALERT_FLASH_FRAMES.
    # Once scene-change OR bbox-flood fires, hold the skip for N more
    # frames — same shape as _self_slew_in_transition() but signal-
    # driven instead of command-driven. 20 frames ≈ 1s at 20fps,
    # covering the typical pan duration.
    _SCENE_CHANGE_COOLDOWN_FRAMES = int(os.getenv("SCENE_CHANGE_COOLDOWN_FRAMES", "20"))
    # Aligned with PREVIEW_ANNOTATE_MAX_DETS (40) so the annotation-
    # suppressed + processing-skipped states converge — the 40-59 window
    # was paying full per-det cost while already showing the "pan-storm
    # suppressed" banner. Now: >40 dets → skip both.
    _MAX_DETS_PER_FRAME_SKIP = int(os.getenv("MAX_DETS_PER_FRAME_SKIP", "40"))
    _prev_scene_gray: np.ndarray | None = None
    _chaos_cooldown_remaining = 0
    vlm_pool = ThreadPoolExecutor(max_workers=_VLM_WORKERS, thread_name_prefix="vlm")
    logger.info("VLM pool: workers=%d max_inflight=%d max_alert_age=%.1fs",
                _VLM_WORKERS, _VLM_MAX_INFLIGHT, _VLM_MAX_ALERT_AGE_S)
    # (future, snap_frame, bbox, yolo_conf, crop_bytes_sent_to_vlm, submit_ts)
    # submit_ts tags job at submission so we can measure queue-age at harvest —
    # deep queue means alerts fire N seconds after their triggering frame.
    vlm_jobs: dict[int, tuple[Future, np.ndarray, tuple, float, bytes, float]] = {}
    last_vlm_ts: dict[int, float] = {}
    VLM_INTERVAL_S = float(os.getenv("VLM_INTERVAL_S", "2.0"))
    # Stationary-FP suppression: blinking indicator lights (LEDs, chargers,
    # camera IR emitters) turn on/off at the same pixel spot repeatedly.
    # MOG2/KNN sees each transition as fresh foreground and creates a new
    # track per blink, so per-track debouncers don't help — each track is a
    # unique ID at the same location. Keep a rolling window of recently
    # VLM-rejected bbox centers; if a new candidate falls within
    # STATIONARY_FP_RADIUS_PX of any recent reject, skip VLM (and the
    # baseline-diff work leading up to it).
    _stationary_fp_radius = int(os.getenv("STATIONARY_FP_RADIUS_PX", "15"))
    _stationary_fp_ttl_s = float(os.getenv("STATIONARY_FP_TTL_S", "120"))
    _stationary_fp_min_hits = int(os.getenv("STATIONARY_FP_MIN_HITS", "3"))
    # list of (cx, cy, first_ts, count)
    _recent_fp_centers: list[list] = []
    _save_rejected_crops = os.getenv("SAVE_REJECTED_CROPS", "0") == "1"
    if _save_rejected_crops:
        logger.info("SAVE_REJECTED_CROPS=1 — rejected VLM crops will be dumped to snapshots/rejected/")
    _motion_overlay_enabled = os.getenv("VLM_MOTION_OVERLAY", "0") == "1"
    if _motion_overlay_enabled:
        logger.info("VLM_MOTION_OVERLAY=1 — VLM crops will get motion-contour outline "
                    "(escalation for camouflaged targets in high-texture backgrounds)")

    # Temporal multi-frame VLM: buffer the last N raw frames, then on VLM
    # dispatch crop the same bbox from each and send the sequence. Gives
    # the VLM motion evidence across frames — biggest lever for small-
    # target detections against high-texture backgrounds (e.g. rooftop
    # rat crawling along a fence with slat interference) where a single
    # still confidently rejects even with a baseline diff.
    #
    # Off by default (memory + per-call latency cost). Enable per-camera
    # via env: VLM_TEMPORAL_ENABLED=1 in the detector-rooftop compose
    # block.
    _temporal_enabled = os.getenv("VLM_TEMPORAL_ENABLED", "0") == "1"
    _temporal_frames = int(os.getenv("VLM_TEMPORAL_FRAMES", "4"))
    _temporal_window_s = float(os.getenv("VLM_TEMPORAL_WINDOW_S", "1.0"))
    _recent_frames: deque[tuple[float, np.ndarray]] = deque(maxlen=_temporal_frames)
    if _temporal_enabled:
        logger.info(
            "VLM_TEMPORAL_ENABLED=1 — buffering last %d raw frames (%.1fs window) "
            "for temporal VLM dispatch",
            _temporal_frames, _temporal_window_s,
        )

    # Track IDs that fired a positive alert this frame — drawn red on the preview.
    # Cleared each iteration; brief flash is fine, cooldown suppresses re-fires anyway.
    alert_ids: set[int] = set()
    # Alert LRU so the red flash persists across a few frames per event (~2s at 20 fps).
    alert_ttl: dict[int, int] = {}
    ALERT_FLASH_FRAMES = int(os.getenv("PREVIEW_ALERT_FLASH_FRAMES", "40"))
    PREVIEW_EVERY_N = max(1, int(os.getenv("PREVIEW_EVERY_N", "2")))
    _frame_count = 0

    stream.start()
    logger.info("Pipeline running — press Ctrl-C to stop")

    try:
        while True:
            # Cooperative shutdown — checked once per iteration and again after
            # the blocking stream.get_frame() call returns. This is what makes
            # Ctrl+C actually work: the signal handler flips the event, we
            # notice on the next loop, exit through the finally block.
            if shutdown_event is not None and shutdown_event.is_set():
                logger.info("Shutdown event set — exiting main loop")
                break
            frame = stream.get_frame()
            if frame is None:
                continue

            fh, fw = frame.shape[:2]
            if (fw, fh) != (det_w, det_h):
                frame = cv2.resize(frame, (det_w, det_h))

            # Self-slew transition guard: while the PTZ is physically
            # panning, MOG would see the whole frame as motion. Skip
            # this iteration's MOG/YOLO work — no detections, no VLM
            # calls, no baseline update. Camera settles within
            # transition_pause_s (default 2 s, ~40 frames at 20 fps).
            if _self_slew_in_transition():
                continue

            # Operator pause (global) — file-sentinel toggled from web UI.
            # Wins over daytime skip below because it's an explicit
            # operator choice, not a heuristic. Preview still publishes
            # so the banner is visible.
            if os.path.exists(_PAUSE_FLAG_PATH):
                _frame_count += 1
                if _frame_count % PREVIEW_EVERY_N == 0:
                    ok_raw, buf_raw = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok_raw:
                        _publish_raw_frame(buf_raw.tobytes())
                    _paused_annotated = frame.copy()
                    cv2.putText(
                        _paused_annotated,
                        "detection paused (operator)",
                        (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0, 165, 255), 2, cv2.LINE_AA,
                    )
                    ok, buf = cv2.imencode(".jpg", _paused_annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    if ok:
                        _publish_preview_frame(buf.tobytes())
                continue

            # Daytime detection skip — some cameras (yard, backyard) have
            # dominant shadow FPs during daylight that overwhelm MOG.
            # Fires when EITHER trigger is active:
            #   (a) mean frame brightness >= DAYTIME_SKIP_BRIGHTNESS_THRESHOLD
            #   (b) local hour in [SKIP_DAYTIME_START_HOUR, SKIP_DAYTIME_END_HOUR)
            # (a) alone fails on shaded/overcast daylight where Reolink
            # auto-exposure sits at ~108 grayscale (below threshold);
            # (b) alone doesn't catch overcast dawn/dusk edges. Belt +
            # suspenders per operator.
            if _SKIP_DAYTIME_DETECTION:
                _small_dt = cv2.resize(frame, (160, 90))
                _mean_brightness = float(
                    (cv2.cvtColor(_small_dt, cv2.COLOR_BGR2GRAY) if _small_dt.ndim == 3 else _small_dt).mean()
                )
                _bright_hit = _mean_brightness >= _DAYTIME_SKIP_BRIGHTNESS_THRESHOLD
                _hour = time.localtime().tm_hour
                if _DAYTIME_SKIP_START_HOUR <= _DAYTIME_SKIP_END_HOUR:
                    _time_hit = _DAYTIME_SKIP_START_HOUR <= _hour < _DAYTIME_SKIP_END_HOUR
                else:  # wraps midnight
                    _time_hit = _hour >= _DAYTIME_SKIP_START_HOUR or _hour < _DAYTIME_SKIP_END_HOUR
                # Combine per DAYTIME_SKIP_TRIGGER_MODE. Unknown modes
                # fall back to "or" (original behavior).
                if _DAYTIME_SKIP_TRIGGER_MODE == "and":
                    _fire = _bright_hit and _time_hit
                elif _DAYTIME_SKIP_TRIGGER_MODE == "time_only":
                    _fire = _time_hit
                elif _DAYTIME_SKIP_TRIGGER_MODE == "brightness_only":
                    _fire = _bright_hit
                else:  # "or" or unknown
                    _fire = _bright_hit or _time_hit
                if _fire:
                    _trigger = "bright" if _bright_hit and not _time_hit else (
                        "time" if _time_hit and not _bright_hit else "both"
                    )
                    _frame_count += 1
                    if _frame_count % PREVIEW_EVERY_N == 0:
                        ok_raw, buf_raw = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                        if ok_raw:
                            _publish_raw_frame(buf_raw.tobytes())
                        _daytime_annotated = frame.copy()
                        cv2.putText(
                            _daytime_annotated,
                            f"detection paused (daytime, {_trigger}: b={_mean_brightness:.0f}/{_DAYTIME_SKIP_BRIGHTNESS_THRESHOLD} h={_hour}/{_DAYTIME_SKIP_START_HOUR}-{_DAYTIME_SKIP_END_HOUR})",
                            (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                            (0, 200, 255), 2, cv2.LINE_AA,
                        )
                        ok, buf = cv2.imencode(".jpg", _daytime_annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
                        if ok:
                            _publish_preview_frame(buf.tobytes())
                    continue

            # Chaos-cooldown decay: after any chaos-gate fire (scene-
            # change or bbox-flood), we hold the skip for N more frames
            # to cover the tail of a pan/zoom/lighting event. Same
            # bulkhead shape as _self_slew_in_transition() but signal-
            # driven instead of command-driven.
            #
            # Preview MUST still publish during cooldown or the video
            # freezes for the whole skip window — operator reads that as
            # "the app crashed" not "detection intentionally paused".
            # Stamp a banner on the frame so the pause is legible.
            if _chaos_cooldown_remaining > 0:
                _chaos_cooldown_remaining -= 1
                _frame_count += 1
                if _frame_count % PREVIEW_EVERY_N == 0:
                    ok_raw, buf_raw = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok_raw:
                        _publish_raw_frame(buf_raw.tobytes())
                    _cooldown_annotated = frame.copy()
                    cv2.putText(
                        _cooldown_annotated,
                        f"detection paused (chaos gate, {_chaos_cooldown_remaining} frames left)",
                        (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 200, 255), 2, cv2.LINE_AA,
                    )
                    ok, buf = cv2.imencode(".jpg", _cooldown_annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    if ok:
                        _publish_preview_frame(buf.tobytes())
                continue

            # Scene-change bulkhead — catches whole-scene shifts the
            # self-slew guard misses (manual PTZ via camera app, zoom,
            # PIR light kick, brief loss-of-sync). Downsample to 160x90,
            # abs-diff vs previous frame, skip when >SCENE_CHANGE_SKIP_
            # FRACTION of pixels differ by >= SCENE_CHANGE_PIXEL_DELTA
            # grayscale levels. ~1ms overhead per frame; catches events
            # ONVIF GetStatus polling would miss on non-ONVIF cameras
            # (yard Reolink, rooftop Annke).
            if _SCENE_CHANGE_SKIP_FRACTION < 1.0:
                _small = cv2.resize(frame, (160, 90))
                _gray_now = cv2.cvtColor(_small, cv2.COLOR_BGR2GRAY) if _small.ndim == 3 else _small
                if _prev_scene_gray is not None:
                    _diff = cv2.absdiff(_gray_now, _prev_scene_gray)
                    _changed_frac = float((_diff > _SCENE_CHANGE_PIXEL_DELTA).sum()) / _diff.size
                    if _changed_frac > _SCENE_CHANGE_SKIP_FRACTION:
                        _chaos_cooldown_remaining = _SCENE_CHANGE_COOLDOWN_FRAMES
                        logger.info(
                            "Scene-change skip: changed=%.2f > %.2f (pan/zoom/lighting) — cooldown=%d frames",
                            _changed_frac, _SCENE_CHANGE_SKIP_FRACTION, _chaos_cooldown_remaining,
                        )
                        _prev_scene_gray = _gray_now
                        continue
                _prev_scene_gray = _gray_now

            # Temporal buffer: keep the last N frames so a VLM dispatch
            # can pull recent history and crop the same bbox from each.
            # frame.copy() is unavoidable — subsequent iterations mutate
            # in-place through resize etc.
            if _temporal_enabled:
                _recent_frames.append((time.time(), frame.copy()))

            # ── Zone hot-reload check ────────────────────────────────────
            # The preview editor bumps the polygon version on POST; rebuild
            # ZoneFilter (cheap — just a Path constructor).
            if zone_holder is not None:
                _new_poly, _new_ver = zone_holder.snapshot()
                if _new_ver != _zone_version and len(_new_poly) >= 3:
                    zone_polygon = _new_poly
                    zone_filter = ZoneFilter(zones={zone_key: _new_poly})
                    _zone_version = _new_ver
                    logger.info("Zone reloaded (v=%d, %d vertices)", _new_ver, len(_new_poly))

            # ── OSD mask hot-reload check ────────────────────────────────
            if mask_holder is not None:
                _new_masks, _new_mver = mask_holder.snapshot()
                if _new_mver != _mask_version:
                    osd_masks = _new_masks
                    _mask_version = _new_mver
                    logger.info("OSD masks reloaded (v=%d, %d masks)", _new_mver, len(_new_masks))

            _preview_stats.record_frame()
            all_dets: list[Detection] = detector.detect(frame)

            # Motion supplement — the load-bearing signal for rodents (COCO
            # has no rat/mouse, so YOLO alone would miss them entirely).
            # Drop motion blobs that overlap a YOLO detection to avoid double-firing.
            motion_dets = motion.detect(frame)
            yolo_bboxes = [d.bbox for d in all_dets]
            motion_dets = [
                d for d in motion_dets
                if not any(_overlaps(d.bbox, yb) for yb in yolo_bboxes)
            ]
            # Enforce minimum bbox size — filters bugs, dust, shadow-edge noise
            # before any downstream stage does expensive work.
            if _min_motion_bbox_px > 0:
                motion_dets = [
                    d for d in motion_dets
                    if (d.bbox[2] - d.bbox[0]) >= _min_motion_bbox_px
                    and (d.bbox[3] - d.bbox[1]) >= _min_motion_bbox_px
                ]
            all_dets = all_dets + motion_dets
            if motion_dets:
                _preview_stats.record_motion(len(motion_dets))

            # Bbox-flood bulkhead — chaotic frames aren't worth the per-det
            # inner loop (baseline diff + insect check + VLM submit per
            # bbox at ~5ms each). Common triggers: MOG storm from wind-
            # swayed foliage, IR flicker on brick/tarp textures, first
            # frames post-restart before MOG background stabilizes.
            # Complements the softer PREVIEW_ANNOTATE_MAX_DETS annotation
            # guard: annotation suppression fires at 40, full processing
            # skip at 60. Set MAX_DETS_PER_FRAME_SKIP=0 to disable.
            if _MAX_DETS_PER_FRAME_SKIP > 0 and len(all_dets) > _MAX_DETS_PER_FRAME_SKIP:
                # Large-bbox exemption: if any single bbox is big enough
                # to be a real animal (cat, opossum, raccoon), process
                # the frame despite the flood. Otherwise a cat crossing
                # + wind-moved foliage gets treated like a pure pan-
                # storm (08/10 10:46 yard incident).
                _biggest_bbox_area = max(
                    (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1])
                    for d in all_dets
                )
                if (_CHAOS_EXEMPT_MIN_BBOX_AREA > 0
                        and _biggest_bbox_area >= _CHAOS_EXEMPT_MIN_BBOX_AREA):
                    logger.info(
                        "Bbox-flood EXEMPT: %d dets but biggest=%d px >= %d — processing (large animal likely)",
                        len(all_dets), _biggest_bbox_area, _CHAOS_EXEMPT_MIN_BBOX_AREA,
                    )
                else:
                    _chaos_cooldown_remaining = _SCENE_CHANGE_COOLDOWN_FRAMES
                    logger.info(
                        "Bbox-flood skip: %d dets > %d biggest=%d px — skipping frame per-det loop, cooldown=%d frames",
                        len(all_dets), _MAX_DETS_PER_FRAME_SKIP, _biggest_bbox_area, _chaos_cooldown_remaining,
                    )
                    continue
            # Surface the kinematic-gate reject counts (populated inside
            # MotionDetector.detect above) so the funnel chip can show
            # how much the velocity + persistence gates are killing.
            _v_rej, _p_rej = motion.pop_reject_counts()
            if _v_rej:
                _preview_stats.record_motion_kinematic_rejected("velocity", _v_rej)
            if _p_rej:
                _preview_stats.record_motion_kinematic_rejected("persistence", _p_rej)

            zone_dets = zone_filter.filter(all_dets, zone_key)
            # Reject any bbox whose center falls inside an OSD mask — the
            # timestamp/watermark region is never wildlife.
            if osd_masks:
                zone_dets = [d for d in zone_dets if not _in_any_mask(d.center, osd_masks)]

            # Human-exclusion mask: YOLO ran alongside motion detection and
            # separately captured any `person` bboxes (via detector's
            # `exclusion_bboxes` property). Two modes:
            #
            #   1. Per-bbox (default): motion whose center falls inside a
            #      human bbox is dropped. Handles shirt/hair flicker.
            #
            #   2. Whole-frame (HUMAN_EXCLUSION_WHOLE_FRAME=1, default on):
            #      when ANY person is in frame, drop ALL rodent-candidate
            #      motion in that frame. Empirically, a walking human
            #      generates dozens of small peripheral shadow/IR-reflection
            #      artifacts outside their body bbox that YOLO happily
            #      classifies as mouse/rat at conf 0.85. Blast-radius scoped
            #      to whole-frame prevents the FP flood + downstream VLM
            #      queue pressure. Trade: rodent that actually crosses while
            #      you're standing there won't fire — accepted given how
            #      pathological the FP rate is when a human is present.
            _exclusion_bboxes = detector.exclusion_bboxes
            _WHOLE_FRAME_EXCLUSION = os.getenv("HUMAN_EXCLUSION_WHOLE_FRAME", "1") == "1"
            if _exclusion_bboxes:
                _pre = len(zone_dets)
                if _WHOLE_FRAME_EXCLUSION:
                    zone_dets = []
                else:
                    zone_dets = [
                        d for d in zone_dets
                        if not any(
                            px1 <= d.center[0] <= px2 and py1 <= d.center[1] <= py2
                            for (px1, py1, px2, py2) in _exclusion_bboxes
                        )
                    ]
                _dropped = _pre - len(zone_dets)
                if _dropped > 0:
                    logger.info(
                        "human-exclusion (%s): dropped %d dets (persons=%d)",
                        "whole-frame" if _WHOLE_FRAME_EXCLUSION else "per-bbox",
                        _dropped, len(_exclusion_bboxes),
                    )
                # Human-presence heartbeat: emit one alert per interval so
                # the operator knows when human activity is suppressing
                # rodent detection. Debounce at the call site, NOT via
                # notifier's cooldown_seconds — otherwise every frame does
                # a wasted _save_snapshot before being cooldown-rejected.
                #
                # Species tag is deliberately "human_heartbeat" (not
                # "person") so the snapshot filename, alert row, and bbox
                # overlay all read as a heuristic-triggered heartbeat, not
                # a real YOLO/VLM classification of a human individual.
                # Snapshot lands at snapshots/YYYY-MM-DD/human_heartbeat_*.jpg
                # with the person bbox drawn + label "human_heartbeat 75%".
                _now_h = time.time()
                if _now_h - _last_human_alert_ts >= _HUMAN_ALERT_INTERVAL_S:
                    _p_bbox = _exclusion_bboxes[0]
                    _human_desc = (
                        f"[HEURISTIC] Human presence heartbeat — {len(_exclusion_bboxes)} "
                        f"person bbox(es) in frame; whole-frame rodent exclusion active."
                    )
                    _human_result = {
                        "wildlife_detected": True,
                        "species":           "human_heartbeat",
                        "is_rodent":         False,
                        "confidence":        0.75,
                        "description":       _human_desc,
                    }
                    _hp = notifier.send("human_heartbeat", _human_result, frame, _p_bbox, yolo_conf=0.75)
                    _hp_ref = None
                    if _hp:
                        try:
                            _hp_ref = str(_hp.relative_to(_hp.parent.parent)).replace('\\', '/')
                        except Exception:
                            _hp_ref = _hp.name
                    _preview_stats.record_alert(
                        species="human_heartbeat",
                        confidence=0.75,
                        description=_human_desc,
                        snapshot=_hp_ref,
                        track_id=None,
                        yolo_conf=0.75,
                    )
                    _last_human_alert_ts = _now_h
                    logger.info("Human heartbeat alert fired (%d person bbox(es), next in %.0fs)",
                                len(_exclusion_bboxes), _HUMAN_ALERT_INTERVAL_S)

            # Stationary-FP suppression — drop detections whose center is
            # within N pixels of a spot that VLM has already rejected M+ times
            # recently. Blinking indicator lights (LED, IR emitter, reflective
            # tape) fire hundreds of MOG2 events at the same pixel with fresh
            # track_ids each blink; without this filter, real detections at
            # other frame regions get drowned out by the debounce/rate-limit
            # budget being spent on a known-bad spot.
            _now_expire = time.time() - _stationary_fp_ttl_s
            _recent_fp_centers[:] = [e for e in _recent_fp_centers if e[2] >= _now_expire]
            if _recent_fp_centers:
                _suppress_cells = [(e[0], e[1]) for e in _recent_fp_centers
                                   if e[3] >= _stationary_fp_min_hits]
                if _suppress_cells:
                    _kept = []
                    for d in zone_dets:
                        cx, cy = d.center
                        if any(abs(cx - sx) <= _stationary_fp_radius
                               and abs(cy - sy) <= _stationary_fp_radius
                               for sx, sy in _suppress_cells):
                            continue
                        _kept.append(d)
                    _dropped = len(zone_dets) - len(_kept)
                    if _dropped > 0:
                        logger.debug("Stationary-FP suppression: dropped %d dets", _dropped)
                    zone_dets = _kept
            active_ids = {d.track_id for d in zone_dets}
            if zone_dets:
                _preview_stats.record_zone_motion(len(zone_dets))

            # ── Harvest completed VLM jobs ──────────────────────────────────
            for tid in list(vlm_jobs.keys()):
                fut, snap_fr, bbox, yolo_conf, crop_bytes, submit_ts = vlm_jobs[tid]
                if not fut.done():
                    continue
                del vlm_jobs[tid]
                try:
                    result = fut.result()
                except Exception:
                    logger.exception("VLM job for track=%d raised", tid)
                    continue

                _queue_age = time.time() - submit_ts
                _inflight = len(vlm_jobs)
                _stats = _bbox_signature(snap_fr, bbox)
                _bw, _bh = _stats.w, _stats.h
                _b_mean, _b_max, _b_ar = _stats.mean, _stats.max, _stats.aspect_ratio
                logger.info(
                    "DECISION track=%d species=%s rodent=%s conf=%.2f bbox=%s (%dx%d) mean=%.0f max=%.0f AR=%.2f vlm_queue_age=%.1fs inflight_after=%d",
                    tid, result.get("species"), result.get("is_rodent"),
                    result.get("confidence", 0.0), bbox, _bw, _bh,
                    _b_mean, _b_max, _b_ar, _queue_age, _inflight,
                )

                # Freshness deadline: discard alerts whose snapshot is too old
                # to be actionable. Belt-and-suspenders with the ingress cap —
                # if a job somehow slipped through and completed late, we'd
                # rather drop it than fire a minutes-old alert with a stale
                # snapshot. Load-bearing signal is silent success; log the
                # drop so we know when we're falling behind.
                if _queue_age > _VLM_MAX_ALERT_AGE_S:
                    logger.warning("VLM stale-drop: track=%d queue_age=%.1fs > %.1fs (workers/inflight too small for load) — discarding alert",
                                   tid, _queue_age, _VLM_MAX_ALERT_AGE_S)
                    continue

                if not result.get("wildlife_detected", False):
                    _preview_stats.record_vlm_rejected()
                    # Split the "rejected" bucket: real insect classifications
                    # are informative signal, not noise — the analyzer forced
                    # detected=false on species='insect' via the non-alertable
                    # gate, but we still want a distinct counter for funnel
                    # visibility ("moths flying tonight" vs "genuinely nothing").
                    if str(result.get("species", "")).lower() == "insect":
                        _preview_stats.record_vlm_insect()
                        logger.info("VLM insect classified: track=%d conf=%.2f — count-only, no alert",
                                    tid, result.get("confidence", 0.0))
                    # Record this bbox center for stationary-FP suppression —
                    # if new motion keeps firing at this same spot (blinking
                    # LED, camera IR emitter), we'll skip VLM entirely rather
                    # than burn cycles rejecting it 100x.
                    _cx = (bbox[0] + bbox[2]) // 2
                    _cy = (bbox[1] + bbox[3]) // 2
                    _now = time.time()
                    _matched = False
                    for entry in _recent_fp_centers:
                        if abs(entry[0] - _cx) <= _stationary_fp_radius \
                           and abs(entry[1] - _cy) <= _stationary_fp_radius:
                            entry[3] += 1
                            entry[2] = _now  # refresh TTL on re-hit
                            _matched = True
                            break
                    if not _matched:
                        _recent_fp_centers.append([_cx, _cy, _now, 1])
                    # Diagnostic: dump the crop the VLM actually looked at so
                    # we can eyeball model-blindness vs bad-crop-framing.
                    # Enable with SAVE_REJECTED_CROPS=1 in .env.
                    if _save_rejected_crops and crop_bytes:
                        try:
                            _save_reject_crop(crop_bytes, tid, bbox, _bw, _bh,
                                              result.get("description", ""))
                        except Exception:
                            logger.exception("Failed to save rejected crop for track=%d", tid)
                    # Alert-on-VLM-reject: when VLM says "no wildlife"
                    # but MOG + baseline-diff already agreed something
                    # meaningful is there, fire an 'other' alert anyway
                    # so the operator sees + labels it. **Fires per
                    # motion event by default (VLM_REJECT_ALERT_MIN_AREA_PX=0)**
                    # — silent misses are worse than misclassifications
                    # (human can relabel a wrong species, can't recover
                    # a dropped alert). Set the env to a positive px area
                    # to gate on bbox size if the alert volume becomes
                    # a problem.
                    #
                    # Insect classification excluded (species='insect' is
                    # count-only by design). The upstream gates
                    # (MIN_MOTION_BBOX_PX + baseline diff threshold +
                    # insect brightness pre-filter) still control the
                    # firing floor.
                    _reject_alert_min_area = int(os.getenv("VLM_REJECT_ALERT_MIN_AREA_PX", "0"))
                    _rejected_species = str(result.get("species", "")).lower()
                    _bbox_area = _bw * _bh
                    if (
                        _rejected_species != "insect"
                        and _bbox_area >= _reject_alert_min_area
                    ):
                        logger.info(
                            "VLM-reject override: track=%d bbox=%dx%d area=%d — firing 'other' for human review",
                            tid, _bw, _bh, _bbox_area,
                        )
                        _override_result = {
                            "wildlife_detected": True,
                            "species": "other",
                            "is_rodent": False,
                            "confidence": 0.5,
                            # bypass_min_confidence: 0.5 is intentional
                            # "fire for human review" — without this the
                            # notifier's min_conf gate silently drops the
                            # alert (only snapshot lands, no HA webhook,
                            # no DB row, no ALERT log line).
                            "bypass_min_confidence": True,
                            # bypass_cooldown: overrides are per-event
                            # labeling triggers, not operator alerts —
                            # every event should reach the human. Default
                            # 120s cooldown was dropping 12/14 overrides
                            # in sandbox on the 2341 cat clip.
                            "bypass_cooldown": True,
                            "description": (
                                f"[VLM-REJECT-OVERRIDE] VLM said {_rejected_species!r} "
                                f"(conf={result.get('confidence', 0.0):.2f}); MOG + baseline "
                                f"agreed motion is here (bbox {_bw}x{_bh}={_bbox_area}px "
                                f"mean={_b_mean:.0f} max={_b_max:.0f} AR={_b_ar:.2f}). "
                                f"Filing as 'other' for human review. Original: "
                                f"{result.get('description', '')[:120]}"
                            ),
                        }
                        snap_path = notifier.send("other", _override_result, snap_fr, bbox, yolo_conf=yolo_conf)
                        _snap_ref = None
                        if snap_path:
                            try:
                                _snap_ref = str(snap_path.relative_to(snap_path.parent.parent)).replace('\\', '/')
                            except Exception:
                                _snap_ref = snap_path.name
                        _preview_stats.record_alert(
                            species="other",
                            confidence=0.5,
                            description=_override_result["description"],
                            snapshot=_snap_ref,
                            track_id=tid,
                            yolo_conf=yolo_conf,
                        )
                    continue

                # Positive rodent — fire alert + slew secondary camera.
                # Append brightness signature so the alert card in the UI
                # shows the same features we log in DECISION lines. Enables
                # visual-feature labeling analysis across all alerts (not
                # just overrides).
                _orig_desc = result.get("description", "") or ""
                result = dict(result)  # avoid mutating the VLM's cached dict
                result["description"] = (
                    f"{_orig_desc} [bbox {_bw}x{_bh}={_bw*_bh}px "
                    f"mean={_b_mean:.0f} max={_b_max:.0f} AR={_b_ar:.2f}]"
                )
                snap_path = notifier.send("rodent", result, snap_fr, bbox, yolo_conf=yolo_conf)
                sh, sw = snap_fr.shape[:2]
                maybe_slew(bbox=bbox, event_key=("rodent", tid),
                           frame_width=sw, frame_height=sh)
                # Self-slew: for cameras that own their own PTZ (backyard),
                # pan to the zone-matched preset. No-op when SELF_SLEW_ENABLED
                # is false or the per-camera config block is disabled.
                maybe_self_slew(bbox=bbox, event_key=("rodent", tid),
                                frame_width=sw, frame_height=sh)
                # Flag the track for a red bbox flash on the preview.
                alert_ttl[tid] = ALERT_FLASH_FRAMES
                # snapshot field stores the RELATIVE path from snapshots/ so
                # /snapshots/<subpath> maps correctly. e.g. '2026-07-17/rodent_...jpg'.
                _snap_ref = None
                if snap_path:
                    try:
                        _snap_ref = str(snap_path.relative_to(snap_path.parent.parent)).replace('\\', '/')
                    except Exception:
                        _snap_ref = snap_path.name
                _preview_stats.record_alert(
                    species=str(result.get("species", "unknown")),
                    confidence=float(result.get("confidence", 0.0)),
                    description=str(result.get("description", ""))[:200],
                    snapshot=_snap_ref,
                    track_id=int(tid),
                    yolo_conf=float(yolo_conf) if yolo_conf is not None else None,
                )

            # Refresh the alert_ids set from the TTL map so the preview keeps the
            # red box visible for a few frames after the alert fires.
            alert_ids = {tid for tid, ttl in alert_ttl.items() if ttl > 0}
            alert_ttl = {tid: ttl - 1 for tid, ttl in alert_ttl.items() if ttl > 1}

            # ── Refresh decoded baseline (cheap when version unchanged) ─────
            # Auto-picks day or night baseline based on current frame brightness.
            _baseline_meta = _baseline_holder.snapshot() if _baseline_holder else None
            if _baseline_holder is not None:
                # Encode the current frame as JPEG (cheap) so the baseline holder
                # can decide day vs night from its brightness — we only need the
                # mean grayscale, so use a low-quality encode.
                _ok_bmode, _bmode_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 40])
                _current_jpeg_for_mode = _bmode_buf.tobytes() if _ok_bmode else b""
                _b_bytes, _b_ver, _b_mode = _baseline_holder.snapshot_bytes(
                    mode="auto", current_frame_jpeg=_current_jpeg_for_mode,
                )
                _cache_key = (_b_ver, _b_mode)
                if _cache_key != _baseline_cache[0]:
                    _baseline_cache = (_cache_key, _decode_baseline(_b_bytes, det_w, det_h))
                    logger.info("Baseline switched to %s slot (v=%d)", _b_mode, _b_ver)

            # Compute per-frame daytime flag ONCE, before the for-det loop.
            # Previously assigned inside the loop after the YOLO fast-path
            # read it — the first daytime iteration after restart hit an
            # UnboundLocalError (issue #30). Reads at
            # `if det.track_id < 1000 and not _is_daytime` still work
            # correctly; the guard now sees a consistent value all frame.
            # None when baseline isn't loaded yet (early startup); the
            # `not None` truthiness check downstream treats None as
            # falsy, which matches the pre-fix "night" default.
            _is_daytime = _baseline_cache[0][1] == "day" if _baseline_cache[1] is not None else None

            # ── Submit new VLM jobs for zone detections ─────────────────────
            for det in zone_dets:
                if det.track_id in vlm_jobs:
                    continue
                now = time.time()
                if now - last_vlm_ts.get(det.track_id, 0.0) < VLM_INTERVAL_S:
                    continue

                # ── YOLO fast-path — DAYTIME ONLY ────────────────────────────
                # Two-detector routing by time-of-day:
                #   - DAY: YOLO on COCO catches cat/dog/bird directly. Skip VLM.
                #   - NIGHT: rats via VLM eyeshine cascade (COCO has no rodent
                #     class, so YOLO can't help). Silently drop any YOLO hits at
                #     night — the MOG2 cascade is the sole nighttime signal.
                # YOLO track_ids are < 1000 by convention; MOG2 supplements are
                # >= 1000.
                if det.track_id < 1000 and not _is_daytime:
                    # Silently drop nighttime YOLO detections — the cascade owns nights.
                    last_vlm_ts[det.track_id] = now
                    continue
                if det.track_id < 1000:
                    last_vlm_ts[det.track_id] = now
                    # YOLO fast-path fires for ANY COCO class it detected — but
                    # only some are wildlife. Person / vehicle / furniture classes
                    # are always false-alert territory (property owner walking
                    # through, parked car). Hard-drop those before they become
                    # alerts; only pass known-wildlife classes through.
                    _WILDLIFE_COCO = {"cat", "dog", "bird", "horse", "sheep",
                                       "cow", "elephant", "bear", "zebra", "giraffe"}
                    if det.class_name not in _WILDLIFE_COCO:
                        logger.debug("YOLO drop: non-wildlife class '%s' track=%d",
                                     det.class_name, det.track_id)
                        continue
                    # Cat/bird sanity check: overhead + IR silhouettes of
                    # cats commonly trip YOLO's 'bird' class at 0.5-0.7
                    # confidence (observed on backyard 23:40-23:41). When
                    # YOLO says a low-confidence bird, DON'T take the
                    # fast-path — fall through to MOG/VLM so the VLM has a
                    # chance to reclassify (usually cat when the silhouette
                    # is cat-sized). Env-tunable via
                    # YOLO_FASTPATH_MIN_CONF_BIRD (default 0.85).
                    _bird_min_conf = float(os.getenv("YOLO_FASTPATH_MIN_CONF_BIRD", "0.85"))
                    if det.class_name == "bird" and det.confidence < _bird_min_conf:
                        logger.info(
                            "YOLO bird-conf gate: track=%d conf=%.2f < %.2f → fall through to VLM",
                            det.track_id, det.confidence, _bird_min_conf,
                        )
                        # Don't `continue` — we want the normal cascade to
                        # handle this track. But this is inside the YOLO
                        # fast-path branch; drop THROUGH to the MOG path
                        # by returning early from the branch. Easiest: just
                        # skip the alert-fire + slew for this loop iter.
                        continue
                    # Compute brightness signature for YOLO alerts too so
                    # every alert card carries the same feature triplet.
                    _ys = _bbox_signature(frame, det.bbox)
                    _yolo_result = {
                        "wildlife_detected": True,
                        "species":           det.class_name,
                        "is_rodent":         False,
                        "confidence":        float(det.confidence),
                        "description": (
                            f"YOLO/COCO detection: {det.class_name} conf={det.confidence:.2f} "
                            f"[bbox {_ys.w}x{_ys.h}={_ys.area}px mean={_ys.mean:.0f} max={_ys.max:.0f} AR={_ys.aspect_ratio:.2f}]"
                        ),
                    }
                    logger.info("YOLO fast-path: track=%d species=%s conf=%.2f bbox=%s",
                                det.track_id, det.class_name, det.confidence, det.bbox)
                    snap_path = notifier.send(det.class_name, _yolo_result, frame,
                                              det.bbox, yolo_conf=det.confidence)
                    sh, sw = frame.shape[:2]
                    maybe_slew(bbox=det.bbox, event_key=(det.class_name, det.track_id),
                               frame_width=sw, frame_height=sh)
                    maybe_self_slew(bbox=det.bbox, event_key=(det.class_name, det.track_id),
                                    frame_width=sw, frame_height=sh)
                    alert_ttl[det.track_id] = ALERT_FLASH_FRAMES
                    _snap_ref = None
                    if snap_path:
                        try:
                            _snap_ref = str(snap_path.relative_to(snap_path.parent.parent)).replace('\\', '/')
                        except Exception:
                            _snap_ref = snap_path.name
                    _preview_stats.record_alert(
                        species=det.class_name,
                        confidence=float(det.confidence),
                        description=_yolo_result["description"],
                        snapshot=_snap_ref,
                        track_id=int(det.track_id),
                        yolo_conf=float(det.confidence),
                    )
                    continue

                # Baseline pixel-diff pre-filter: skip VLM if the pixels inside
                # this motion bbox barely changed vs the empty reference. Cheap,
                # kills FPs from lighting jitter / IR grain / static objects.
                # OSD masks are zeroed in both frames before diff so timestamp
                # churn doesn't inflate the score.
                _baseline_np = _baseline_cache[1]
                if _baseline_np is None:
                    # Load-bearing signal is silent — surface it prominently.
                    logger.info("baseline pre-filter: BASELINE NOT LOADED — VLM will run on every zone motion. "
                                "Check data/baseline_{day,night}.jpg and baseline holder init.")
                else:
                    _score = _diff_score(_baseline_np, frame, det.bbox, osd_masks=osd_masks)
                    # Pick threshold based on active baseline mode — shadows during
                    # daytime need a higher bar than IR-clean nighttime.
                    _threshold = (
                        _baseline_diff_threshold_day
                        if _baseline_cache[0][1] == "day"
                        else _baseline_diff_threshold_night
                    )
                    logger.info("baseline diff: track=%d bbox=%s diff=%.3f threshold=%.3f (%s) → %s",
                                det.track_id, det.bbox, _score, _threshold,
                                _baseline_cache[0][1],
                                "SKIP VLM" if _score < _threshold else "run VLM")
                    if _score < _threshold:
                        _preview_stats.record_baseline_filtered()
                        # Still update last_vlm_ts so we don't retry every frame.
                        last_vlm_ts[det.track_id] = now
                        continue

                # Insect pre-filter (physical hard rail) — mammal fur absorbs
                # IR/light, moth wings reflect it. A bbox that looks like a
                # moth is physically-can't-be-a-mammal → classify as insect,
                # skip VLM. Deterministic, faster than a VLM call, immune to
                # the small-VLM failure mode where qwen hallucinates rodent
                # anatomy on moth crops. Runs regardless of day/night flag:
                # rooftop under street/courtyard lighting stays in "day"
                # baseline mode even at 2 AM local, so gating on mode==night
                # misses the majority of overnight moth activity.
                #
                # Three orthogonal signatures, ANY one triggers insect:
                #   (a) mean_brightness   — whole crop is IR-hot (near-white
                #       moth against near-white ground)
                #   (b) max_brightness    — a bright streak on dark ground
                #       averages down but has 255-pixel peaks (the failure
                #       mode of mean-only: yard IR at 00:55 8/10)
                #   (c) elongation ratio  — thin fuzzy line = moth wing
                #       blur; rodents in overhead crops are ~square
                # Size guard: also require bbox area < INSECT_FILTER_MAX_AREA_PX.
                # Moths are physically small; a large bright blob is a mammal
                # with pale fur or IR reflection off the ground under it,
                # NOT a moth. Without this guard, cats/raccoons at overhead
                # angle get silently rejected (see Aug 8 02:23 cat incident).
                _is = _bbox_signature(frame, det.bbox)
                if _is.area > 0 and _is.area < _INSECT_FILTER_MAX_AREA_PX:
                    _mean_hit = _is.mean >= _NIGHT_INSECT_BRIGHTNESS_MIN
                    _max_hit = _is.max >= _NIGHT_INSECT_MAX_BRIGHTNESS_MIN
                    _elong_hit = (
                        _is.aspect_ratio >= _NIGHT_INSECT_ELONGATION_MIN
                        and _is.max >= _NIGHT_INSECT_MAX_BRIGHTNESS_MIN
                    )
                    if _mean_hit or _max_hit or _elong_hit:
                        _trigger = "mean" if _mean_hit else ("max" if _max_hit else "elong")
                        logger.info(
                            "Insect pre-filter: track=%d bbox=%s area=%d mean=%.0f max=%.0f AR=%.2f trigger=%s mode=%s → classify as insect, skip VLM",
                            det.track_id, det.bbox, _is.area,
                            _is.mean, _is.max, _is.aspect_ratio, _trigger,
                            _baseline_cache[0][1] if _baseline_np is not None else "?",
                        )
                        _preview_stats.record_vlm_insect()
                        last_vlm_ts[det.track_id] = now
                        continue

                last_vlm_ts[det.track_id] = now
                # For camouflaged targets (raccoon in brush, rodent behind
                # foliage) the raw frame carries no spatial signal — the
                # animal is only visible via inter-frame motion. Overlay the
                # motion contour on the crop so the temporal signal enters
                # the VLM's spatial input. Opt-in via VLM_MOTION_OVERLAY=1.
                if _motion_overlay_enabled and getattr(det, "contour", None) is not None:
                    crop = _crop_wide_bytes_with_motion_overlay(frame, det.bbox, det.contour)
                else:
                    crop = _crop_wide_bytes(frame, det.bbox, osd_masks=osd_masks)
                if not crop:
                    continue
                # VLM input selection, in priority order:
                #   1. Temporal (3+ recent frames within the window) — motion
                #      signature across time; analyzer switches to temporal
                #      prompt. Biggest lever for small targets on textured
                #      backgrounds; motion beats single-frame silhouette.
                #   2. Baseline compare (baseline + current) — what changed;
                #      static motion context from a paired reference.
                #   3. Single frame — no reference, VLM classifies the crop
                #      standalone.
                # Baseline crop is NEVER annotated — it's the reference for "what
                # was here before"; overlay would create a spurious diff.
                # Both frames get the same OSD blackout so timestamp burn-in
                # doesn't become the dominant "what changed" signal for VLM.
                _vlm_input = [crop]
                if _temporal_enabled and len(_recent_frames) >= 3:
                    _cutoff = now - _temporal_window_s
                    _recent_crops = [
                        _crop_wide_bytes(f, det.bbox, osd_masks=osd_masks)
                        for ts, f in _recent_frames
                        if ts >= _cutoff
                    ]
                    _recent_crops = [c for c in _recent_crops if c]
                    if len(_recent_crops) >= 3:
                        _vlm_input = _recent_crops
                elif _baseline_np is not None:
                    _baseline_crop = _crop_wide_from_ndarray(_baseline_np, det.bbox, osd_masks=osd_masks)
                    if _baseline_crop:
                        _vlm_input = [_baseline_crop, crop]   # order: baseline first, current second
                # Pass day/night context so analyzer can apply time-of-day rules
                # (e.g. daytime rodent skepticism gate). `_is_daytime` is
                # hoisted to per-frame above the for-det loop — see comment
                # near baseline-cache refresh + issue #30.
                # Optional daytime VLM bypass — rats are nocturnal; skip Claude
                # cost during the sun-and-shadow hours. Set VLM_SKIP_DAYTIME=1
                # in .env to enable. Motion + zone still tracked; only VLM stage
                # gates. Cat/dog/squirrel daytime detection sacrificed.
                if _is_daytime and os.getenv("VLM_SKIP_DAYTIME", "0") == "1":
                    logger.debug("VLM_SKIP_DAYTIME active — skipping VLM for track=%d", det.track_id)
                    last_vlm_ts[det.track_id] = now
                    continue
                # Ingress load-shedding: cap in-flight jobs, drop oldest
                # unstarted future when the queue is full. Prefer freshness
                # over completeness — an alert 30s late on a rodent that
                # already left is worse than a missed detection during a
                # motion storm we'd have false-positived anyway.
                while len(vlm_jobs) >= _VLM_MAX_INFLIGHT:
                    _oldest_tid = min(vlm_jobs.keys(), key=lambda t: vlm_jobs[t][5])
                    _old_fut = vlm_jobs[_oldest_tid][0]
                    if _old_fut.cancel():
                        del vlm_jobs[_oldest_tid]
                        logger.debug("VLM load-shed: cancelled oldest pending track=%d", _oldest_tid)
                    else:
                        # Already running — can't cancel. Skip THIS submission
                        # rather than pile on more.
                        logger.debug("VLM load-shed: queue full and oldest running; skipping track=%d",
                                     det.track_id)
                        break
                else:
                    fut = vlm_pool.submit(vlm.analyze, _vlm_input, _is_daytime)
                    # Stash `crop` (the current-frame JPEG bytes actually sent to VLM)
                    # so a rejected verdict can save it for eyeballing.
                    vlm_jobs[det.track_id] = (fut, frame.copy(), det.bbox, det.confidence, crop, time.time())
                    _preview_stats.record_vlm_call()

            # Evict gone tracks
            for tid in list(last_vlm_ts.keys()):
                if tid not in active_ids and tid not in vlm_jobs:
                    del last_vlm_ts[tid]

            # ── Push annotated + raw frames to the preview server ───────────
            # Rate-limit to every Nth frame to keep JPEG encode cost down.
            _frame_count += 1
            if _frame_count % PREVIEW_EVERY_N == 0:
                # Raw first — baseline capture endpoint pulls from this holder.
                ok_raw, buf_raw = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok_raw:
                    _publish_raw_frame(buf_raw.tobytes())
                annotated = _annotate(frame, all_dets, zone_dets, alert_ids, zone_polygon)
                ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok:
                    _publish_preview_frame(buf.tobytes())

    except KeyboardInterrupt:
        logger.info("Shutting down…")
    finally:
        vlm_pool.shutdown(wait=False)
        stream.stop()
