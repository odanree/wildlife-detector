import os
import queue
import re
import threading
import time
import logging
from datetime import datetime, timedelta, timezone
import cv2

logger = logging.getLogger(__name__)

# Force TCP, video-only.  C-level stderr suppression is handled in main_web.py
# before any cv2 import so that OpenCV's FFmpeg plugin DLL initialises its
# static-CRT stderr to NUL rather than the console.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "rtsp_transport;tcp|allowed_media_types;video"
)


def build_nvr_playback_url(
    timestamp: float,
    base_rtsp_url: str = "",
    pre_roll_seconds: int = 30,
    nvr_channel: int | None = None,
    speed: int = 1,
) -> str:
    """Return an NVR RTSP playback URL for the given unix timestamp.

    Tries the Dahua RPC2 recording index first; falls back to the time-based
    URL format supported by most Amcrest/Dahua firmware.

    speed: playback multiplier (1, 2, 4, 8) — appended as &speedpara=N.
    """
    # Optional Dahua RPC2 index lookup — if the helper module isn't
    # shipped (public builds omit it), fall through to the time-based
    # URL directly. Time-based works on stock Amcrest/Dahua firmware.
    try:
        from src.stream.amcrest_api import find_recording_rtsp  # type: ignore[import-not-found]
    except ImportError:
        def find_recording_rtsp(*_a, **_kw):  # type: ignore[no-redef]
            return None

    host = os.getenv("AMCREST_HOST") or (re.search(r'@([^:/]+)', base_rtsp_url, re.I) and re.search(r'@([^:/]+)', base_rtsp_url).group(1)) or ""
    port = os.getenv("AMCREST_PORT", "554")
    user = os.getenv("AMCREST_USER") or (re.search(r'://([^:]+):', base_rtsp_url) and re.search(r'://([^:]+):', base_rtsp_url).group(1)) or ""
    pwd  = os.getenv("AMCREST_PASS") or (re.search(r'://[^:]+:([^@]+)@', base_rtsp_url) and re.search(r'://[^:]+:([^@]+)@', base_rtsp_url).group(1)) or ""
    ch_m = re.search(r'channel=(\d+)', base_rtsp_url)
    ch   = str(nvr_channel) if nvr_channel else (ch_m.group(1) if ch_m else '1')

    # Format the timestamp in the NVR's local timezone. Amcrest/Dahua
    # /cam/playback expects starttime/endtime in the CAMERA's local
    # clock, not UTC. Container runs in UTC by default so .astimezone()
    # with no arg stays UTC — that ships timestamps 7-8h off from
    # Pacific and the NVR returns no data. NVR_TZ env override defaults
    # to America/Los_Angeles.
    _nvr_tz_name = os.getenv("NVR_TZ", "America/Los_Angeles")
    try:
        from zoneinfo import ZoneInfo
        _nvr_tz = ZoneInfo(_nvr_tz_name)
    except Exception:
        logger.warning("NVR_TZ='%s' invalid; falling back to UTC. Playback URL timestamps will be wrong if NVR clock isn't UTC.",
                       _nvr_tz_name)
        _nvr_tz = timezone.utc
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(_nvr_tz)
    url = find_recording_rtsp(host, user, pwd, port, int(ch), dt, pre_roll_seconds)

    speed_suffix = f"&speedpara={speed}" if speed != 1 else ""

    if url is None:
        start     = dt - timedelta(seconds=pre_roll_seconds)
        end       = start + timedelta(hours=2)
        start_str = start.strftime("%Y_%m_%d_%H_%M_%S")
        end_str   = end.strftime("%Y_%m_%d_%H_%M_%S")
        url = (
            f"rtsp://{user}:{pwd}@{host}:{port}"
            f"/cam/playback?channel={ch}&starttime={start_str}&endtime={end_str}{speed_suffix}"
        )
        logger.info("NVR playback (time-based fallback) ch=%s start=%s speed=%dx", ch, start_str, speed)
    else:
        url += speed_suffix
        safe = re.sub(r'://[^:]+:[^@]+@', '://****:****@', url)
        logger.info("NVR playback → %s (speed=%dx)", safe, speed)

    return url


