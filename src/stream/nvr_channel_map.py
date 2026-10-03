"""Startup NVR channel-map verifier.

Queries the Amcrest/Dahua NVR's RemoteDevice CGI for the current
channel -> device-IP assignment and cross-checks it against the
NVR_CHANNEL_<CAM> env vars the web + archiver read.

Pattern: single-source-of-truth reconciliation with fail-fast at the
trust boundary (startup log warnings) and graceful degrade on the
runtime path (never blocks startup or serving; if the NVR is
unreachable we keep the env-declared map and mark it stale).

The concrete failure this catches: a channel silently repurposed on
the NVR OSD (e.g. under_adu_ptz landing on channel 7 after crawlspace
ext was moved) will make Replay pull the wrong footage even though
detectors keep alerting correctly (they read direct-RTSP, not NVR).
Live-vs-recorded split-brain — hard to spot from telemetry, obvious in
this crosscheck.

Called once from wildlife_web startup and clip_archiver startup so
either container surfaces divergence in its own log stream.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Amcrest / Dahua config CGI. Returns key=value lines including
#   table.RemoteDevice[N].Address=<device-ip>
# where N is the 0-based slot index. NVR channel numbering in the RTSP
# path (/cam/playback?channel=X) is 1-based, so channel = N + 1.
_REMOTE_DEVICE_PATH = "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice"

_ENV_CHANNEL_PREFIX = "NVR_CHANNEL_"

# Bogus placeholder Amcrest reports for unwired slots. Filtered out so
# a divergence line isn't emitted for every empty channel.
_EMPTY_ADDRESSES: frozenset[str] = frozenset({"", "0.0.0.0", "1.1.1.1"})


@dataclass(frozen=True)
class NvrChannelMap:
    """Reconciled view of camera_id -> NVR channel -> device IP.

    env_channels: what NVR_CHANNEL_<CAM> declares (camera_id -> channel).
    nvr_devices:  what the NVR currently reports on each slot
                  (channel -> device_ip). Empty when stale=True.
    divergences:  human-readable one-per-mismatch strings for logs.
    stale:        True when the NVR was unreachable and only env values
                  were available. Callers may badge Replay URLs
                  differently in this state if they want to.
    """
    env_channels: dict[str, int]
    nvr_devices: dict[int, str]
    divergences: list[str] = field(default_factory=list)
    stale: bool = False

    def has_mismatches(self) -> bool:
        return bool(self.divergences)


def _collect_env_channels() -> dict[str, int]:
    """Read every NVR_CHANNEL_<CAM> env into a camera_id -> channel map.

    camera_id is lowercased so it matches alerts.camera_id values that
    downstream (web_service._local_clip_path_for, playback URL builder)
    already normalize to lowercase.

    Skips non-integer values with a WARN; empty strings are treated as
    unset per feedback_env_getenv_default_trap.
    """
    out: dict[str, int] = {}
    for key, val in os.environ.items():
        if not key.startswith(_ENV_CHANNEL_PREFIX):
            continue
        val = (val or "").strip()
        if not val:
            continue
        cam = key[len(_ENV_CHANNEL_PREFIX):].lower()
        try:
            out[cam] = int(val)
        except ValueError:
            logger.warning(
                "nvr_channel_map: %s=%r is not an integer, skipping", key, val,
            )
    return out


def _fetch_remote_devices(
    host: str,
    port: int,
    user: str,
    pwd: str,
    timeout_s: float = 5.0,
) -> dict[int, str]:
    """Query Amcrest/Dahua RemoteDevice CGI. Returns channel -> device IP.

    Uses urllib.request + HTTPDigestAuthHandler (stdlib only; the web
    image doesn't ship `requests`). Amcrest firmware >=2019 rejects
    plain Basic on this endpoint, so digest is the only path that
    works. Raises URLError / HTTPError on network/HTTP failure -- caller
    catches and downgrades to stale mode.
    """
    url = f"http://{host}:{port}{_REMOTE_DEVICE_PATH}"

    pw_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pw_mgr.add_password(None, url, user, pwd)
    opener = urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(pw_mgr),
    )
    with opener.open(url, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    channel_ip: dict[int, str] = {}
    pat = re.compile(r"table\.RemoteDevice\[(\d+)\]\.Address=([^\r\n]*)")
    for m in pat.finditer(body):
        idx = int(m.group(1))
        ip = m.group(2).strip()
        if ip not in _EMPTY_ADDRESSES:
            channel_ip[idx + 1] = ip
    return channel_ip


def load_and_verify(
    nvr_host: str | None = None,
    nvr_port: int | None = None,
    nvr_user: str | None = None,
    nvr_pass: str | None = None,
) -> NvrChannelMap:
    """Load env channel declarations, cross-check against the live NVR.

    All parameters default to AMCREST_HOST/PORT/USER/PASS env. Meant to
    be called once at process startup; never raises. Emits a WARN log
    line per divergence and a summary WARN if any were found, so this
    lights up any log-based alerting the operator has wired up.
    """
    env_channels = _collect_env_channels()

    host = (nvr_host or os.getenv("AMCREST_HOST") or "").strip()
    if not host:
        logger.info(
            "nvr_channel_map: AMCREST_HOST unset -- skipping NVR crosscheck. "
            "%d NVR_CHANNEL_* env declarations trusted as-is.",
            len(env_channels),
        )
        return NvrChannelMap(env_channels, {}, [], stale=True)

    try:
        port = int(nvr_port or os.getenv("AMCREST_PORT") or "80")
    except ValueError:
        port = 80
    user = (nvr_user or os.getenv("AMCREST_USER") or "").strip()
    pwd  = (nvr_pass or os.getenv("AMCREST_PASS") or "").strip()

    try:
        nvr_devices = _fetch_remote_devices(host, port, user, pwd)
    except Exception as exc:  # noqa: BLE001 - want network + auth + parse
        logger.warning(
            "nvr_channel_map: NVR crosscheck FAILED at startup (%s: %s) -- "
            "using env values only. Verify http://%s:%d reachable and "
            "AMCREST_USER/PASS correct. Replay URLs may point at stale "
            "channels until this succeeds.",
            type(exc).__name__, exc, host, port,
        )
        return NvrChannelMap(env_channels, {}, [], stale=True)

    divergences: list[str] = []
    for cam, channel in sorted(env_channels.items()):
        nvr_ip = nvr_devices.get(channel)
        if not nvr_ip:
            divergences.append(
                f"{cam}: NVR_CHANNEL_{cam.upper()}={channel} but NVR reports "
                f"no device on that slot (channel unwired or slot empty)"
            )
            continue
        cam_host = (os.getenv(f"CAM_HOST_{cam.upper()}") or "").strip()
        if cam_host and cam_host != nvr_ip:
            divergences.append(
                f"{cam}: NVR_CHANNEL_{cam.upper()}={channel} -> NVR ch{channel} "
                f"records {nvr_ip}, but CAM_HOST_{cam.upper()}={cam_host}. "
                f"Detector reads {cam_host} live; Replay pulls {nvr_ip}. "
                f"Split-brain -- fix NVR channel assignment or update env."
            )

    for msg in divergences:
        logger.warning("nvr_channel_map: %s", msg)

    if divergences:
        logger.warning(
            "nvr_channel_map: %d divergence(s) between env and NVR. "
            "Live-detection and Replay paths are split-brain until fixed.",
            len(divergences),
        )
    else:
        logger.info(
            "nvr_channel_map: verified -- %d camera(s) map to correct NVR "
            "channels, %d slot(s) populated on NVR.",
            len(env_channels), len(nvr_devices),
        )

    return NvrChannelMap(env_channels, nvr_devices, divergences, stale=False)
