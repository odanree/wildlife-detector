"""Probe the VLM against one or more still images from the rejected pile.

Uses the SAME VLMAnalyzer + prompt that runs in production, so what you see
here is exactly what the pipeline sees. Edit `src/vlm/analyzer.py` prompt,
re-run this script, iterate until the model correctly classifies your
target image.

Usage:
    python -m scripts.vlm_probe <image1> [image2 ...]
    python -m scripts.vlm_probe snapshots/rejected/2026-07-18/vlmreject_083433_track1480_24x22.jpg

Compare mode (baseline vs current, matches production two-image call):
    python -m scripts.vlm_probe --baseline data/baseline_night.jpg <image>

Env respected:
    VLM_BACKEND, OLLAMA_MODEL, OLLAMA_URL, ANTHROPIC_API_KEY (via analyzer)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Configure logging so hard-rail overrides print to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s  %(message)s",
)

from src.vlm.analyzer import VLMAnalyzer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe VLM on still images")
    ap.add_argument("images", nargs="+", help="Path(s) to JPEG crops")
    ap.add_argument("--baseline", help="Optional baseline JPEG for two-image compare mode")
    ap.add_argument("--daytime", choices=["day", "night", "auto"], default="night",
                    help="Time-of-day hint the analyzer receives (default: night)")
    ap.add_argument("--backend", choices=["claude", "ollama", "mock", "cascade"],
                    help="Force VLM backend, overriding .env / VLM_BACKEND env var")
    args = ap.parse_args()

    # Mirror pipeline's env-driven factory. --backend overrides VLM_BACKEND
    # (including 'cascade', which routes through the CascadeVLMAnalyzer).
    import os
    if args.backend:
        os.environ["VLM_BACKEND"] = args.backend
    from src.vlm.analyzer import build_vlm_analyzer_from_env
    vlm = build_vlm_analyzer_from_env()
    print(f"Backend: {getattr(vlm, '_backend', 'unknown')}")
    print(f"Model:   {getattr(vlm, 'model_name', 'unknown')}")
    print()

    baseline_bytes = None
    if args.baseline:
        baseline_bytes = Path(args.baseline).read_bytes()

    is_daytime = {"day": True, "night": False, "auto": None}[args.daytime]

    for img_path in args.images:
        p = Path(img_path)
        if not p.exists():
            print(f"SKIP {img_path} — not found")
            continue
        crop = p.read_bytes()
        frames = [baseline_bytes, crop] if baseline_bytes else [crop]

        print(f"===== {p.name} ({len(crop)} bytes) =====")
        t0 = time.perf_counter()
        try:
            result = vlm.analyze(frames, is_daytime)
        except Exception as e:
            print(f"ERROR: {e}")
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000

        print(f"latency: {elapsed_ms:.0f} ms")
        print(json.dumps(result, indent=2, ensure_ascii=False))

        # Verdict banner for quick scanning
        if result.get("wildlife_detected"):
            print(f"  ✅ DETECTED: {result.get('species')} conf={result.get('confidence')}")
        else:
            print(f"  ❌ REJECTED — reason: '{result.get('description', '')[:120]}'")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