class RTSPHandler:
    """Thread-safe RTSP frame producer.

    Drops stale frames when the consumer falls behind so the pipeline always
    sees the most recent image rather than a growing backlog.
    """

    def __init__(self, url: str, queue_size: int = 2, reconnect_delay: float = 3.0):
        self._base_url = url          # original live-stream URL
        self._url = url               # active URL (may be a playback URL)
        self._pending_url: str | None = None
        self._url_lock = threading.Lock()
        self._is_playback = False
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._reconnect_delay = reconnect_delay
        self._thread: threading.Thread | None = None
        # Rolling pickup-age samples for consumer-side lag telemetry
        self._pickup_age_samples: list[float] = []

    @property
    def is_playback(self) -> bool:
        return self._is_playback

    def seek_to_datetime(self, dt: datetime, pre_roll_seconds: int = 30, nvr_channel: int | None = None) -> None:
        """Reconnect to NVR playback at dt. Delegates URL building to build_nvr_playback_url()."""
        playback_url = build_nvr_playback_url(
            dt.timestamp(), self._base_url, pre_roll_seconds, nvr_channel
        )
        with self._url_lock:
            self._pending_url = playback_url
            self._is_playback = True
        safe_url = re.sub(r'://[^:]+:[^@]+@', '://****:****@', playback_url)
        logger.info("NVR seek → %s", safe_url)

    def go_live(self) -> None:
        """Switch back to the original live stream URL."""
        with self._url_lock:
            self._pending_url = self._base_url
            self._is_playback = False
        logger.info("NVR → live stream")

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rtsp-reader")
        self._thread.start()
        safe = re.sub(r'://[^:]+:[^@]+@', '://****:****@', self._url)
        logger.info("RTSP reader started → %s", safe)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)

    def get_frame(self, timeout: float = 2.0):
        """Return the latest frame or None on timeout.

        Frames carry a capture wall-clock; we strip it before returning
        and periodically log the pickup-age. Age reflects time between
        cap.read() and consumer pickup — high age means our queue is
        holding stale frames (downstream bug); low age means any lag
        the user sees is upstream (camera / network buffer).
        """
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        captured_ts, frame = item
        age = time.time() - captured_ts
        self._pickup_age_samples.append(age)
        if len(self._pickup_age_samples) >= 100:
            samples = sorted(self._pickup_age_samples)
            n = len(samples)
            logger.info(
                "consumer-pickup-age: n=%d avg=%.0fms p50=%.0fms p95=%.0fms max=%.0fms",
                n,
                1000.0 * sum(samples) / n,
                1000.0 * samples[n // 2],
                1000.0 * samples[int(n * 0.95)],
                1000.0 * samples[-1],
            )
            self._pickup_age_samples.clear()
        return frame

    # ── Internal ──────────────────────────────────────────────────────────────

    def _open_capture(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _loop(self) -> None:
        cap = self._open_capture()
        # Reader cadence instrumentation — logs fps + rapid-read ratio every
        # 5s. If natural fps is ~30 and we see fps=60+ with rapid_pct high,
        # we're burning FFmpeg-demuxer backlog (consumer-side backpressure);
        # frames coming off cap.read() are stale. Confirms whether we need
        # a grab-and-discard drain at this boundary.
        m_win_start = time.monotonic()
        m_last_read = m_win_start
        m_frames = 0
        m_rapid = 0
        while not self._stop.is_set():
            # Hot-swap URL (seek or go-live)
            with self._url_lock:
                pending = self._pending_url
                if pending is not None:
                    self._url = pending
                    self._pending_url = None
            if pending is not None:
                safe = re.sub(r'://[^:]+:[^@]+@', '://****:****@', pending).split("?")[0]
                logger.info("Hot-swap → %s", safe)
                cap.release()
                cap = self._open_capture()
                logger.debug("Capture opened  isOpened=%s", cap.isOpened())
                continue

            ok, frame = cap.read()
            now = time.monotonic()
            if ok:
                m_frames += 1
                if now - m_last_read < 0.010:
                    m_rapid += 1
                m_last_read = now
                elapsed = now - m_win_start
                if elapsed >= 5.0:
                    fps = m_frames / elapsed
                    rapid_pct = 100.0 * m_rapid / max(m_frames, 1)
                    logger.info(
                        "reader-cadence: fps=%.1f rapid=%.0f%% (rapid=<10ms between reads → FFmpeg-demuxer backlog)",
                        fps, rapid_pct,
                    )
                    m_win_start = now
                    m_frames = 0
                    m_rapid = 0
            if not ok:
                logger.warning("Stream read failed — reconnecting in %.0fs…", self._reconnect_delay)
                cap.release()
                time.sleep(self._reconnect_delay)
                cap = self._open_capture()
                continue

            # Evict the stale frame so we never block
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
            # Tag frame with capture wall-clock so the consumer can log age
            # at pickup and prove where any lag is (upstream vs our queue).
            self._queue.put((time.time(), frame))

        cap.release()
