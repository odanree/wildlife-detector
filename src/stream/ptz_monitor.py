"""PTZ position monitor — detects when a camera has physically moved
off its home preset via camera-side auto-tracking / manual control.

Motivation: on-camera AI (Reolink AI, Jennov AI, etc.) can pan the
physical camera to track a target. Our pipeline doesn't know the pan
happened; the zone polygon (drawn against the home view) now points
at the wrong region → real animals in the panned view fall outside
the zone → alerts don't fire.

Design: background thread that polls ONVIF GetStatus every N seconds.
Compares reported PanTilt to the home position (learned at startup).
Exposes `is_at_home()` for the pipeline main loop to gate detection.

Same shape as _self_slew_in_transition() but signal-driven from the
camera itself instead of command-driven from our slew controller.

Env:
  PTZ_MONITOR_ENABLED_{cam_id}  '1' to enable per-camera monitoring
  PTZ_MONITOR_INTERVAL_S        Poll cadence (default 5s)
  PTZ_HOME_TOLERANCE            Tolerance for at-home check (default 0.05,
                                ONVIF-normalized units so ~5% of full range)
"""
from __future__ import annotations

import logging
import os
import threading
import time

from src.stream.ptz import ptz_status

logger = logging.getLogger(__name__)


class PtzMonitor:
    """Polls one ONVIF camera's PTZ position; tracks whether it's at
    home preset. Thread-safe reads via is_at_home()."""

    def __init__(self, camera_id: int, poll_interval_s: float = 5.0,
                 tolerance: float = 0.05) -> None:
        """Tolerance is backend-relative:
          - ONVIF: 0.05 = 5% of the -1..1 range (~0.1 total width)
          - Reolink: value is native units; sensible default ~30 for
            pan range 0-3600 (~0.8% of range). Callers should set
            PTZ_HOME_TOLERANCE=30 when using Reolink backend.
        """
        self._camera_id = camera_id
        self._poll_interval = poll_interval_s
        self._tolerance = tolerance
        self._home_pan: float | None = None
        self._home_tilt: float | None = None
        self._current_pan: float | None = None
        self._current_tilt: float | None = None
        self._at_home = True  # optimistic: assume home until proven otherwise
        self._last_probe_ok = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Learn home position + start polling thread."""
        status = ptz_status(self._camera_id)
        if status is None:
            logger.warning(
                "PtzMonitor cam=%d: initial probe failed (no ONVIF or auth) — disabling monitor",
                self._camera_id,
            )
            return
        self._home_pan = status["pan"]
        self._home_tilt = status["tilt"]
        self._current_pan = status["pan"]
        self._current_tilt = status["tilt"]
        self._last_probe_ok = True
        logger.info(
            "PtzMonitor cam=%d: home position learned pan=%.3f tilt=%.3f — polling every %.1fs",
            self._camera_id, self._home_pan, self._home_tilt, self._poll_interval,
        )
        self._thread = threading.Thread(
            target=self._loop,
            name=f"ptz-monitor-{self._camera_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            status = ptz_status(self._camera_id)
            if status is None:
                # Transient failure — retain last-known state, retry next tick.
                self._last_probe_ok = False
                continue
            with self._lock:
                self._current_pan = status["pan"]
                self._current_tilt = status["tilt"]
                self._last_probe_ok = True
                pan_off = abs(status["pan"] - (self._home_pan or 0.0))
                tilt_off = abs(status["tilt"] - (self._home_tilt or 0.0))
                new_at_home = pan_off <= self._tolerance and tilt_off <= self._tolerance
                if new_at_home != self._at_home:
                    logger.info(
                        "PtzMonitor cam=%d: at_home changed %s → %s (pan_off=%.3f tilt_off=%.3f)",
                        self._camera_id, self._at_home, new_at_home, pan_off, tilt_off,
                    )
                self._at_home = new_at_home

    def is_at_home(self) -> bool:
        """True when camera is within tolerance of learned home position.
        Returns True optimistically if last probe failed (better to
        detect than to blank-screen on transient network issues)."""
        with self._lock:
            return self._at_home if self._last_probe_ok else True

    def snapshot(self) -> dict:
        """Diagnostic snapshot for logs/preview."""
        with self._lock:
            return {
                "camera_id": self._camera_id,
                "home_pan": self._home_pan,
                "home_tilt": self._home_tilt,
                "current_pan": self._current_pan,
                "current_tilt": self._current_tilt,
                "at_home": self._at_home,
                "last_probe_ok": self._last_probe_ok,
            }


# Module-level singleton — pipeline imports and calls at_home() per frame.
_monitor: PtzMonitor | None = None


def init_monitor_for_current_camera() -> None:
    """Called once at pipeline startup. Reads env to decide whether to
    enable monitoring for the current CAMERA_ID; no-op if disabled."""
    global _monitor
    cam_str = os.getenv("CAMERA_ID", "")
    # Map CAMERA_ID to numeric id for ptz.py's PTZ_* env lookup.
    cam_id_env = os.getenv("PTZ_MONITOR_CAMERA_ID_NUM")
    if cam_id_env is None:
        logger.info("PtzMonitor: no PTZ_MONITOR_CAMERA_ID_NUM set — monitor disabled")
        return
    try:
        cam_num = int(cam_id_env)
    except ValueError:
        logger.warning("PtzMonitor: PTZ_MONITOR_CAMERA_ID_NUM=%r not an int — monitor disabled", cam_id_env)
        return
    if os.getenv(f"PTZ_MONITOR_ENABLED_{cam_num}", "0") != "1":
        logger.info("PtzMonitor cam=%d: not enabled (PTZ_MONITOR_ENABLED_%d != 1)", cam_num, cam_num)
        return
    interval = float(os.getenv("PTZ_MONITOR_INTERVAL_S", "5.0"))
    tolerance = float(os.getenv("PTZ_HOME_TOLERANCE", "0.05"))
    _monitor = PtzMonitor(cam_num, poll_interval_s=interval, tolerance=tolerance)
    _monitor.start()


def is_camera_at_home() -> bool:
    """Called by pipeline main loop. True when camera is at home or
    monitor is disabled (fail-open — don't gate detection when we
    have no signal)."""
    return _monitor.is_at_home() if _monitor is not None else True
