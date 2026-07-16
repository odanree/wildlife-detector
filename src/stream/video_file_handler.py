"""Video file source — drop-in replacement for RTSPHandler during testing.

Forward: sequential cap.read() at native fps × speed.
Reverse: chunk-based — reads REVERSE_CHUNK frames forward into a buffer then
plays them backwards.  One I-frame decode per chunk, not per frame.

Usage: set VIDEO_PATH=/path/to/clip.mp4 in .env — pipeline.py picks it up.
"""

from __future__ import annotations

import queue
import re
import threading
import time
import logging
from datetime import datetime
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)

_VIDEO_EXTS    = {".mp4", ".avi", ".mkv", ".mov", ".ts", ".m4v"}
_REVERSE_CHUNK = 20

# NVR filename pattern: NVR_ch4_main_20260504111210_20260504111236.mp4
_NVR_TS_RE = re.compile(r'(\d{14})')


class VideoFileHandler:
    def __init__(
        self,
        path: str,
        loop: bool = True,
        speed: float = 1.0,
        queue_size: int = 8,
    ) -> None:
        self._path      = path
        self._loop      = loop
        self._speed     = speed
        self._direction: int = 1
        self._seek_frames: int = 0   # set by seek(); applied on next loop tick
        self._current_fps: float = 20.0
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._stop   = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._file_start_ts: float | None = None  # Unix ts of current file's first frame
        self._file_pos_sec: float = 0.0           # seconds into the current file

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop_fn, daemon=True, name="video-file"
        )
        self._thread.start()
        mode = "looping" if self._loop else "single-pass"
        logger.info("Video file source (%s) → %s  speed=%.1fx", mode, self._path, self._speed)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def set_speed(self, speed: float) -> None:
        self._speed = max(0.1, speed)

    def set_direction(self, direction: int) -> None:
        self._direction = 1 if direction >= 0 else -1

    def seek(self, frames: int) -> None:
        """Offset playback position by +/- frames. Applied on the next loop tick."""
        self._seek_frames += frames

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def get_frame(self, timeout: float = 2.0):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_current_wall_time(self) -> float | None:
        """Return estimated wall-clock Unix timestamp of the current frame.

        Parsed from NVR filename (YYYYMMDDHHMMSS) + position within file.
        Returns None if the filename doesn't match the NVR naming pattern.
        """
        start = self._file_start_ts
        if start is None:
            return None
        return start + self._file_pos_sec

    @staticmethod
    def _parse_file_start_ts(file_path: str) -> float | None:
        m = _NVR_TS_RE.search(Path(file_path).stem)
        if not m:
            return None
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
            return dt.astimezone().timestamp()
        except ValueError:
            return None

    def _files(self) -> list[str]:
        p = Path(self._path)
        if p.is_dir():
            files = sorted(f for f in p.iterdir() if f.suffix.lower() in _VIDEO_EXTS)
            if not files:
                logger.error("No video files found in %s", self._path)
            return [str(f) for f in files]
        return [self._path]

    def _loop_fn(self) -> None:
        while not self._stop.is_set():
            files = self._files()
            ordered = list(reversed(files)) if self._direction < 0 else files
            for file_path in ordered:
                if self._stop.is_set():
                    break
                prev_dir = self._direction
                self._play_file(file_path)
                # Direction changed mid-file — restart outer loop immediately
                # so the playlist order and start position are recalculated.
                if self._direction != prev_dir:
                    break

            if not self._loop:
                logger.info("Video playback finished — pipeline will idle")
                break

            logger.info("Video playlist looping…")

    def _play_file(self, file_path: str) -> None:
        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened():
            logger.error("Cannot open video file: %s", file_path)
            return

        fps   = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if total == 0:
            logger.warning("Skipping file with no frames: %s", file_path)
            cap.release()
            return

        self._current_fps = fps
        self._file_start_ts = self._parse_file_start_ts(file_path)
        self._file_pos_sec  = 0.0
        logger.info("Playing %s  %.0f fps  %d frames  dir=%+d",
                    file_path, fps, total, self._direction)

        # ── Forward ───────────────────────────────────────────────────────────
        if self._direction >= 0:
            while not self._stop.is_set():
                while self._paused.is_set() and not self._stop.is_set():
                    time.sleep(0.05)
                if self._stop.is_set():
                    break
                if self._direction < 0:
                    break  # direction changed mid-file

                # Apply pending seek (thread-safe: int assignment is atomic in CPython)
                if self._seek_frames:
                    delta = self._seek_frames
                    self._seek_frames = 0
                    pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(total - 1, pos + delta)))

                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

                # Speed > 1x: skip intermediate frames so the pipeline sees
                # every N-th frame while still advancing through the video at
                # the requested rate.  Skipping via cap.read() is cheaper than
                # a seek on CPU-decoded H.264 streams.
                skip = max(0, round(self._speed) - 1)
                for _ in range(skip):
                    cap.read()

                ok, frame = cap.read()
                if not ok:
                    break

                self._file_pos_sec = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

                # Blocking put — video paces itself to the pipeline instead of
                # dropping frames.  Frame drops broke ByteTrack continuity and
                # reset the chalking entry-frame counter mid-person.
                self._queue.put(frame)

        # ── Reverse (chunk-based) ─────────────────────────────────────────────
        else:
            head = total  # exclusive end of the next chunk
            while not self._stop.is_set() and head > 0:
                while self._paused.is_set() and not self._stop.is_set():
                    time.sleep(0.05)
                if self._stop.is_set():
                    break
                if self._direction >= 0:
                    break  # direction changed mid-file

                chunk_start = max(0, head - _REVERSE_CHUNK)
                cap.set(cv2.CAP_PROP_POS_FRAMES, chunk_start)

                frames: list = []
                for _ in range(head - chunk_start):
                    ok, f = cap.read()
                    if not ok:
                        break
                    frames.append(f)

                if not frames:
                    break

                fps_now     = cap.get(cv2.CAP_PROP_FPS) or fps
                frame_delay = 1.0 / (fps_now * self._speed)

                for frame in reversed(frames):
                    while self._paused.is_set() and not self._stop.is_set():
                        time.sleep(0.05)
                    if self._stop.is_set() or self._direction >= 0:
                        cap.release()
                        return

                    t0 = time.monotonic()
                    if self._queue.full():
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            pass
                    self._queue.put(frame)

                    elapsed = time.monotonic() - t0
                    sleep   = frame_delay - elapsed
                    if sleep > 0:
                        time.sleep(sleep)

                head = chunk_start

        cap.release()
