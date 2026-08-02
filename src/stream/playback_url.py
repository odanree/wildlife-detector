"""NVR playback URL builder — cv2-free so lightweight services (archiver,
sidecars) can import it without pulling in OpenCV.

Extracted from `rtsp_handler.py` when the clip archiver needed the URL
builder in a container that doesn't run cv2. `rtsp_handler` re-exports
`build_nvr_playback_url` from here for backwards compatibility so
existing detector call sites don't need to change.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def build_nvr_playback_url(
    timestamp: float,
    base_rtsp_url: str = "",
    pre_roll_seconds: int = 15,
    nvr_channel: int | None = None,
    speed: int = 1,
) -> str:
    """Return an NVR RTSP playback URL for the given unix timestamp.

    Tries the Dahua RPC2 recording index first; falls back to a
    time-based /cam/playback URL that stock Amcrest/Dahua firmware
    accepts. `subtype=0` is required for the fallback — without it,
    firmware returns an empty stream and VLC reports 'failed to open'.

    speed: playback multiplier (1, 2, 4, 8) — appended as &speedpara=N.
    """
    # Optional Dahua RPC2 index lookup — public builds omit this helper.
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

    # Amcrest/Dahua /cam/playback expects starttime/endtime in the
    # NVR's local clock, not UTC. Container defaults to UTC so
    # .astimezone() with no arg stays UTC — that ships timestamps 7-8h
    # off Pacific and the NVR returns no data. NVR_TZ env defaults to
    # America/Los_Angeles.
    _nvr_tz_name = os.getenv("NVR_TZ", "America/Los_Angeles")
    try:
        from zoneinfo import ZoneInfo
        _nvr_tz = ZoneInfo(_nvr_tz_name)
    except Exception:
        logger.warning(
            "NVR_TZ='%s' invalid; falling back to UTC. Playback URL timestamps "
            "will be wrong if NVR clock isn't UTC.",
            _nvr_tz_name,
        )
        _nvr_tz = timezone.utc
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(_nvr_tz)
    url = find_recording_rtsp(host, user, pwd, port, int(ch), dt, pre_roll_seconds)

    speed_suffix = f"&speedpara={speed}" if speed != 1 else ""

    if url is None:
        # subtype=0 pins the main stream — /cam/playback returns nothing
        # (VLC: 'failed to open') without it.
        # Post-roll defaults to 2min: long enough to capture the whole
        # event, short enough that VLC / ffmpeg doesn't try to seek
        # through 2h of motion-recording gaps to find the next chunk.
        start     = dt - timedelta(seconds=pre_roll_seconds)
        end       = dt + timedelta(minutes=2)
        start_str = start.strftime("%Y_%m_%d_%H_%M_%S")
        end_str   = end.strftime("%Y_%m_%d_%H_%M_%S")
        url = (
            f"rtsp://{user}:{pwd}@{host}:{port}"
            f"/cam/playback?channel={ch}&subtype=0&starttime={start_str}&endtime={end_str}{speed_suffix}"
        )
        logger.info("NVR playback (time-based fallback) ch=%s start=%s speed=%dx", ch, start_str, speed)
    else:
        url += speed_suffix
        safe = re.sub(r'://[^:]+:[^@]+@', '://****:****@', url)
        logger.info("NVR playback → %s (speed=%dx)", safe, speed)

    return url
