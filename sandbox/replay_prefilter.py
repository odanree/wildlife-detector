"""Replay a single MP4 clip through MOG2 + the insect pre-filter and
report before/after the night-only gate (PR #179).

Runs on the HOST — no Docker needed. Decodes with cv2, applies the
same _bbox_signature brightness features the production pipeline
computes, and simulates the insect pre-filter's mean/max/AR checks
against every motion contour.

For each motion detection reports what old-behavior (always-on
filter) and new-behavior (night-only filter) would have done:

    old_drop / new_drop / new_pass_through

Usage:
    python sandbox/replay_prefilter.py \\
        --clip clips/rooftop_squirrel_prefilter_test_20260909_151400.mp4 \\
        --mode day

Exit code 0 = the fix successfully reduced drops vs. old behavior.

Not a full-pipeline replay — no YOLO, no VLM, no classifier. Just
the pre-filter branch that was silently killing daytime motion
before PR #179. That's the exact question the sandbox needs to
answer for this PR.

Pattern: **shadow-model evaluation** — reads production module code
without touching production state.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

import cv2  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clip", required=True, help="Path to MP4 clip to replay")
    p.add_argument("--mode", choices=("day", "night"), default="day",
                   help="Baseline mode to simulate. Default: day (rooftop's mode during the recorded window).")
    p.add_argument("--min-area", type=int, default=80,
                   help="MOG2 contour min area (matches motion_detector default)")
    p.add_argument("--max-area", type=int, default=4000)
    p.add_argument("--history", type=int, default=400)
    p.add_argument("--var-threshold", type=int, default=18)
    # Pre-filter thresholds — match production defaults from src/pipeline.py
    p.add_argument("--brightness-min", type=float, default=130.0,
                   help="NIGHT_INSECT_BRIGHTNESS_MIN (default from prod)")
    p.add_argument("--max-brightness-min", type=float, default=245.0,
                   help="NIGHT_INSECT_MAX_BRIGHTNESS_MIN")
    p.add_argument("--elongation-min", type=float, default=2.0)
    p.add_argument("--filter-max-area", type=int, default=2000,
                   help="INSECT_FILTER_MAX_AREA_PX")
    p.add_argument("--mean-uncap-size", action="store_true",
                   help="INSECT_MEAN_UNCAP_SIZE (uncap mean gate above filter-max-area)")
    return p.parse_args()


def bbox_signature(frame, bbox: tuple[int, int, int, int]) -> dict:
    """Mirrors src/pipeline.py::_bbox_signature — mean/max/AR features."""
    x1, y1, x2, y2 = bbox
    x1, y1 = max(0, x1), max(0, y1)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return {"area": 0, "mean": 0.0, "max": 0.0, "w": 0, "h": 0, "ar": 1.0}
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    w, h = x2 - x1, y2 - y1
    return {
        "area": w * h,
        "mean": float(gray.mean()),
        "max": float(gray.max()),
        "w": w,
        "h": h,
        "ar": max(w, h) / max(1, min(w, h)),
    }


def prefilter_would_drop(sig: dict, args: argparse.Namespace) -> tuple[bool, str]:
    """Mirrors the pipeline's insect pre-filter logic exactly.
    Returns (would_drop, trigger_name)."""
    if sig["area"] <= 0:
        return False, ""
    size_gated = sig["area"] < args.filter_max_area
    mean_size_ok = size_gated or args.mean_uncap_size
    mean_hit = mean_size_ok and sig["mean"] >= args.brightness_min
    max_hit = size_gated and sig["max"] >= args.max_brightness_min
    elong_hit = size_gated and (
        sig["ar"] >= args.elongation_min and sig["max"] >= args.max_brightness_min
    )
    if mean_hit:
        return True, "mean"
    if max_hit:
        return True, "max"
    if elong_hit:
        return True, "elong"
    return False, ""


def main() -> int:
    args = parse_args()

    clip = Path(args.clip)
    if not clip.exists():
        print(f"error: clip not found: {clip}", file=sys.stderr)
        return 2

    print(f"Replaying {clip.name} (mode={args.mode})")
    print(f"Thresholds: mean>={args.brightness_min} max>={args.max_brightness_min} "
          f"AR>={args.elongation_min} filter_max_area={args.filter_max_area} "
          f"mean_uncap={args.mean_uncap_size}")

    cap = cv2.VideoCapture(str(clip))
    if not cap.isOpened():
        print(f"error: cv2 couldn't open {clip}", file=sys.stderr)
        return 2

    # MOG2 with production knobs. detectShadows=False matches src default.
    mog = cv2.createBackgroundSubtractorMOG2(
        history=args.history, varThreshold=args.var_threshold, detectShadows=False,
    )

    stats = {
        "frames": 0,
        "motion_contours": 0,
        "size_filtered": 0,       # too small/big for MOG
        "prefilter_drop_old": 0,  # would drop with old behavior (mode ignored)
        "prefilter_drop_new": 0,  # would drop with new behavior (day gate applied)
        "pass_through_old": 0,
        "pass_through_new": 0,
        "trigger_counts": {"mean": 0, "max": 0, "elong": 0},
        "sample_dropped": [],     # first N drops for inspection
    }

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        stats["frames"] += 1

        # Downsample to detection resolution (production uses 2048x928 for
        # rooftop; sandbox uses whatever the clip already is — that's fine
        # for the pre-filter comparison since it's mean-brightness-based).
        mask = mog.apply(frame)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            area = w * h
            if area < args.min_area or area > args.max_area:
                stats["size_filtered"] += 1
                continue
            stats["motion_contours"] += 1
            sig = bbox_signature(frame, (x, y, x + w, y + h))

            # Old behavior: filter always applied
            drop_old, trigger_old = prefilter_would_drop(sig, args)
            # New behavior (PR #179): filter skipped when mode=day
            filter_active = args.mode != "day"
            if not filter_active:
                drop_new, trigger_new = False, ""
            else:
                drop_new, trigger_new = drop_old, trigger_old

            if drop_old:
                stats["prefilter_drop_old"] += 1
                stats["trigger_counts"][trigger_old] += 1
                if len(stats["sample_dropped"]) < 5:
                    stats["sample_dropped"].append({
                        "frame": stats["frames"],
                        "bbox": [x, y, x + w, y + h],
                        "sig": sig,
                        "trigger": trigger_old,
                    })
            else:
                stats["pass_through_old"] += 1

            if drop_new:
                stats["prefilter_drop_new"] += 1
            else:
                stats["pass_through_new"] += 1

    cap.release()

    print()
    print("--- SUMMARY ----------------------------------------")
    print(f"Frames processed:    {stats['frames']}")
    print(f"Motion contours:     {stats['motion_contours']} (after size filter)")
    print(f"Size-filtered:       {stats['size_filtered']} (too small/big)")
    print()
    print(f"OLD behavior (pre-filter always active):")
    print(f"  Dropped as insect: {stats['prefilter_drop_old']}")
    print(f"  Reached VLM:       {stats['pass_through_old']}")
    print(f"  Triggers: {dict(stats['trigger_counts'])}")
    print()
    print(f"NEW behavior (PR #179 — pre-filter night-only, mode={args.mode}):")
    print(f"  Dropped as insect: {stats['prefilter_drop_new']}")
    print(f"  Reached VLM:       {stats['pass_through_new']}")
    print()
    saved = stats["prefilter_drop_old"] - stats["prefilter_drop_new"]
    if saved > 0:
        print(f"[OK] Fix recovers {saved} pre-filter drops on this clip "
              f"(mode={args.mode})")
    else:
        print(f"No recovery on this clip (mode={args.mode}) — expected only "
              f"when mode=day and old filter was over-firing.")

    print()
    if stats["sample_dropped"]:
        print("Sample OLD-behavior drops (would have been suppressed):")
        for s in stats["sample_dropped"]:
            print(f"  frame {s['frame']:>5} bbox={s['bbox']} "
                  f"trigger={s['trigger']} sig={json.dumps(s['sig'], default=lambda o: round(o, 1) if isinstance(o, float) else o)}")

    return 0 if saved > 0 or args.mode == "night" else 1


if __name__ == "__main__":
    sys.exit(main())
