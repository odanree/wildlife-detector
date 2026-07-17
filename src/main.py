"""Entry point — headless wildlife detector.

Usage:
    python -m src.main                          # live RTSP from RTSP_URL env
    python -m src.main --video path/to/clip.mp4 # replay a local file

Environment (see .env.example for the full list):
    RTSP_URL              rtsp://user:pass@host/stream
    VIDEO_PATH            local file for replay mode (alternative to RTSP_URL)
    VLM_BACKEND           claude | ollama | mock
    ANTHROPIC_API_KEY     required for VLM_BACKEND=claude
    SLEW_ENABLED          true to actually move the secondary PTZ camera
    SECONDARY_CAMERA_ID   0-based PTZ_HOST_{n} / PTZ_CHANNEL_{n} slot
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
    """Set a shutdown event on Ctrl+C so the pipeline loop can exit cleanly.
    Also register os._exit(0) on the SECOND Ctrl+C to force-quit if the first
    one doesn't unblock threads (RTSP cv2.VideoCapture.read() can hang)."""
    if _shutdown.is_set():
        # Second signal — force exit, don't wait for graceful cleanup.
        logging.getLogger("main").warning("Second Ctrl+C — force exit")
        os._exit(1)
    logging.getLogger("main").info("Signal %d received — shutting down (Ctrl+C again to force)", signum)
    _shutdown.set()


def main() -> None:
    ap = argparse.ArgumentParser(description="Headless wildlife detector")
    ap.add_argument("--video", help="Local video file to replay instead of RTSP")
    ap.add_argument("--rtsp",  help="RTSP URL override (defaults to RTSP_URL env)")
    ap.add_argument("--preview", action="store_true", help="Also start the MJPEG preview server (overrides PREVIEW_ENABLED)")
    args = ap.parse_args()

    # Install signal handlers early. On Windows, SIGBREAK (Ctrl+Break) is also
    # honored — use if Ctrl+C is intercepted by the terminal.
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    preview_enabled = args.preview or os.getenv("PREVIEW_ENABLED", "false").lower() == "true"
    if preview_enabled:
        from src.web.preview import start_in_thread
        start_in_thread(
            host=os.getenv("PREVIEW_HOST", "0.0.0.0"),
            port=int(os.getenv("PREVIEW_PORT", "8000")),
        )

    from src import pipeline
    log = logging.getLogger("main")
    try:
        pipeline.run(stream_url=args.rtsp, video_path=args.video, shutdown_event=_shutdown)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — clean exit")
    finally:
        # Force-exit after pipeline.run() returns. Daemon threads (Flask/
        # werkzeug, VLM ThreadPoolExecutor workers stuck on httpx, RTSPHandler
        # cv2.VideoCapture.read()) sometimes keep the interpreter alive on
        # Windows despite the main thread being done. os._exit skips atexit
        # handlers + finalizers but kills the process reliably.
        log.info("Main returning — force-exit to release daemon threads")
        os._exit(0)


if __name__ == "__main__":
    main()
