"""Slew-to-zone dispatcher for the secondary PTZ camera.

Given a positive-detection bbox in primary-camera coordinates, look up which
zone contains the detection center, then send the secondary Amcrest/Dahua PTZ
camera to the preset saved for that zone.

Pattern: **zone → preset lookup** (MVP). No calibration math — the operator
saves N presets on the secondary in the NVR UI and maps them to N primary-FOV
polygons in ``config/detection.yaml`` under ``slew_presets``. First polygon
that contains the bbox center wins.

Per-event **lockout**: once a slew fires for a given ``event_key`` we suppress
further slews for that key for ``SLEW_LOCKOUT_SECONDS`` (default 10 s) — same
debounce-coalescer idea as a vector-store dedup, but for PTZ commands. Failed
PTZ commands roll back the lockout so a transient failure doesn't drop the
next event.

Env vars:
  SECONDARY_CAMERA_ID   Which PTZ camera_id to command (default: 1).
  SLEW_LOCKOUT_SECONDS  Per-event debounce window (default: 10).
  SLEW_ENABLED          Master kill-switch enforced by the pipeline caller.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

import yaml

from src.stream.ptz import ptz_preset

logger = logging.getLogger(__name__)

_LOCKOUT_DEFAULT_S = 10.0


@dataclass
class ZonePreset:
    name: str
    polygon: list[tuple[int, int]]
    preset: int


@dataclass
class SlewDispatcher:
    """Zone→preset dispatcher with per-event lockout.

    Thread-safe (pipeline harvests VLM jobs on the main loop, but a test or
    admin path could call from another thread).
    """
    zones: list[ZonePreset]
    camera_id: int = 1
    lockout_seconds: float = _LOCKOUT_DEFAULT_S
    _last_fire: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def zone_for_point(self, x: int, y: int) -> ZonePreset | None:
        """Return the first zone polygon that contains (x, y), or None."""
        for z in self.zones:
            if _point_in_polygon(x, y, z.polygon):
                return z
        return None

    def slew_to_bbox(
        self,
        bbox: tuple[int, int, int, int],
        frame_width: int,
        frame_height: int,
        event_key: tuple,
    ) -> bool:
        """Look up the zone for the bbox center and issue GotoPreset.

        Returns True if the PTZ command was sent, False if suppressed
        (no matching zone, lockout active, or command failed).
        """
        del frame_width, frame_height  # reserved for the future homography path
        cx = (bbox[0] + bbox[2]) // 2
        cy = (bbox[1] + bbox[3]) // 2
        zone = self.zone_for_point(cx, cy)
        if zone is None:
            logger.debug("slew: no matching zone for point (%d,%d) — no-op", cx, cy)
            return False

        with self._lock:
            now = time.monotonic()
            last = self._last_fire.get(event_key, 0.0)
            if now - last < self.lockout_seconds:
                logger.debug("slew: lockout active for %s (%.1fs < %.1fs) — no-op",
                             event_key, now - last, self.lockout_seconds)
                return False
            self._last_fire[event_key] = now

        ok = ptz_preset(self.camera_id, preset=zone.preset)
        if ok:
            logger.info("slew: cam=%d preset=%d zone=%s bbox_center=(%d,%d) event=%s",
                        self.camera_id, zone.preset, zone.name, cx, cy, event_key)
        else:
            logger.warning("slew: PTZ preset FAILED cam=%d preset=%d zone=%s",
                           self.camera_id, zone.preset, zone.name)
            # Roll back the lockout so a transient failure doesn't drop the next event.
            with self._lock:
                self._last_fire.pop(event_key, None)
        return ok


def _point_in_polygon(x: int, y: int, poly: list[tuple[int, int]]) -> bool:
    """Ray-casting point-in-polygon."""
    if len(poly) < 3:
        return False
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        ):
            inside = not inside
        j = i
    return inside


def _load_zones_from_config(path: str) -> list[ZonePreset]:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    zones = []
    for entry in cfg.get("slew_presets", []):
        poly = [tuple(pt) for pt in entry.get("polygon", [])]
        zones.append(ZonePreset(
            name=entry.get("name", "unnamed"),
            polygon=poly,
            preset=int(entry.get("preset", 1)),
        ))
    return zones


_dispatcher: SlewDispatcher | None = None
_dispatcher_lock = threading.Lock()


def get_dispatcher(config_path: str = "config/detection.yaml") -> SlewDispatcher:
    """Return a process-wide dispatcher, lazily built from config."""
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is None:
            zones = _load_zones_from_config(config_path)
            _dispatcher = SlewDispatcher(
                zones=zones,
                camera_id=int(os.getenv("SECONDARY_CAMERA_ID", "1")),
                lockout_seconds=float(os.getenv("SLEW_LOCKOUT_SECONDS", str(_LOCKOUT_DEFAULT_S))),
            )
            logger.info("slew: dispatcher initialized cam=%d zones=%d lockout=%.1fs",
                        _dispatcher.camera_id, len(zones), _dispatcher.lockout_seconds)
        return _dispatcher


def reset_dispatcher() -> None:
    """Test hook — drop the cached dispatcher so a fresh config is loaded."""
    global _dispatcher
    with _dispatcher_lock:
        _dispatcher = None


def maybe_slew(bbox: tuple[int, int, int, int], event_key: tuple,
               frame_width: int, frame_height: int) -> bool:
    """Convenience wrapper: gated by SLEW_ENABLED env var; safe to call unconditionally."""
    if os.getenv("SLEW_ENABLED", "false").lower() != "true":
        return False
    try:
        return get_dispatcher().slew_to_bbox(
            bbox=bbox, frame_width=frame_width, frame_height=frame_height, event_key=event_key
        )
    except Exception:
        logger.exception("maybe_slew failed for event=%s", event_key)
        return False
