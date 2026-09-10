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
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from src.stream.playback_url import build_nvr_playback_url

logger = logging.getLogger(__name__)

# Age at which we call an AgentDVR chunk "stale" — i.e. AgentDVR has moved
# on and this file is a closed record, not the currently-being-written
# chunk. During normal recording, mtime updates at least every few seconds
# as ffmpeg flushes the encoder buffer; 30s of mtime silence is a strong
# signal that AgentDVR closed the chunk and started (or failed to start)
# a new one. Used to distinguish "open chunk race, retry later" from
# "alert falls in AgentDVR outage gap, permanent failure."
_AGENTDVR_CHUNK_STALE_SECONDS = 30.0

# AgentDVR continuous-chunk filename shape:
#   <cam_index>_<YYYY-MM-DD>_<HH-MM-SS>_<msec>.mkv
# Example: 4_2026-09-09_22-31-58_176.mkv (camera 4, chunk started
# 2026-09-09 22:31:58.176 local time). Timestamps are the recorder's
# WALL-CLOCK time — we convert alert_ts (unix UTC) to the same zone
# via NVR_TZ (defaults to America/Los_Angeles) before comparing.
_AGENTDVR_FILENAME_RE = re.compile(
    r"^\d+_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})_\d+\.mkv$"
)


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

    def failure_path(self, alert_id: int, alert_ts: float) -> Path:
        """Permanent-failure sentinel path.

        Lives beside `clip_path` with a `.failed` suffix so day-directory
        listings show clips and failures side-by-side, and both share the
        same rotation/backup rules. Presence means: this alert was
        determined to be unrecoverable (AgentDVR outage gap, source
        rotated off the NVR, etc.) and further archiver runs should not
        attempt it. Semantically the "tombstone" pattern — a permanent
        marker distinct from "haven't tried yet."
        """
        day = datetime.fromtimestamp(alert_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        return self.clips_dir / day / f"{alert_id}.failed"

    def has_clip(self, alert_id: int, alert_ts: float) -> bool:
        p = self.clip_path(alert_id, alert_ts)
        return p.exists() and p.stat().st_size > 0

    def is_permanent_failure(self, alert_id: int, alert_ts: float) -> bool:
        """True if a prior pull attempt marked this alert unrecoverable."""
        return self.failure_path(alert_id, alert_ts).exists()

    def _mark_permanent_failure(
        self, alert_id: int, alert_ts: float, reason: str,
    ) -> None:
        """Drop the tombstone. Idempotent; a `.failed` file may already
        exist from a prior attempt. Body carries the reason for later
        forensics (why did we give up on this alert?)."""
        p = self.failure_path(alert_id, alert_ts)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with p.open("w", encoding="utf-8") as f:
                f.write(f"{reason}\n")
        except OSError as e:
            logger.warning(
                "Archiver: failed to write tombstone for alert=%d: %s "
                "(continuing — alert will be re-attempted on next backfill).",
                alert_id, e,
            )

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
        if self.is_permanent_failure(alert_id, alert_ts):
            logger.debug(
                "Archiver: alert=%d already marked permanent failure, skipping",
                alert_id,
            )
            if on_done:
                on_done(alert_id, None)
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
        """Dispatch to the right source for this camera.

        Path-translation adapter pattern: each camera declares its footage
        source via env. AgentDVR local directory wins if present; NVR
        channel is the legacy path; if neither is set we WARN and return
        None (closes the silent channel-1 fallback bug — a no-NVR camera
        with no local dir configured used to archive channel-1 NVR
        footage under an unrelated alert_id).
        """
        out_path = self.clip_path(alert_id, alert_ts)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        agentdvr_dir = os.environ.get(f"AGENTDVR_DIR_{camera_id.upper()}")
        if agentdvr_dir:
            return self._pull_from_agentdvr(alert_id, alert_ts, camera_id, agentdvr_dir, out_path)

        env_channel = os.environ.get(f"NVR_CHANNEL_{camera_id.upper()}")
        if env_channel:
            try:
                channel = int(env_channel)
            except ValueError:
                channel = None
            return self._pull_from_nvr(alert_id, alert_ts, camera_id, channel, out_path)

        logger.warning(
            "Archiver: no footage source configured for camera=%s (alert=%d) — "
            "set AGENTDVR_DIR_%s or NVR_CHANNEL_%s in env. Skipping.",
            camera_id, alert_id, camera_id.upper(), camera_id.upper(),
        )
        return None

    def _pull_from_nvr(
        self,
        alert_id: int,
        alert_ts: float,
        camera_id: str,
        channel: Optional[int],
        out_path: Path,
    ) -> Optional[Path]:
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
        return self._run_ffmpeg(cmd, alert_id, camera_id, out_path, source="nvr")

    def _pull_from_agentdvr(
        self,
        alert_id: int,
        alert_ts: float,
        camera_id: str,
        agentdvr_dir: str,
        out_path: Path,
    ) -> Optional[Path]:
        """Slice a clip out of AgentDVR's continuous local chunks.

        AgentDVR writes back-to-back MKV chunks with wall-clock start
        times encoded in the filename. We find the chunk containing
        `alert_ts - pre_roll_seconds`, compute the offset from chunk
        start, and stream-copy `duration_seconds` out with ffmpeg -ss.
        Zero re-encode, HEVC preserved, `-tag:v hvc1` so browsers that
        support HEVC-in-MP4 (Safari) can play the output.
        """
        source_dir = Path(agentdvr_dir)
        if not source_dir.is_dir():
            logger.warning(
                "Archiver: AGENTDVR_DIR_%s='%s' is not a directory (alert=%d). "
                "Check the bind mount in docker-compose.yml.",
                camera_id.upper(), agentdvr_dir, alert_id,
            )
            return None

        target_local = self._alert_ts_to_local(alert_ts)
        window_start_local = target_local - timedelta(seconds=self.pre_roll_seconds)

        chunk = self._pick_agentdvr_chunk(source_dir, window_start_local)
        if chunk is None:
            # No chunk with start ≤ target means one of: (a) alert
            # predates AgentDVR's recording history, (b) retention
            # rotated the covering chunk off, (c) real gap in coverage.
            # All three are permanent failures — no future backfill run
            # will find footage that doesn't exist. Tombstone so the
            # archive_queue stops re-triggering us.
            reason = (
                f"no_agentdvr_chunk: no file with start <= "
                f"{window_start_local.isoformat()} in {source_dir}. "
                f"Predates history, was rotated off, or coverage gap."
            )
            logger.warning(
                "Archiver: alert=%d permanent failure — %s",
                alert_id, reason,
            )
            self._mark_permanent_failure(alert_id, alert_ts, reason)
            return None

        chunk_path, chunk_start_local = chunk
        offset_seconds = (window_start_local - chunk_start_local).total_seconds()
        if offset_seconds < 0:
            # Shouldn't happen given the picker's start<=target invariant, but
            # guard anyway — negative -ss is a silent misread in ffmpeg.
            offset_seconds = 0.0

        # Two failure modes look the same at first glance but need
        # opposite recovery semantics:
        #
        #   OPEN CHUNK RACE — picked file is currently being written by
        #     AgentDVR. Tail of our desired window hasn't been flushed
        #     yet; ffmpeg -ss would return a truncated/empty clip. Retry
        #     after the chunk closes is correct; backfill handles it.
        #
        #   AGENTDVR GAP — picked file was closed hours ago; AgentDVR
        #     wasn't recording during the alert's timestamp window
        #     (RTSP disconnect, service restart, etc.). No footage will
        #     ever exist. Retry is wrong — it burns backfill cycles
        #     forever and the operator sees the same misleading log
        #     line on every attempt.
        #
        # Discriminate by chunk age: if mtime is fresh (< 30s ago),
        # AgentDVR is actively flushing → OPEN. If mtime is stale, the
        # chunk closed at that time and the alert falls in the gap
        # between it and whatever came next (if anything). See PR that
        # closed wildlife-detector#183 for the prod incident that
        # motivated the split — a 15-min AgentDVR outage at 04:14
        # produced 3 permanently-unrecoverable alerts that were being
        # re-enqueued as "open chunk" indefinitely.
        try:
            file_mtime = chunk_path.stat().st_mtime
        except OSError:
            file_mtime = 0.0
        desired_end_unix = alert_ts + self.duration_seconds
        if desired_end_unix > file_mtime + 2.0:  # 2s safety margin for mtime lag
            now = time.time()
            chunk_age_s = now - file_mtime
            if chunk_age_s > _AGENTDVR_CHUNK_STALE_SECONDS:
                # Stale chunk — AgentDVR isn't writing to this file, and
                # the desired window is past its close time. Permanent
                # gap. Tombstone so backfill stops re-enqueueing.
                reason = (
                    f"agentdvr_gap: chunk {chunk_path.name} closed "
                    f"{int(chunk_age_s)}s ago at mtime={file_mtime:.0f}, "
                    f"alert desired_end={desired_end_unix:.0f} past close. "
                    f"AgentDVR wasn't recording when the alert fired."
                )
                logger.warning(
                    "Archiver: alert=%d permanent failure — %s",
                    alert_id, reason,
                )
                self._mark_permanent_failure(alert_id, alert_ts, reason)
                return None
            # Fresh chunk — AgentDVR still writing. Retry-later is right.
            logger.info(
                "Archiver: alert=%d in currently-open AgentDVR chunk "
                "(%s, mtime=%.0f age=%.1fs, desired_end=%.0f). "
                "Skipping — backfill after chunk closes.",
                alert_id, chunk_path.name, file_mtime, chunk_age_s,
                desired_end_unix,
            )
            return None

        # -ss BEFORE -i: input seek, uses container index for fast keyframe
        # jump. -c:v copy preserves HEVC bytes; -tag:v hvc1 so the MP4
        # container declares HEVC in the box that Safari/QuickTime read.
        # Without hvc1, HEVC-in-MP4 only opens in VLC.
        cmd = [
            self.ffmpeg_binary,
            "-nostdin",
            "-loglevel", "warning",
            "-y",
            "-ss", f"{offset_seconds:.3f}",
            "-i", str(chunk_path),
            "-t", str(self.duration_seconds),
            "-c:v", "copy",
            "-tag:v", "hvc1",
            "-an",
            "-movflags", "+faststart",
            str(out_path),
        ]
        return self._run_ffmpeg(cmd, alert_id, camera_id, out_path, source="agentdvr")

    def _pick_agentdvr_chunk(
        self, source_dir: Path, target_local: datetime,
    ) -> Optional[tuple[Path, datetime]]:
        """Latest chunk whose start ≤ target_local. Returns (path, start).

        Filename regex is anchored so garbage files (thumbnails, xml
        sidecars) are silently ignored.
        """
        best: Optional[tuple[Path, datetime]] = None
        for entry in source_dir.iterdir():
            if not entry.is_file():
                continue
            m = _AGENTDVR_FILENAME_RE.match(entry.name)
            if not m:
                continue
            try:
                start = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)),
                    tzinfo=target_local.tzinfo,
                )
            except ValueError:
                continue
            if start > target_local:
                continue
            if best is None or start > best[1]:
                best = (entry, start)
        return best

    def _alert_ts_to_local(self, alert_ts: float) -> datetime:
        """Convert unix UTC to the recorder's wall-clock zone (NVR_TZ)."""
        tz_name = os.getenv("NVR_TZ", "America/Los_Angeles")
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        return datetime.fromtimestamp(alert_ts, tz=timezone.utc).astimezone(tz)

    def _run_ffmpeg(
        self,
        cmd: list[str],
        alert_id: int,
        camera_id: str,
        out_path: Path,
        source: str,
    ) -> Optional[Path]:
        """Shared ffmpeg invocation + result handling for both NVR and
        AgentDVR pulls."""
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
            logger.info(
                "Archiver: ffmpeg timeout for alert=%d source=%s after %ds",
                alert_id, source, timeout,
            )
            self._cleanup_partial(out_path)
            return None
        except Exception as e:
            logger.info(
                "Archiver: ffmpeg spawn failed for alert=%d source=%s — %s",
                alert_id, source, e,
            )
            return None

        if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            logger.info(
                "Archiver: no clip for alert=%d source=%s (rc=%d, stderr=%s)",
                alert_id, source, result.returncode,
                (result.stderr or "").strip()[:200],
            )
            self._cleanup_partial(out_path)
            return None

        size_kb = out_path.stat().st_size // 1024
        logger.info(
            "Archiver: archived alert=%d camera=%s source=%s size=%dKB path=%s",
            alert_id, camera_id, source, size_kb, out_path,
        )
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
