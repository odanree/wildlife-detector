"""Replay a single MP4 clip through MotionDetector + ZoneFilter with a
configurable knob-set, and report whether an alert would have fired.

Runs on the HOST. Imports from src/ via PYTHONPATH manipulation.

Usage:
    python sandbox/replay.py --clip clips/rooftop_tp_155174.mp4
    python sandbox/replay.py --clip <path> --min-track-age 4 --max-velocity 30

Every knob is a CLI arg (with defaults matching current production).
Motion detector's module-level constants are monkey-patched between
runs since they're frozen at import — see `_apply_config()`.

Output: JSON to stdout with per-frame detection counts + surviving
detections after zone-filter. Exit code 0 = detection fired
(alert would have happened), 1 = no detection (silent).

Pattern: **shadow-model evaluation** — reads production module code
without touching production state; every knob variation is a fresh
`MotionDetector` instance so background-subtractor state doesn't
leak between runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Path shim so `from src.detection...` works when running from anywhere.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

import cv2  # noqa: E402
import yaml  # noqa: E402

from src.detection import motion_detector as md  # noqa: E402
from src.detection.motion_detector import MotionDetector  # noqa: E402
from src.detection.zone_filter import ZoneFilter  # noqa: E402


def _apply_config(min_track_age: int, max_velocity: int) -> None:
    """Monkey-patch motion_detector module constants. They're read once
    at import from env vars, so between-run tweaks require this."""
    md._MIN_TRACK_AGE = min_track_age
    md._MAX_VELOCITY_PX = max_velocity


def _load_zone_polygon(camera: str, det_w: int, det_h: int) -> list[tuple[int, int]]:
    """Read the zone polygon for `camera` from config/detection.yaml,
    scale from normalized [0..1] coords to detection-frame pixels."""
    cfg_path = REPO_ROOT / "config" / "detection.yaml"
    with cfg_path.open() as f:
        cfg = yaml.safe_load(f)
    # Zone lookup: zones[<camera>_zone] mirrors production convention
    zone_key = f"{camera}_zone"
    zones = cfg.get("zones", {})
    if zone_key not in zones:
        raise SystemExit(f"No zone {zone_key!r} in {cfg_path}")
    norm = zones[zone_key]["polygon"]
    return [(int(x * det_w), int(y * det_h)) for x, y in norm]


def _infer_camera_from_clip(clip_path: Path) -> str:
    """Filename convention: <camera>_<verdict>_<id>.mp4 → parse the camera."""
    parts = clip_path.stem.split("_", 1)
    return parts[0] if parts else "rooftop"


def replay(
    clip_path: Path,
    *,
    min_track_age: int,
    max_velocity: int,
    min_area: int,
    max_area: int,
    var_threshold: float,
    history: int,
    edge_margin: int,
    use_zone: bool,
    min_bbox_px: int = 0,
) -> dict:
    """Run one clip through the pipeline with the given config. Returns
    a summary dict — see the JSON printed by main()."""
    _apply_config(min_track_age=min_track_age, max_velocity=max_velocity)

    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        raise SystemExit(f"cv2 failed to open {clip_path}")
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    camera = _infer_camera_from_clip(clip_path)
    zone_polygon = _load_zone_polygon(camera, frame_w, frame_h) if use_zone else None
    zone_filter = None
    if zone_polygon:
        zone_key = f"{camera}_zone"
        zone_filter = ZoneFilter(zones={zone_key: zone_polygon})

    motion = MotionDetector(
        history=history,
        var_threshold=var_threshold,
        min_area=min_area,
        max_area=max_area,
        edge_margin=edge_margin,
    )

    frame_count = 0
    dets_pre_bbox_filter = 0
    dets_pre_zone = 0    # raw detections that survived the bbox pre-filter
    dets_post_zone = 0   # after zone filter
    velocity_rej_total = 0
    persistence_rej_total = 0
    bbox_prefilter_rej = 0
    first_hit_frame = None

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_count += 1
        # NOTE: no color conversion — MotionDetector accepts either
        # single-channel or 3-channel and internally converts.
        dets = motion.detect(frame)
        dets_pre_bbox_filter += len(dets)

        # MIN_MOTION_BBOX_PX pre-filter — production applies this in
        # pipeline.py right after MotionDetector.detect. Requires bbox
        # width AND height ≥ threshold. Not part of MotionDetector
        # itself so we replicate here.
        if min_bbox_px > 0 and dets:
            filtered = [
                d for d in dets
                if (d.bbox[2] - d.bbox[0]) >= min_bbox_px
                and (d.bbox[3] - d.bbox[1]) >= min_bbox_px
            ]
            bbox_prefilter_rej += len(dets) - len(filtered)
            dets = filtered

        dets_pre_zone += len(dets)

        if zone_filter and dets:
            dets = zone_filter.filter(dets, f"{camera}_zone")
        dets_post_zone += len(dets)

        if dets and first_hit_frame is None:
            first_hit_frame = frame_count

        v_rej, p_rej = motion.pop_reject_counts()
        velocity_rej_total += v_rej
        persistence_rej_total += p_rej

    cap.release()

    return {
        "clip": str(clip_path.relative_to(REPO_ROOT)) if clip_path.is_absolute() else str(clip_path),
        "camera": camera,
        "frame_count": frame_count,
        "frame_size": [frame_w, frame_h],
        "config": {
            "min_track_age": min_track_age,
            "max_velocity": max_velocity,
            "min_area": min_area,
            "max_area": max_area,
            "var_threshold": var_threshold,
            "history": history,
            "edge_margin": edge_margin,
            "use_zone": use_zone,
            "min_bbox_px": min_bbox_px,
        },
        "detections_pre_bbox_filter": dets_pre_bbox_filter,
        "detections_pre_zone": dets_pre_zone,
        "detections_post_zone": dets_post_zone,
        "first_hit_frame": first_hit_frame,
        "velocity_rejects": velocity_rej_total,
        "persistence_rejects": persistence_rej_total,
        "bbox_prefilter_rejects": bbox_prefilter_rej,
        "would_fire_alert": dets_post_zone > 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clip", required=True, type=Path, help="path to MP4")
    ap.add_argument("--min-track-age", type=int, default=2,
                    help="MOTION_MIN_TRACK_AGE_FRAMES (default 2 = production)")
    ap.add_argument("--max-velocity", type=int, default=40,
                    help="MOTION_MAX_VELOCITY_PX_PER_FRAME (default 40)")
    ap.add_argument("--min-area", type=int, default=80,
                    help="MotionDetector min_area px² (default 80 = production)")
    ap.add_argument("--max-area", type=int, default=4000,
                    help="MotionDetector max_area px² (default 4000)")
    ap.add_argument("--var-threshold", type=float, default=18.0,
                    help="MOG2 varThreshold (default 18 = production)")
    ap.add_argument("--history", type=int, default=400,
                    help="MOG2 history frames (default 400)")
    ap.add_argument("--edge-margin", type=int, default=20)
    ap.add_argument("--min-bbox-px", type=int, default=30,
                    help="MIN_MOTION_BBOX_PX pre-filter — bbox W AND H both must be >= this "
                         "(default 30 = rooftop production; use 0 to disable)")
    ap.add_argument("--no-zone", action="store_true",
                    help="disable zone-filter (all detections pass)")
    args = ap.parse_args()

    if not args.clip.exists():
        raise SystemExit(f"clip not found: {args.clip}")

    result = replay(
        args.clip,
        min_track_age=args.min_track_age,
        max_velocity=args.max_velocity,
        min_area=args.min_area,
        max_area=args.max_area,
        var_threshold=args.var_threshold,
        history=args.history,
        edge_margin=args.edge_margin,
        use_zone=not args.no_zone,
        min_bbox_px=args.min_bbox_px,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["would_fire_alert"] else 1


if __name__ == "__main__":
    sys.exit(main())
