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


def main() -> None:
    ap = argparse.ArgumentParser(description="Headless wildlife detector")
    ap.add_argument("--video", help="Local video file to replay instead of RTSP")
    ap.add_argument("--rtsp",  help="RTSP URL override (defaults to RTSP_URL env)")
    args = ap.parse_args()

    from src import pipeline
    pipeline.run(stream_url=args.rtsp, video_path=args.video)


if __name__ == "__main__":
    main()
