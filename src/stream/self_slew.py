"""Self-slewing PTZ controller — for cameras that both DETECT and PAN.

Different lifecycle from the primary→secondary SlewDispatcher (slew.py):
that module handles the case where camera A detects and commands camera
B to pan. Here, the same physical camera does both. Panning away means
we lose the wide-scene view of the detecting camera, so we need a
snap-back-to-home step after some idle period.

## Lifecycle

    home ── on_positive(bbox) ──▶  zoomed_on_preset(N)
                                          │
                                          │  idle > return_home_after_s
                                          ▼
                                       home

## Coordination hazards this owns

1. **Motion storm during pan**. While the PTZ is physically moving,
   MOG sees the whole frame as motion. The caller checks
   `is_in_transition()` at the top of each loop iteration and skips
   MOG updates until settled. Duration configurable via
   `transition_pause_s` (default 2 s — Amcrest/Dahua presets typically
   complete within 1–2 s).
2. **Preset-return race**. `on_positive()` and `_watchdog()` share
   `self._lock` so they can't both fire snap-to-preset concurrently.
3. **PTZ command failures**. If the HTTP CGI call fails, we roll back
   the lockout so the next detection can retry. Same shape as
   SlewDispatcher.

## Deferred / known gaps

- **Baseline invalidation** when zoomed on non-home preset. The
  baseline was captured at home preset; diffing against it at a
  different preset produces spurious motion. MVP accepts this and
  expects more VLM calls when zoomed. Fix later by either recomputing
  baseline on settle, or maintaining per-preset baselines.
- **Follow behavior**. This is snap-only: pan once to the preset,
  don't try to keep target centered. Follow-mode adds continuous PTZ
  command queueing + latency compensation — bigger surface, deferred
  until snap proves useful.

## Config

Reads `slew.<camera_id>` block from the same detection.yaml the
existing SlewDispatcher parses. Example:

    slew:
      backyard:
        enabled: true
        camera_id: 2                # PTZ command target (env-mapped)
        home_preset: 1
        return_home_after_s: 30
        lockout_seconds: 10
        transition_pause_s: 2.0
        presets:
          - name: gate_corner
            polygon: [[0.1,0.2],[0.4,0.2],[0.4,0.6],[0.1,0.6]]
            preset: 2

Polygon points are in **normalized** (0.0–1.0) frame coordinates so
the same config survives INPUT_WIDTH/HEIGHT changes.

## Env

- SELF_SLEW_ENABLED   Master kill-switch. Only "true" enables.
- CAMERA_ID           The camera_id used to look up the config block.
                      Set per-container in compose.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

import yaml

from src.stream.ptz import ptz_preset
from src.stream.slew import _point_in_polygon

logger = logging.getLogger(__name__)


@dataclass
class SelfSlewPreset:
    """A zone polygon (normalized coords) mapped to a PTZ preset number."""
    name: str
    polygon_norm: list[tuple[float, float]]  # 0.0–1.0 per axis
    preset: int


@dataclass
class SelfSlewConfig:
    enabled: bool = False
    camera_id: int = 2
    home_preset: int = 1
    return_home_after_s: float = 30.0
    lockout_seconds: float = 10.0
    transition_pause_s: float = 2.0
    presets: list[SelfSlewPreset] = field(default_factory=list)


class SelfSlewController:
    """PTZ-on-positive controller with snap-and-return + idle watchdog.

    Lifecycle: home → zoomed (via on_positive) → home (via watchdog).
    Thread-safe: on_positive and the background watchdog share
    self._lock.
    """

    def __init__(self, cfg: SelfSlewConfig) -> None:
        self.cfg = cfg
        self._current_preset = cfg.home_preset
        self._last_positive_ts = 0.0        # monotonic
        self._last_fire: dict = {}          # event_key → monotonic timestamp
        self._transition_until = 0.0        # monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

        if cfg.enabled and cfg.presets:
            self._watchdog_thread = threading.Thread(
                target=self._watchdog, name="self-slew-watchdog", daemon=True,
            )
            self._watchdog_thread.start()
            logger.info(
                "self-slew: enabled cam=%d home=%d presets=%d idle=%.0fs lockout=%.0fs pause=%.1fs",
                cfg.camera_id, cfg.home_preset, len(cfg.presets),
                cfg.return_home_after_s, cfg.lockout_seconds, cfg.transition_pause_s,
            )
        elif cfg.enabled:
            logger.warning(
                "self-slew: enabled but no presets configured — watchdog NOT started",
            )

    # ── Public API ─────────────────────────────────────────────────────────

    def is_in_transition(self) -> bool:
        """True while the camera is physically panning. Caller should
        skip MOG updates during this window."""
        return time.monotonic() < self._transition_until

    def is_at_home_preset(self) -> bool:
        """True when the camera is (nominally) parked at the home preset.
        Used by the pipeline to decide whether to bypass the zone filter
        — while at a non-home preset, the home-anchored zone polygon is
        stale and would drop the target the camera panned toward."""
        return self._current_preset == self.cfg.home_preset

    def on_positive(
        self,
        bbox: tuple[int, int, int, int],
        frame_width: int,
        frame_height: int,
        event_key: tuple,
    ) -> bool:
        """Slew to the zone-matched preset for this bbox center.

        Returns True if the PTZ command was sent, False if suppressed
        (disabled, no matching zone, lockout active, or command failed).
        """
        if not self.cfg.enabled:
            return False
        if not self.cfg.presets:
            return False

        cx = (bbox[0] + bbox[2]) // 2
        cy = (bbox[1] + bbox[3]) // 2
        preset = self._preset_for_point(cx, cy, frame_width, frame_height)
        if preset is None:
            logger.debug("self-slew: no matching zone for center=(%d,%d) — no-op", cx, cy)
            return False

        with self._lock:
            now = time.monotonic()
            # ANY positive in a mapped zone extends the watchdog's idle
            # timer — decoupled from the GotoPreset lockout so a
            # stationary target with a stable track_id can't run out
            # the return_home_after_s window while it's still there.
            # (Was previously nested INSIDE the lockout branch, which
            # meant a rat with a persistent track_id could only refresh
            # the timer every lockout_seconds — theoretically letting
            # the watchdog snap home while the rat was still visible.)
            self._last_positive_ts = now
            last = self._last_fire.get(event_key, 0.0)
            if now - last < self.cfg.lockout_seconds:
                logger.debug(
                    "self-slew: lockout active for %s (%.1fs < %.1fs) — idle-reset only",
                    event_key, now - last, self.cfg.lockout_seconds,
                )
                return False
            self._last_fire[event_key] = now
            # Same-preset short-circuit: don't fire GotoPreset if we're
            # already there. Idle timer already updated above.
            if preset.preset == self._current_preset:
                logger.debug("self-slew: already at preset=%d for zone=%s — idle-reset only",
                             preset.preset, preset.name)
                return False

        ok = ptz_preset(self.cfg.camera_id, preset=preset.preset)
        if ok:
            with self._lock:
                self._current_preset = preset.preset
                self._transition_until = time.monotonic() + self.cfg.transition_pause_s
            logger.info(
                "self-slew: cam=%d preset=%d zone=%s bbox_center=(%d,%d) event=%s",
                self.cfg.camera_id, preset.preset, preset.name, cx, cy, event_key,
            )
        else:
            logger.warning(
                "self-slew: PTZ preset FAILED cam=%d preset=%d zone=%s",
                self.cfg.camera_id, preset.preset, preset.name,
            )
            # Roll back the lockout so the next event can retry.
            with self._lock:
                self._last_fire.pop(event_key, None)
        return ok

    def stop(self) -> None:
        """Signal the watchdog to exit. Called on pipeline shutdown."""
        self._stop.set()

    # ── Internals ──────────────────────────────────────────────────────────

    def _preset_for_point(
        self, x: int, y: int, frame_width: int, frame_height: int,
    ) -> SelfSlewPreset | None:
        """First zone polygon that contains (x, y) — polygons are stored
        in normalized coords and converted to pixel coords per call."""
        for p in self.cfg.presets:
            px_poly = [
                (int(px * frame_width), int(py * frame_height))
                for (px, py) in p.polygon_norm
            ]
            if _point_in_polygon(x, y, px_poly):
                return p
        return None

    def _watchdog(self) -> None:
        """Snap back to home preset after return_home_after_s of no
        positive detections. Fires every 1s; guards against race with
        on_positive() via self._lock."""
        while not self._stop.wait(1.0):
            with self._lock:
                if self._current_preset == self.cfg.home_preset:
                    continue
                idle_for = time.monotonic() - self._last_positive_ts
                if idle_for < self.cfg.return_home_after_s:
                    continue
                target = self.cfg.home_preset

            ok = ptz_preset(self.cfg.camera_id, preset=target)
            if ok:
                with self._lock:
                    self._current_preset = target
                    self._transition_until = time.monotonic() + self.cfg.transition_pause_s
                logger.info(
                    "self-slew: return-home fired cam=%d preset=%d (idle=%.1fs)",
                    self.cfg.camera_id, target, idle_for,
                )
            else:
                logger.warning(
                    "self-slew: return-home PTZ FAILED cam=%d preset=%d — will retry next tick",
                    self.cfg.camera_id, target,
                )
                # Don't update _current_preset on failure; next tick retries.


# ── Config loader + module-level singleton ─────────────────────────────────

def _load_config(config_path: str, camera_id: str) -> SelfSlewConfig:
    """Read slew.<camera_id> block from detection.yaml.

    Returns disabled cfg if the file, block, or `enabled` flag are missing —
    self-slew opt-in per camera; unopinionated by default.
    """
    if not camera_id:
        return SelfSlewConfig(enabled=False)
    try:
        with open(config_path, encoding="utf-8") as fh:
            cfg_all = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return SelfSlewConfig(enabled=False)

    block = (cfg_all.get("slew") or {}).get(camera_id) or {}
    if not block:
        return SelfSlewConfig(enabled=False)

    presets = []
    for p in block.get("presets", []):
        poly = [tuple(pt) for pt in p.get("polygon", [])]
        presets.append(SelfSlewPreset(
            name=p.get("name", "unnamed"),
            polygon_norm=poly,
            preset=int(p.get("preset", 1)),
        ))
    return SelfSlewConfig(
        enabled=bool(block.get("enabled", False)),
        camera_id=int(block.get("camera_id", 2)),
        home_preset=int(block.get("home_preset", 1)),
        return_home_after_s=float(block.get("return_home_after_s", 30.0)),
        lockout_seconds=float(block.get("lockout_seconds", 10.0)),
        transition_pause_s=float(block.get("transition_pause_s", 2.0)),
        presets=presets,
    )


_controller: SelfSlewController | None = None
_controller_lock = threading.Lock()


def get_controller(config_path: str = "config/detection.yaml") -> SelfSlewController:
    """Return the process-wide self-slew controller.

    Config lookup keyed on the container's CAMERA_ID env — each detector
    process only ever slews its own camera, so a per-container singleton
    is correct.
    """
    global _controller
    with _controller_lock:
        if _controller is None:
            camera_id = os.getenv("CAMERA_ID", "")
            cfg = _load_config(config_path, camera_id)
            _controller = SelfSlewController(cfg)
        return _controller


def reset_controller() -> None:
    """Test hook — drop the cached controller so a fresh config is loaded."""
    global _controller
    with _controller_lock:
        if _controller is not None:
            _controller.stop()
        _controller = None


def maybe_self_slew(
    bbox: tuple[int, int, int, int], event_key: tuple,
    frame_width: int, frame_height: int,
) -> bool:
    """Convenience wrapper: gated by SELF_SLEW_ENABLED env var; safe to
    call unconditionally. Config's per-camera `enabled` flag is an
    additional gate."""
    if os.getenv("SELF_SLEW_ENABLED", "false").lower() != "true":
        return False
    try:
        return get_controller().on_positive(
            bbox=bbox, frame_width=frame_width, frame_height=frame_height,
            event_key=event_key,
        )
    except Exception:
        logger.exception("maybe_self_slew failed for event=%s", event_key)
        return False


def is_in_transition() -> bool:
    """Query for the pipeline main loop — skip MOG updates while True."""
    if os.getenv("SELF_SLEW_ENABLED", "false").lower() != "true":
        return False
    try:
        return get_controller().is_in_transition()
    except Exception:
        return False


def is_at_home_preset() -> bool:
    """Query for the pipeline main loop — True when the self-slew
    camera is parked at its configured home preset. Fail-open: returns
    True when self-slew is disabled OR the controller can't be loaded
    (so the pipeline doesn't bypass zone filter unnecessarily)."""
    if os.getenv("SELF_SLEW_ENABLED", "false").lower() != "true":
        return True
    try:
        return get_controller().is_at_home_preset()
    except Exception:
        return True
