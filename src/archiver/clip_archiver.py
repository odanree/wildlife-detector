"""Off-NVR clip archiver — pulls RTSP playback footage for TP-verified
alerts and writes local mp4 copies before the NVR's FIFO rotation eats
the source.

Motivation: the Amcrest/Dahua NVR runs at max capacity and FIFOs oldest
recordings when disk fills. A motion-heavy burst can silently push a
week of older clips off the back end (observed Jul 20-27 gap). Since
our detector already logs the alert record + snapshot to durable local
storage, the missing piece is the video clip. Pulling it eagerly at
label-time — when the operator has just confirmed the alert is a real
TP — turns the NVR into a rolling cache and our local disk into the
authoritative archive for the ground-truth dataset.

## Design

**Trigger**: web_service posts every "correct" verdict here (idempotent
by alert_id — if the clip file already exists, we skip). Fires from the
label endpoint after StateDB.set_label commits.

**Concurrency**: bounded ThreadPoolExecutor(max_workers=2). ffmpeg is
cheap (stream copy, no re-encode) but the NVR can only serve a couple
of concurrent RTSP sessions before it starts dropping packets. Bounded
pool also caps CPU/memory blast radius if an operator labels rapidly.

**Fetch**: `ffmpeg -rtsp_transport tcp -y -i <playback_url> -t 45
-c copy <out>`. `-c copy` mux preserves the NVR's H.264/H.265 as-is;
`-rtsp_transport tcp` avoids UDP packet loss on marginal networks;
`-t 45` caps to 45s so a runaway pull can't grow unbounded.

**Idempotency**: `data/clips/YYYY-MM-DD/<alert_id>.mp4` is derived from
alert_id alone — same alert always writes the same path. Exists-check
before ffmpeg spawn.

**Failure mode**: ffmpeg returning empty or erroring out is expected
(NVR rotation gap, transient network, camera offline). We log at INFO
level (not WARN) so the operator's log tail isn't spammed. Missing
clips just mean the RTSP fallback URL will be used on Replay.

Pattern: **eager materialization of the durable subset** — same shape
as any write-through cache: high-value items get promoted to durable
storage the moment they're identified.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from src.stream.playback_url import build_nvr_playback_url

logger = logging.getLogger(__name__)


class ClipArchiver:
    """Bounded-pool archiver. Instantiate once at app startup, share
    the instance across request handlers. `submit()` is fire-and-forget
    — never blocks the caller.
    """

    def __init__(
        self,
        clips_dir: Path,
        pre_roll_seconds: int = 15,
        duration_seconds: int = 45,
        max_workers: int = 2,
        ffmpeg_binary: str = "ffmpeg",
    ) -> None:
        self.clips_dir = Path(clips_dir)
        self.pre_roll_seconds = pre_roll_seconds
        self.duration_seconds = duration_seconds
        self.ffmpeg_binary = ffmpeg_binary
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="clip-archiver",
        )
        # In-flight guard: two overlapping labels on the same alert_id
        # shouldn't launch two ffmpeg processes.
        self._inflight: set[int] = set()
        self._inflight_lock = threading.Lock()
        self.clips_dir.mkdir(parents=True, exist_ok=True)

    def clip_path(self, alert_id: int, alert_ts: float) -> Path:
        """Derived path — same alert always yields the same path so the
        exists-check is stable across restarts."""
        day = datetime.fromtimestamp(alert_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        return self.clips_dir / day / f"{alert_id}.mp4"

    def has_clip(self, alert_id: int, alert_ts: float) -> bool:
        p = self.clip_path(alert_id, alert_ts)
        return p.exists() and p.stat().st_size > 0

    def submit(
        self,
        alert_id: int,
        alert_ts: float,
        camera_id: str,
        on_done: Optional[Callable[[int, Optional[Path]], None]] = None,
    ) -> None:
        """Enqueue a clip pull for this alert. Skips if clip already
        exists or is in-flight. Fire-and-forget — returns immediately.

        `on_done(alert_id, clip_path_or_None)` fires after the pull
        completes (success or failure) if provided.
        """
        if self.has_clip(alert_id, alert_ts):
            logger.debug("Archiver: clip exists for alert=%d, skipping", alert_id)
            if on_done:
                on_done(alert_id, self.clip_path(alert_id, alert_ts))
            return
        with self._inflight_lock:
            if alert_id in self._inflight:
                logger.debug("Archiver: already in-flight for alert=%d", alert_id)
                return
            self._inflight.add(alert_id)
        self._pool.submit(self._pull_and_release, alert_id, alert_ts, camera_id, on_done)

    def _pull_and_release(
        self,
        alert_id: int,
        alert_ts: float,
        camera_id: str,
        on_done: Optional[Callable[[int, Optional[Path]], None]],
    ) -> None:
        try:
            path = self._pull(alert_id, alert_ts, camera_id)
        finally:
            with self._inflight_lock:
                self._inflight.discard(alert_id)
        if on_done:
            on_done(alert_id, path)

    def _pull(self, alert_id: int, alert_ts: float, camera_id: str) -> Optional[Path]:
        out_path = self.clip_path(alert_id, alert_ts)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Per-camera NVR channel from env, matching web_service.api_alert_playback.
        env_channel = os.environ.get(f"NVR_CHANNEL_{camera_id.upper()}")
        try:
            channel = int(env_channel) if env_channel else None
        except ValueError:
            channel = None

        url = build_nvr_playback_url(
            timestamp=alert_ts,
            base_rtsp_url="",
            pre_roll_seconds=self.pre_roll_seconds,
            speed=1,
            nvr_channel=channel,
        )

        # -y: overwrite (should never fire, since we checked exists first
        # — but a half-written file from a crashed prior attempt would
        # otherwise wedge). -c:v copy: no video re-encode (fast, no CPU
        # cost). -an: drop audio; the NVR mic uses pcm_mulaw which mp4
        # containers don't support, and wildlife alerts don't need audio
        # anyway. -t: hard duration cap.
        cmd = [
            self.ffmpeg_binary,
            "-nostdin",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-y",
            "-i", url,
            "-t", str(self.duration_seconds),
            "-c:v", "copy",
            "-an",
            "-movflags", "+faststart",
            str(out_path),
        ]

        # Hard subprocess timeout at duration * 3 — gives ffmpeg headroom
        # to negotiate + flush but kills a pull that stalls indefinitely.
        timeout = self.duration_seconds * 3
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.info("Archiver: ffmpeg timeout for alert=%d after %ds", alert_id, timeout)
            self._cleanup_partial(out_path)
            return None
        except Exception as e:
            logger.info("Archiver: ffmpeg spawn failed for alert=%d — %s", alert_id, e)
            return None

        if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            logger.info(
                "Archiver: no clip for alert=%d (rc=%d, stderr=%s)",
                alert_id, result.returncode, (result.stderr or "").strip()[:200],
            )
            self._cleanup_partial(out_path)
            return None

        size_kb = out_path.stat().st_size // 1024
        logger.info("Archiver: archived alert=%d camera=%s size=%dKB path=%s",
                    alert_id, camera_id, size_kb, out_path)
        return out_path

    def _cleanup_partial(self, path: Path) -> None:
        try:
            if path.exists() and path.stat().st_size == 0:
                path.unlink()
        except OSError:
            pass

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


_singleton: Optional[ClipArchiver] = None


def get_archiver() -> Optional[ClipArchiver]:
    """Module-level accessor for the shared archiver instance."""
    return _singleton


def init_archiver(clips_dir: Path) -> ClipArchiver:
    """Idempotent init — safe to call at app startup."""
    global _singleton
    if _singleton is None:
        _singleton = ClipArchiver(clips_dir=clips_dir)
        logger.info("ClipArchiver initialized: clips_dir=%s", clips_dir)
    return _singleton
