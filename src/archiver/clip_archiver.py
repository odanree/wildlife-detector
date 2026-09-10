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
# a new one. In-container (Linux mount of the AgentDVR host dir) this is
# fine; do NOT invoke from a Windows-native process (native NTFS lazily
# updates directory mtime and would tombstone every current chunk).
_AGENTDVR_CHUNK_STALE_SECONDS = 30.0

# Max seconds between chunk N's close and chunk N+1's start before we
# call it a real gap (as opposed to a normal file-rotation seam).
# AgentDVR's normal seam is ~1s. 5s gives room for occasional flush
# jitter without swallowing genuine short outages.
_AGENTDVR_SEAM_TOLERANCE_SECONDS = 5.0

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

        # Enumerate all chunks up front so we can reason about neighbors
        # (seam vs. gap discrimination) and distinguish "dir is empty"
        # from "all chunks postdate the alert."
        all_chunks = self._list_agentdvr_chunks(source_dir, target_local.tzinfo)
        if not all_chunks:
            # Empty listing: bind mount is present but no chunks visible.
            # Could be a mount that lost its backing store (external
            # drive dropped, Docker Desktop share hiccup) or a genuine
            # first-boot state before AgentDVR has written anything.
            # Either way, tombstoning would be premature — the operator
            # can re-run backfill after fixing the mount, and if this is
            # first-boot the alert probably has no footage anyway but
            # tombstoning locks that in permanently. Skip without a
            # tombstone; retry-safe.
            logger.warning(
                "Archiver: alert=%d — AGENTDVR_DIR_%s='%s' is empty of "
                "chunks. Bind mount or first-boot? Not tombstoning; "
                "run backfill after diagnosing.",
                alert_id, camera_id.upper(), agentdvr_dir,
            )
            return None

        # Pick the covering chunk (latest with start ≤ window_start).
        chunk_index: Optional[int] = None
        for i, (_, start) in enumerate(all_chunks):
            if start <= window_start_local:
                chunk_index = i
            else:
                break
        if chunk_index is None:
            # Chunks exist but all start > target. Alert predates
            # AgentDVR's recording history for this camera; no future
            # backfill will find footage that never existed.
            first_chunk_name = all_chunks[0][0].name
            reason = (
                f"predates_history: alert window_start "
                f"{window_start_local.isoformat()} predates earliest "
                f"chunk ({first_chunk_name}). Camera onboarded after "
                f"the alert or retention rotated the covering chunk off."
            )
            logger.warning(
                "Archiver: alert=%d permanent failure — %s",
                alert_id, reason,
            )
            self._mark_permanent_failure(alert_id, alert_ts, reason)
            return None

        chunk_path, chunk_start_local = all_chunks[chunk_index]
        next_chunk: Optional[tuple[Path, datetime]] = (
            all_chunks[chunk_index + 1] if chunk_index + 1 < len(all_chunks) else None
        )

        # Three failure modes had to be teased apart from what previously
        # looked like one "desired_end > mtime" skip:
        #
        #   OPEN CHUNK RACE — covering chunk is being written by
        #     AgentDVR right now. Tail of our desired window hasn't
        #     been flushed. Retry after chunk closes is correct.
        #
        #   BOUNDARY SEAM — covering chunk closed cleanly and the next
        #     chunk started within ~5s. Alert crosses the seam. Footage
        #     lives in the next chunk — slice from there with offset=0
        #     (loses pre-roll if window_start < next.start, but that's
        #     better than tombstoning recoverable footage).
        #
        #   AGENTDVR GAP — covering chunk closed and either no next
        #     chunk exists yet (AgentDVR still down) or the next
        #     chunk's start is far past the covering chunk's mtime
        #     (real outage window). No footage will ever exist for the
        #     alert's window. Tombstone.
        #
        # See PR that closed wildlife-detector#183 for the prod
        # incident (15-min outage at 04:14, 3 permanently-unrecoverable
        # alerts) that motivated the split, and Fable's two review
        # passes that caught: (1st) boundary-seam false-positive
        # tombstones and (2nd) the alert_ts-vs-desired_end confusion
        # that used to redirect alerts INSIDE a stale chunk to its
        # successor (silently returning wrong footage).
        try:
            file_mtime = chunk_path.stat().st_mtime
        except OSError:
            file_mtime = 0.0
        desired_end_unix = alert_ts + self.duration_seconds

        if desired_end_unix > file_mtime + 2.0:  # 2s safety margin for mtime lag
            now = time.time()
            chunk_age_s = now - file_mtime

            if chunk_age_s <= _AGENTDVR_CHUNK_STALE_SECONDS:
                # Covering chunk is fresh — AgentDVR still writing. The
                # tail of our desired window hasn't been flushed yet.
                # Retry-later is right.
                logger.info(
                    "Archiver: alert=%d in currently-open AgentDVR chunk "
                    "(%s, mtime=%.0f age=%.1fs, desired_end=%.0f). "
                    "Skipping — backfill after chunk closes.",
                    alert_id, chunk_path.name, file_mtime, chunk_age_s,
                    desired_end_unix,
                )
                return None

            # Covering chunk is stale (closed). Where does the ALERT
            # MOMENT itself lie — inside A, or past A's close?
            #
            # Fable's 2nd review caught the subtle case: alert=14:14:00,
            # A.mtime=14:14:35, B.start=14:14:36. desired_end (14:14:45)
            # is past A.mtime, so the old code redirected to B and
            # sliced from B.start=14:14:36 — 36 seconds AFTER the alert.
            # Silent wrong evidence. Fix: only redirect / tombstone
            # when the alert moment itself is past A's close time. If
            # the alert falls INSIDE A, slice A even though the tail
            # is truncated — ffmpeg -t gives us whatever's available
            # and the resulting clip is correct if short.
            if alert_ts <= file_mtime:
                logger.info(
                    "Archiver: alert=%d inside stale chunk %s "
                    "(mtime=%.0f, alert_ts=%.0f); slicing truncated. "
                    "Clip may be shorter than %ds but represents "
                    "authoritative footage.",
                    alert_id, chunk_path.name, file_mtime, alert_ts,
                    self.duration_seconds,
                )
                return self._slice_agentdvr_chunk(
                    alert_id, alert_ts, camera_id,
                    chunk_path=chunk_path,
                    chunk_start_local=chunk_start_local,
                    window_start_local=window_start_local,
                    out_path=out_path,
                )

            # Alert moment past A's close. Seam or gap?
            if next_chunk is not None:
                next_path, next_start_local = next_chunk
                seam_gap_s = (next_start_local.timestamp() - file_mtime)
                if seam_gap_s <= _AGENTDVR_SEAM_TOLERANCE_SECONDS:
                    # Boundary seam — footage continues in the next
                    # chunk. But: B itself may be the currently-open
                    # chunk. If B's mtime is fresh AND desired_end is
                    # past B's mtime, the tail STILL isn't flushed —
                    # retry-later applies to B too, not just A.
                    try:
                        next_mtime = next_path.stat().st_mtime
                    except OSError:
                        next_mtime = 0.0
                    next_age_s = now - next_mtime
                    if (next_age_s <= _AGENTDVR_CHUNK_STALE_SECONDS
                            and desired_end_unix > next_mtime + 2.0):
                        logger.info(
                            "Archiver: alert=%d seams from %s into open "
                            "chunk %s (next.mtime=%.0f age=%.1fs, "
                            "desired_end=%.0f). Skipping — backfill "
                            "after next chunk closes.",
                            alert_id, chunk_path.name, next_path.name,
                            next_mtime, next_age_s, desired_end_unix,
                        )
                        return None
                    return self._slice_agentdvr_chunk(
                        alert_id, alert_ts, camera_id,
                        chunk_path=next_path,
                        chunk_start_local=next_start_local,
                        window_start_local=window_start_local,
                        out_path=out_path,
                    )
                # Real gap between chunk and its successor.
                reason = (
                    f"agentdvr_gap: chunk {chunk_path.name} closed "
                    f"{int(chunk_age_s)}s ago (mtime={file_mtime:.0f}), "
                    f"next chunk {next_path.name} started "
                    f"{seam_gap_s:.1f}s later — outside the "
                    f"{_AGENTDVR_SEAM_TOLERANCE_SECONDS:.0f}s seam. "
                    f"Alert desired_end={desired_end_unix:.0f} falls in "
                    f"the gap; AgentDVR wasn't recording."
                )
            else:
                # No next chunk AND covering chunk is stale AND alert
                # moment is past A's close. AgentDVR appears to have
                # stopped recording after this chunk. Tombstone.
                reason = (
                    f"agentdvr_gap: chunk {chunk_path.name} is the "
                    f"latest in the directory but closed "
                    f"{int(chunk_age_s)}s ago (mtime={file_mtime:.0f}). "
                    f"AgentDVR appears to have stopped recording; "
                    f"alert_ts={alert_ts:.0f} past close."
                )
            logger.warning(
                "Archiver: alert=%d permanent failure — %s",
                alert_id, reason,
            )
            self._mark_permanent_failure(alert_id, alert_ts, reason)
            return None

        return self._slice_agentdvr_chunk(
            alert_id, alert_ts, camera_id,
            chunk_path=chunk_path,
            chunk_start_local=chunk_start_local,
            window_start_local=window_start_local,
            out_path=out_path,
        )

    def _slice_agentdvr_chunk(
        self,
        alert_id: int,
        alert_ts: float,
        camera_id: str,
        *,
        chunk_path: Path,
        chunk_start_local: datetime,
        window_start_local: datetime,
        out_path: Path,
    ) -> Optional[Path]:
        """Stream-copy `duration_seconds` out of a specific chunk.

        Split out of the main _pull_from_agentdvr flow so the boundary-
        seam fallback can call it against the next chunk without
        duplicating the ffmpeg invocation.
        """
        offset_seconds = max(
            0.0, (window_start_local - chunk_start_local).total_seconds(),
        )
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

    def _list_agentdvr_chunks(
        self, source_dir: Path, tz: Optional["datetime.tzinfo"],
    ) -> list[tuple[Path, datetime]]:
        """All valid chunks in source_dir, sorted by start ascending.

        Distinct from `_pick_agentdvr_chunk` — that returns just the
        best pre-target candidate. This one is used when we need to
        know about neighbors (seam detection, "is the directory empty
        vs. all-future" disambiguation for the tombstone logic).

        Returns an empty list on OSError (bind mount vanished mid-run,
        permission denied, etc.). Caller sees "empty listing" and
        routes to the retry-safe branch instead of tombstoning.
        """
        chunks: list[tuple[Path, datetime]] = []
        try:
            entries = list(source_dir.iterdir())
        except OSError as e:
            logger.warning(
                "Archiver: iterdir failed on %s: %s. Treating as empty listing.",
                source_dir, e,
            )
            return []
        for entry in entries:
            if not entry.is_file():
                continue
            m = _AGENTDVR_FILENAME_RE.match(entry.name)
            if not m:
                continue
            try:
                start = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)),
                    tzinfo=tz,
                )
            except ValueError:
                continue
            chunks.append((entry, start))
        chunks.sort(key=lambda x: x[1])
        return chunks

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
