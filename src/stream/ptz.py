"""PTZ control for Amcrest/Dahua cameras via HTTP CGI, plus ONVIF
fallback for generic cameras (Jennov, no-name Hikvision-family, etc.)
that don't expose the Dahua CGI namespace.

Commands go over HTTP to the NVR (or individual camera) — NOT over RTSP.

Environment variables:
  AMCREST_HOST            NVR/camera IP or hostname (shared with playback)
  AMCREST_USER            HTTP auth username (default: admin)
  AMCREST_PASS            HTTP auth password
  PTZ_HOST_{n}            Override host for camera n   (0-based)
  PTZ_USER_{n}            Override user for camera n
  PTZ_PASS_{n}            Override password for camera n
  PTZ_CHANNEL_{n}         NVR channel number for camera n (default: n+1)
  PTZ_SPEED               Default movement speed 1-8 (default: 4)
  PTZ_PORT                HTTP port for PTZ CGI (default: 80)
  PTZ_BACKEND_{n}         Which backend for camera n. 'dahua' (default)
                          or 'onvif'. Dahua uses /cgi-bin/ptz.cgi; ONVIF
                          uses /onvif/ptz SOAP with WS-Security digest.
  PTZ_ONVIF_PATH_{n}      ONVIF PTZ service path (default: /onvif/ptz)
  PTZ_ONVIF_PROFILE_{n}   ONVIF media ProfileToken (default: 'MainStream').
                          Discover with GetProfiles if unsure.

Pattern: **strategy pattern for outbound protocol** — same
`ptz_preset(camera_id, preset)` interface, backend selected per camera
via env. Adding a third backend (Hikvision ISAPI, Reolink API) is one
new function + one dispatch clause.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone

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


def _backend(camera_id: int) -> str:
    return os.getenv(f"PTZ_BACKEND_{camera_id}", "dahua").lower()


def _ws_security_header(user: str, pwd: str) -> str:
    """Build a WS-Security UsernameToken header with PasswordDigest.

    Formula: digest = base64( SHA1( nonce_raw + created + password ) ).
    ONVIF-standard auth — most cameras that speak ONVIF accept this.
    """
    nonce = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + pwd.encode()).digest()
    ).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    return (
        '<Security xmlns="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        "<UsernameToken>"
        f"<Username>{user}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</Password>"
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{nonce_b64}</Nonce>"
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"{created}</Created>"
        "</UsernameToken></Security>"
    )


def _ptz_preset_dahua(camera_id: int, preset: int) -> bool:
    """Amcrest/Dahua CGI: GET /cgi-bin/ptz.cgi?action=start&code=GotoPreset."""
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
            logger.debug("PTZ preset (dahua) cam=%d preset=%d ch=%d → %d",
                         camera_id, preset, ch, r.status_code)
            return True
    except Exception:
        logger.exception("PTZ preset (dahua) failed cam=%d preset=%d", camera_id, preset)
        return False


def _ptz_preset_onvif(camera_id: int, preset: int) -> bool:
    """ONVIF PTZ: POST /onvif/ptz SOAP with WS-Security UsernameToken.

    Handles Jennov / no-name Hikvision-family cameras that expose only
    the ONVIF surface (no Dahua/Hikvision proprietary CGI). Preset
    token equals str(preset_num) on all tested devices — cameras
    expose 255 slots even if the user only saved a few.
    """
    host = _host(camera_id)
    if not host:
        logger.warning("PTZ: no host configured for camera %d", camera_id)
        return False
    user, pwd = _creds(camera_id)
    path = os.getenv(f"PTZ_ONVIF_PATH_{camera_id}", "/onvif/ptz")
    profile = os.getenv(f"PTZ_ONVIF_PROFILE_{camera_id}", "MainStream")
    security = _ws_security_header(user, pwd)
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"<s:Header>{security}</s:Header>"
        "<s:Body>"
        '<GotoPreset xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
        f"<ProfileToken>{profile}</ProfileToken>"
        f"<PresetToken>{preset}</PresetToken>"
        "</GotoPreset>"
        "</s:Body></s:Envelope>"
    )
    url = f"http://{host}:{_PTZ_PORT}{path}"
    try:
        r = httpx.post(url, content=body, timeout=3.0, headers={
            "Content-Type": "application/soap+xml; charset=utf-8",
        })
        r.raise_for_status()
        if "Fault" in r.text:
            # 200 OK with SOAP Fault body = auth error or bad preset.
            # Log the reason for debugging.
            import re
            m = re.search(r"<[^:]+:Reason.*?<[^:]+:Text[^>]*>([^<]+)", r.text, re.S)
            logger.warning("PTZ preset (onvif) SOAP Fault cam=%d preset=%d — %s",
                           camera_id, preset, m.group(1) if m else "unknown")
            return False
        logger.debug("PTZ preset (onvif) cam=%d preset=%d profile=%s → %d",
                     camera_id, preset, profile, r.status_code)
        return True
    except Exception:
        logger.exception("PTZ preset (onvif) failed cam=%d preset=%d", camera_id, preset)
        return False


def ptz_preset(camera_id: int, preset: int = 1) -> bool:
    """Go to a saved preset position.

    Dispatches to the backend selected by PTZ_BACKEND_{camera_id} —
    'dahua' (default) or 'onvif'. Returns True on success, False on
    any error (network, auth, unknown preset). Callers should tolerate
    False and roll back their state accordingly (see self_slew.py).
    """
    backend = _backend(camera_id)
    if backend == "onvif":
        return _ptz_preset_onvif(camera_id, preset)
    return _ptz_preset_dahua(camera_id, preset)


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
