"""Detector service entrypoint (Phase 2 of ADR 002).

Runs the pipeline plus a minimal internal HTTP on 127.0.0.1:8101.
Does NOT serve any UI — that's the web sidecar's job.

Usage:
    python -m src.detector_service                # live RTSP
    python -m src.detector_service --video x.mp4  # replay

Compared to `src/main.py` (all-in-one) this entrypoint:
  - Does not start the user-facing preview server (no 0.0.0.0 bind)
  - Starts the loopback-only internal HTTP for the web sidecar to talk to
  - Otherwise same pipeline, same signal handling, same shutdown behavior

The web sidecar (`src/web_service.py`) can restart independently — the
detector will keep running through it.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            "logs/detector.log", encoding="utf-8",
            maxBytes=10 * 1024 * 1024, backupCount=5,
        ),
    ],
)


_shutdown = threading.Event()


def _signal_handler(signum, _frame):
    if _shutdown.is_set():
        logging.getLogger("detector_service").warning("Second Ctrl+C — force exit")
        os._exit(1)
    logging.getLogger("detector_service").info(
        "Signal %d — shutting down (Ctrl+C again to force)", signum,
    )
    _shutdown.set()


def main() -> None:
    ap = argparse.ArgumentParser(description="Wildlife detector (detection service)")
    ap.add_argument("--video", help="Local video file to replay")
    ap.add_argument("--rtsp",  help="RTSP URL override (defaults to RTSP_URL env)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    log = logging.getLogger("detector_service")
    log.info("Detector service starting — process id %d", os.getpid())

    # Start the loopback-only internal HTTP FIRST so pipeline's hot-reload
    # holders are wired before the pipeline runs its startup init.
    from src.web import detector_api
    detector_api.start_in_thread(
        host=os.getenv("DETECTOR_INTERNAL_HOST", "127.0.0.1"),
        port=int(os.getenv("DETECTOR_INTERNAL_PORT", "8101")),
    )

    from src import pipeline
    try:
        pipeline.run(
            stream_url=args.rtsp,
            video_path=args.video,
            shutdown_event=_shutdown,
        )
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — clean exit")
    finally:
        log.info("Detector service exiting — force-exit to release daemon threads")
        os._exit(0)


if __name__ == "__main__":
    main()
