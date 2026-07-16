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
from concurrent.futures import Future, ThreadPoolExecutor

import cv2
import numpy as np
import yaml
from dotenv import load_dotenv

load_dotenv()

from src.alerts.notifier import Notifier
from src.detection.motion_detector import MotionDetector
from src.detection.object_detector import Detection, ObjectDetector
from src.detection.zone_filter import ZoneFilter
from src.stream.rtsp_handler import RTSPHandler
from src.stream.slew import maybe_slew
from src.stream.video_file_handler import VideoFileHandler
from src.vlm.analyzer import VLMAnalyzer

logger = logging.getLogger(__name__)


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _crop_wide_bytes(frame: np.ndarray, bbox: tuple[int, int, int, int]) -> bytes:
    """Close-up crop around the detection — VLM input."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = max(1, x2 - x1), max(1, y2 - y1)
    pad_x = max(bw * 2, 80)
    pad_y = max(bh * 2, 60)
    crop = frame[max(0, y1 - pad_y): min(h, y2 + pad_y),
                 max(0, x1 - pad_x): min(w, x2 + pad_x)]
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else b""


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


def run(stream_url: str | None = None, video_path: str | None = None) -> None:
    cfg_det = _load_yaml("config/detection.yaml")
    cfg_alerts = _load_yaml("config/alerts.yaml")

    det_cfg = cfg_det["detector"]
    mot_cfg = cfg_det.get("motion_detector", {})
    zone_key = cfg_det.get("zone_key", "yard_zone")
    zone_polygon = cfg_det["zones"][zone_key]["polygon"]

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
    )

    motion = MotionDetector(
        history=mot_cfg.get("history", 400),
        var_threshold=float(mot_cfg.get("var_threshold", 18)),
        min_area=int(mot_cfg.get("min_area", 80)),
        max_area=int(mot_cfg.get("max_area", 4000)),
        edge_margin=int(mot_cfg.get("edge_margin", 20)),
        seam_x=int(mot_cfg.get("seam_x", 0)),
        seam_margin=int(mot_cfg.get("seam_margin", 0)),
    )

    zone_filter = ZoneFilter(zones={zone_key: zone_polygon})

    vlm = VLMAnalyzer(
        backend=os.getenv("VLM_BACKEND", "claude"),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llava:7b-v1.6-mistral-q4_K_M"),
    )

    ha_base = os.getenv("HA_WEBHOOK_URL", "").rsplit("/api/", 1)[0]
    notifier = Notifier(
        config=cfg_alerts["alerts"],
        ha_webhook_base=ha_base,
        ha_token=os.getenv("HA_TOKEN", ""),
    )

    vlm_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vlm")
    vlm_jobs: dict[int, tuple[Future, np.ndarray, tuple, float]] = {}
    last_vlm_ts: dict[int, float] = {}
    VLM_INTERVAL_S = float(os.getenv("VLM_INTERVAL_S", "2.0"))

    stream.start()
    logger.info("Pipeline running — press Ctrl-C to stop")

    try:
        while True:
            frame = stream.get_frame()
            if frame is None:
                continue

            fh, fw = frame.shape[:2]
            if (fw, fh) != (det_w, det_h):
                frame = cv2.resize(frame, (det_w, det_h))

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
            all_dets = all_dets + motion_dets

            zone_dets = zone_filter.filter(all_dets, zone_key)
            active_ids = {d.track_id for d in zone_dets}

            # ── Harvest completed VLM jobs ──────────────────────────────────
            for tid in list(vlm_jobs.keys()):
                fut, snap_fr, bbox, yolo_conf = vlm_jobs[tid]
                if not fut.done():
                    continue
                del vlm_jobs[tid]
                try:
                    result = fut.result()
                except Exception:
                    logger.exception("VLM job for track=%d raised", tid)
                    continue

                logger.info("DECISION track=%d species=%s rodent=%s conf=%.2f",
                            tid, result.get("species"), result.get("is_rodent"),
                            result.get("confidence", 0.0))

                if not result.get("wildlife_detected", False):
                    continue

                # Positive rodent — fire alert + slew secondary camera
                notifier.send("rodent", result, snap_fr, bbox, yolo_conf=yolo_conf)
                sh, sw = snap_fr.shape[:2]
                maybe_slew(bbox=bbox, event_key=("rodent", tid),
                           frame_width=sw, frame_height=sh)

            # ── Submit new VLM jobs for zone detections ─────────────────────
            for det in zone_dets:
                if det.track_id in vlm_jobs:
                    continue
                now = time.time()
                if now - last_vlm_ts.get(det.track_id, 0.0) < VLM_INTERVAL_S:
                    continue
                last_vlm_ts[det.track_id] = now
                crop = _crop_wide_bytes(frame, det.bbox)
                if not crop:
                    continue
                fut = vlm_pool.submit(vlm.analyze, [crop])
                vlm_jobs[det.track_id] = (fut, frame.copy(), det.bbox, det.confidence)

            # Evict gone tracks
            for tid in list(last_vlm_ts.keys()):
                if tid not in active_ids and tid not in vlm_jobs:
                    del last_vlm_ts[tid]

    except KeyboardInterrupt:
        logger.info("Shutting down…")
    finally:
        vlm_pool.shutdown(wait=False)
        stream.stop()
