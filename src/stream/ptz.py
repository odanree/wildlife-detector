"""PTZ control for Amcrest/Dahua cameras via HTTP CGI.

Commands go over HTTP to the NVR (or individual camera) — NOT over RTSP.
The NVR forwards the command to the physical camera on the requested channel.

Environment variables:
  AMCREST_HOST        NVR/camera IP or hostname (shared with playback)
  AMCREST_USER        HTTP auth username (default: admin)
  AMCREST_PASS        HTTP auth password
  PTZ_HOST_{n}        Override host for camera n   (0-based)
  PTZ_USER_{n}        Override user for camera n
  PTZ_PASS_{n}        Override password for camera n
  PTZ_CHANNEL_{n}     NVR channel number for camera n (default: n+1)
  PTZ_SPEED           Default movement speed 1–8 (default: 4)
  PTZ_PORT            HTTP port for PTZ CGI (default: 80)
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_HOST  = os.getenv("AMCREST_HOST", "")
_DEFAULT_USER  = os.getenv("AMCREST_USER", "admin")
_DEFAULT_PASS  = os.getenv("AMCREST_PASS", "")
_DEFAULT_SPEED = int(os.getenv("PTZ_SPEED", "4"))
_PTZ_PORT      = os.getenv("PTZ_PORT", "80")

# Maps frontend direction names → Amcrest/Dahua CGI PTZ codes
DIRECTIONS: dict[str, str] = {
    "up":         "Down",
    "down":       "Up",
    "left":       "Right",
    "right":      "Left",
    "up_left":    "LeftUp",
    "up_right":   "RightUp",
    "down_left":  "LeftDown",
    "down_right": "RightDown",
    "zoom_in":    "ZoomTele",
    "zoom_out":   "ZoomWide",
}


def _host(camera_id: int) -> str:
    return os.getenv(f"PTZ_HOST_{camera_id}", _DEFAULT_HOST)


def _creds(camera_id: int) -> tuple[str, str]:
    return (
        os.getenv(f"PTZ_USER_{camera_id}", _DEFAULT_USER),
        os.getenv(f"PTZ_PASS_{camera_id}", _DEFAULT_PASS),
    )


def _channel(camera_id: int) -> int:
    return int(os.getenv(f"PTZ_CHANNEL_{camera_id}", str(camera_id + 1)))


def _cgi(camera_id: int) -> str:
    host = _host(camera_id)
    return f"http://{host}:{_PTZ_PORT}/cgi-bin/ptz.cgi"


def ptz_move(camera_id: int, direction: str, speed: int | None = None) -> bool:
    """Start moving camera in direction. Returns True on success."""
    code = DIRECTIONS.get(direction)
    if not code:
        logger.warning("PTZ: unknown direction %r", direction)
        return False
    host = _host(camera_id)
    if not host:
        logger.warning("PTZ: no host configured for camera %d (set AMCREST_HOST or PTZ_HOST_%d)", camera_id, camera_id)
        return False
    user, pwd = _creds(camera_id)
    ch = _channel(camera_id)
    spd = speed if speed is not None else _DEFAULT_SPEED
    try:
        with httpx.Client(auth=httpx.DigestAuth(user, pwd), timeout=3.0) as c:
            r = c.get(_cgi(camera_id), params={
                "action": "start", "channel": ch,
                "code": code, "arg1": 0, "arg2": spd, "arg3": 0,
            })
            r.raise_for_status()
            logger.debug("PTZ move cam=%d dir=%s code=%s ch=%d speed=%d → %d", camera_id, direction, code, ch, spd, r.status_code)
            return True
    except Exception:
        logger.exception("PTZ move failed cam=%d dir=%s", camera_id, direction)
        return False


def ptz_preset(camera_id: int, preset: int = 1) -> bool:
    """Go to a saved preset position."""
    host = _host(camera_id)
    if not host:
        logger.warning("PTZ: no host configured for camera %d", camera_id)
        return False
    user, pwd = _creds(camera_id)
    ch = _channel(camera_id)
    try:
        with httpx.Client(auth=httpx.DigestAuth(user, pwd), timeout=3.0) as c:
            r = c.get(_cgi(camera_id), params={
                "action": "start", "channel": ch,
                "code": "GotoPreset", "arg1": 0, "arg2": preset, "arg3": 0,
            })
            r.raise_for_status()
            logger.debug("PTZ preset cam=%d preset=%d ch=%d → %d", camera_id, preset, ch, r.status_code)
            return True
    except Exception:
        logger.exception("PTZ preset failed cam=%d preset=%d", camera_id, preset)
        return False


def ptz_stop(camera_id: int, direction: str | None = None) -> bool:
    """Stop camera movement. direction is used to send the matching stop code."""
    code = DIRECTIONS.get(direction or "", "Up")
    host = _host(camera_id)
    if not host:
        return False
    user, pwd = _creds(camera_id)
    ch = _channel(camera_id)
    try:
        with httpx.Client(auth=httpx.DigestAuth(user, pwd), timeout=3.0) as c:
            r = c.get(_cgi(camera_id), params={
                "action": "stop", "channel": ch,
                "code": code, "arg1": 0, "arg2": 0, "arg3": 0,
            })
            r.raise_for_status()
            logger.debug("PTZ stop cam=%d code=%s ch=%d → %d", camera_id, code, ch, r.status_code)
            return True
    except Exception:
        logger.exception("PTZ stop failed cam=%d", camera_id)
        return False
