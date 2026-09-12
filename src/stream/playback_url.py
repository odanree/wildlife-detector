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


def _nvr_env(camera_id: str, suffix: str, fallback_key: str = "") -> str:
    """Per-camera NVR env lookup with fallback to the fleet-wide AMCREST_* value.

    Two-NVR shape: a `crawlspace_inside` camera can live on an Annke NVR
    at 192.168.1.130 while yard/rooftop/backyard stay on the Amcrest at
    .148. NVR_HOST_<CAM> / NVR_USER_<CAM> / NVR_PASS_<CAM> / NVR_PORT_<CAM> /
    NVR_FAMILY_<CAM> override per camera; missing keys fall through to
    the AMCREST_* fleet defaults so existing deployments don't move.
    """
    if camera_id:
        v = os.getenv(f"NVR_{suffix}_{camera_id.upper()}")
        if v:
            return v
    if fallback_key:
        return os.getenv(fallback_key, "")
    return ""


def build_nvr_playback_url(
    timestamp: float,
    base_rtsp_url: str = "",
    pre_roll_seconds: int = 15,
    nvr_channel: int | None = None,
    speed: int = 1,
    camera_id: str = "",
) -> str:
    """Return an NVR RTSP playback URL for the given unix timestamp.

    Tries the Dahua RPC2 recording index first; falls back to a
    time-based URL. URL shape depends on NVR_FAMILY_<CAM>:
      - "dahua"     (default) → /cam/playback?channel=N&subtype=0&starttime=…
      - "hikvision" → /Streaming/tracks/<ch>01/?starttime=<ISO-UTC>&endtime=…
    Hikvision/Annke firmware doesn't understand /cam/playback and vice
    versa; getting this wrong returns an empty stream that VLC reports
    as 'failed to open'.

    speed: playback multiplier (1, 2, 4, 8). Dahua accepts &speedpara=N;
    Hikvision has no equivalent query knob, so speed is ignored for that
    family and logged at warning.
    """
    # Optional Dahua RPC2 index lookup — public builds omit this helper.
    try:
        from src.stream.amcrest_api import find_recording_rtsp  # type: ignore[import-not-found]
    except ImportError:
        def find_recording_rtsp(*_a, **_kw):  # type: ignore[no-redef]
            return None

    host = _nvr_env(camera_id, "HOST", "AMCREST_HOST") or (re.search(r'@([^:/]+)', base_rtsp_url, re.I) and re.search(r'@([^:/]+)', base_rtsp_url).group(1)) or ""
    port = _nvr_env(camera_id, "PORT", "AMCREST_PORT") or "554"
    user = _nvr_env(camera_id, "USER", "AMCREST_USER") or (re.search(r'://([^:]+):', base_rtsp_url) and re.search(r'://([^:]+):', base_rtsp_url).group(1)) or ""
    pwd  = _nvr_env(camera_id, "PASS", "AMCREST_PASS") or (re.search(r'://[^:]+:([^@]+)@', base_rtsp_url) and re.search(r'://[^:]+:([^@]+)@', base_rtsp_url).group(1)) or ""
    family = (_nvr_env(camera_id, "FAMILY") or "dahua").strip().lower()
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
    # Hikvision family skips the Dahua RPC2 index lookup — different API.
    url = (
        None if family == "hikvision"
        else find_recording_rtsp(host, user, pwd, port, int(ch), dt, pre_roll_seconds)
    )

    speed_suffix = f"&speedpara={speed}" if speed != 1 else ""

    if url is None:
        # Post-roll defaults to 2min: long enough to capture the whole
        # event, short enough that VLC / ffmpeg doesn't try to seek
        # through 2h of motion-recording gaps to find the next chunk.
        start = dt - timedelta(seconds=pre_roll_seconds)
        end   = dt + timedelta(minutes=2)
        if family == "hikvision":
            # Annke / Hikvision playback expects UTC ISO-8601 without
            # separators: YYYYMMDDTHHMMSSZ. The device converts to its
            # own configured timezone internally.
            start_utc = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            end_utc   = end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            if speed != 1:
                logger.warning(
                    "NVR playback: Hikvision family ignores speed=%dx (no query knob).",
                    speed,
                )
            url = (
                f"rtsp://{user}:{pwd}@{host}:{port}"
                f"/Streaming/tracks/{ch}01/?starttime={start_utc}&endtime={end_utc}"
            )
            logger.info("NVR playback (hikvision) ch=%s start=%s", ch, start_utc)
        else:
            # Dahua/Amcrest /cam/playback expects starttime/endtime in
            # the NVR's local clock. subtype=0 pins the main stream —
            # without it firmware returns nothing and VLC reports
            # 'failed to open'.
            start_str = start.strftime("%Y_%m_%d_%H_%M_%S")
            end_str   = end.strftime("%Y_%m_%d_%H_%M_%S")
            url = (
                f"rtsp://{user}:{pwd}@{host}:{port}"
                f"/cam/playback?channel={ch}&subtype=0&starttime={start_str}&endtime={end_str}{speed_suffix}"
            )
            logger.info("NVR playback (dahua fallback) ch=%s start=%s speed=%dx", ch, start_str, speed)
    else:
        url += speed_suffix
        safe = re.sub(r'://[^:]+:[^@]+@', '://****:****@', url)
        logger.info("NVR playback → %s (speed=%dx)", safe, speed)

    return url
