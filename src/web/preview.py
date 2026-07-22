"""Minimal live-preview + status + zone-editor endpoints for the detector.

Not a dashboard — just the three things that make the preview useful:
  1. A live annotated MJPEG stream                    (/, /stream, /snapshot)
  2. Read-only status strip                            (/status → JSON)
  3. In-browser zone polygon editor                    (/api/zone GET/POST)

The pipeline writes into three thread-safe holders (LatestFrame, Stats,
ZoneHolder); this Flask app reads them. Enable with PREVIEW_ENABLED=true.
"""
from __future__ import annotations

import collections
import copy
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

import yaml
from flask import Flask, Response, jsonify, request, send_from_directory, abort

logger = logging.getLogger(__name__)


# ── Latest-frame holder ─────────────────────────────────────────────────────

class LatestFrame:
    """Thread-safe holder for the most recent annotated JPEG."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._jpeg: bytes = b""
        self._version: int = 0

    def set(self, jpeg: bytes) -> None:
        with self._cond:
            self._jpeg = jpeg
            self._version += 1
            self._cond.notify_all()

    def get_next(self, last_seen: int, timeout: float = 5.0) -> tuple[bytes, int]:
        with self._cond:
            self._cond.wait_for(lambda: self._version > last_seen, timeout=timeout)
            return self._jpeg, self._version


_latest = LatestFrame()
_latest_raw = LatestFrame()   # unannotated frame — used for baseline capture


def publish_frame(jpeg: bytes) -> None:
    _latest.set(jpeg)


def publish_raw_frame(jpeg: bytes) -> None:
    """Called by the pipeline with the raw (pre-annotation) JPEG each frame.
    The baseline capture endpoint pulls from this holder — we want a clean
    reference image, not one with overlays baked in."""
    _latest_raw.set(jpeg)


# ── Baseline holder ─────────────────────────────────────────────────────────

class Baseline:
    """Thread-safe holder for TWO 'known-clean' reference frames — day + night.

    The day/night distinction matters because the camera switches between full
    color (day) and IR grayscale (night). A single baseline is useless across
    that transition — the pixel-diff pre-filter can't tell 'empty' from 'moved'
    when the color mode itself has changed.

    Capture always writes to the slot matching the current frame's brightness
    (auto-detected via mean grayscale). Pipeline reads snapshot_bytes(mode)
    where mode is inferred from the current frame each iteration.

    Persisted per-mode: data/baseline_day.jpg + data/baseline_night.jpg.
    Legacy single 'baseline.jpg' at the original path is loaded into whichever
    slot matches its brightness on first startup — no manual migration needed.
    """

    def __init__(self, base_path: str) -> None:
        self._lock = threading.Lock()
        self._base_path = Path(base_path)
        # Two slots: day + night. Keys are 'day' | 'night'.
        self._paths = {
            "day":   self._base_path.parent / (self._base_path.stem + "_day.jpg"),
            "night": self._base_path.parent / (self._base_path.stem + "_night.jpg"),
        }
        self._jpegs: dict[str, bytes] = {"day": b"", "night": b""}
        self._ts:    dict[str, float] = {"day": 0.0, "night": 0.0}
        self._version = 0
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        # Try per-mode files first, then fall back to the legacy single file.
        for mode, path in self._paths.items():
            if path.exists():
                try:
                    self._jpegs[mode] = path.read_bytes()
                    self._ts[mode] = path.stat().st_mtime
                    logger.info("Baseline[%s]: loaded %d bytes from %s",
                                mode, len(self._jpegs[mode]), path)
                except Exception:
                    logger.exception("Baseline[%s]: failed to load %s", mode, path)
        # Legacy single-baseline migration
        if not any(self._jpegs.values()) and self._base_path.exists():
            try:
                jpeg = self._base_path.read_bytes()
                mode = _detect_brightness_mode(jpeg)
                self._jpegs[mode] = jpeg
                self._ts[mode] = self._base_path.stat().st_mtime
                self._paths[mode].write_bytes(jpeg)
                logger.info("Baseline: migrated legacy baseline → %s slot", mode)
            except Exception:
                logger.exception("Baseline: legacy load failed")
        if any(self._jpegs.values()):
            self._version = 1

    def snapshot(self) -> dict:
        """Return metadata for both slots (for the UI status strip)."""
        with self._lock:
            return {
                "exists":  any(bool(v) for v in self._jpegs.values()),
                "version": self._version,
                "day":  {"exists": bool(self._jpegs["day"]),   "ts": self._ts["day"],   "bytes": len(self._jpegs["day"])},
                "night":{"exists": bool(self._jpegs["night"]), "ts": self._ts["night"], "bytes": len(self._jpegs["night"])},
            }

    def snapshot_bytes(self, mode: str = "auto",
                       current_frame_jpeg: bytes | None = None) -> tuple[bytes, int, str]:
        """Return (jpeg, version, mode_used). If mode='auto', pick based on
        the current frame's brightness; otherwise use the explicit slot."""
        if mode == "auto":
            mode = _detect_brightness_mode(current_frame_jpeg) if current_frame_jpeg else "day"
        with self._lock:
            return self._jpegs.get(mode, b""), self._version, mode

    def capture(self, jpeg: bytes, mode: str | None = None) -> str:
        """Save the current frame as the baseline reference.

        mode: 'day' | 'night' | None
          - Explicit mode bypasses the brightness auto-picker. Necessary for
            rooftop / overhead cameras where bright IR-lit foliage confuses
            the day/night detector.
          - None uses brightness auto-detect (single-camera default).
        Returns which slot was written to.
        """
        if not jpeg:
            raise ValueError("empty JPEG — no live frame available yet")
        if mode not in ("day", "night"):
            mode = _detect_brightness_mode(jpeg)
        with self._lock:
            self._paths[mode].parent.mkdir(parents=True, exist_ok=True)
            self._paths[mode].write_bytes(jpeg)
            self._jpegs[mode] = jpeg
            self._ts[mode] = time.time()
            self._version += 1
            logger.info("Baseline[%s]: captured %d bytes → %s (v=%d)",
                        mode, len(jpeg), self._paths[mode], self._version)
        return mode

    def clear(self, mode: str | None = None) -> None:
        """Clear one slot (mode='day' or 'night') or both (mode=None)."""
        modes = [mode] if mode in ("day", "night") else ["day", "night"]
        with self._lock:
            for m in modes:
                try:
                    if self._paths[m].exists():
                        self._paths[m].unlink()
                except Exception:
                    logger.exception("Baseline[%s]: failed to delete %s", m, self._paths[m])
                self._jpegs[m] = b""
                self._ts[m] = 0.0
            self._version += 1
            logger.info("Baseline: cleared %s", modes)


# Brightness threshold that distinguishes day (color, bright) from night (IR).
# Grayscale mean > 100 → day; else night. Adjust via env if your camera's IR
# night mode looks brighter than typical (60-90 range is common).
_DAY_NIGHT_THRESHOLD = int(os.getenv("DAY_NIGHT_BRIGHTNESS_THRESHOLD", "100"))


def _detect_brightness_mode(jpeg: bytes) -> str:
    """Return 'day' or 'night' from a JPEG's mean grayscale brightness."""
    if not jpeg:
        return "day"
    try:
        import numpy as _np
        arr = _np.frombuffer(jpeg, dtype=_np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return "day"
        return "day" if img.mean() >= _DAY_NIGHT_THRESHOLD else "night"
    except Exception:
        return "day"




_baseline: Baseline | None = None


def get_baseline() -> Baseline | None:
    return _baseline


def init_baseline(path: str) -> Baseline:
    global _baseline
    _baseline = Baseline(path)
    return _baseline


# ── Anthropic price sheet (USD per 1M tokens) ──────────────────────────────
# Keep near Stats.record_vlm_tokens where it's actually consumed. Update
# whenever Anthropic changes rate card. Sonnet 4.5 shares Sonnet-5 pricing;
# _default is the "no model matched" fallback and uses Sonnet rates so we
# never under-report a config mistake.
_MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 1.0,  "cache_read": 0.10, "cache_write": 1.25,  "output": 5.0},
    "claude-sonnet-5":           {"input": 3.0,  "cache_read": 0.30, "cache_write": 3.75,  "output": 15.0},
    "claude-sonnet-4-5":         {"input": 3.0,  "cache_read": 0.30, "cache_write": 3.75,  "output": 15.0},
    "claude-opus-4-8":           {"input": 15.0, "cache_read": 1.50, "cache_write": 18.75, "output": 75.0},
    "_default":                  {"input": 3.0,  "cache_read": 0.30, "cache_write": 3.75,  "output": 15.0},
}


# ── Stats holder ────────────────────────────────────────────────────────────

class Stats:
    """Rolling stats snapshot for the status strip."""

    def __init__(self, window: int = 60) -> None:
        self._lock = threading.Lock()
        self._frame_ts: collections.deque[float] = collections.deque(maxlen=window)
        self._alerts: int = 0
        self._start_ts = time.time()
        self._backend = "unknown"
        self._camera = "unknown"                          # display name (RTSP URL etc.)
        self._camera_id = os.getenv("CAMERA_ID", "yard")  # short identifier for the alerts table
        self._zone_key = os.getenv("ZONE_KEY", "yard_zone")  # which polygon this detector uses
        self._detection_size = (0, 0)
        self._last_alert: dict | None = None
        # Self-monitoring — the detector reports its OWN process CPU/RSS so the
        # UI can chart resource spikes against motion-event bursts. psutil is a
        # soft dep; the strip degrades to N/A when unavailable.
        try:
            import psutil  # noqa: F401
            self._psutil = psutil
            self._proc = psutil.Process()
            # Prime the CPU counter — the first call always returns 0.0.
            self._proc.cpu_percent(interval=None)
            self._n_cpu = psutil.cpu_count(logical=True) or 1
        except ImportError:
            self._psutil = None
            self._proc = None
            self._n_cpu = 1
        self._cpu_peak = 0.0
        self._rss_peak_mb = 0.0
        # Gate funnel counters — process-local, reset on restart.
        # Answers "is the VLM silent because there's no motion, or because the
        # baseline pre-filter is eating everything?" — read them via /status.
        self._motion_events = 0        # MOG2 fired at least once this frame
        # Kinematic-gate rejects — counted before motion_events (a
        # rejected blob never becomes a "motion event"). Split by gate
        # so the operator can see which one is doing the work and tune
        # each threshold independently. Reset on restart, session-local.
        self._motion_velocity_rejected = 0
        self._motion_persistence_rejected = 0
        self._zone_events = 0          # at least one motion/YOLO det landed in zone
        self._baseline_filtered = 0    # zone det skipped VLM (pixel-diff below threshold)
        self._vlm_calls = 0            # VLM invocation submitted
        self._vlm_rejected = 0         # VLM returned wildlife_detected=False
        self._vlm_insect_classified = 0  # VLM species='insect' — count-only, no alert fires
        self._vlm_confirmed_session = 0  # VLM confirmed a wildlife event THIS session
                                        # (distinct from self._alerts which is DB-seeded lifetime total)
        # ── Token + cost tracking ─────────────────────────────────────────
        # Accumulates across the process lifetime (resets on restart). Cost
        # estimate is per-model rate-carded, updated on every claude call
        # via record_vlm_tokens(). Cache-hit rate is the observability
        # signal for "is prompt caching actually working" — should stay
        # near 1.0 after the first call in a 5-min cache TTL window.
        self._vlm_tokens_input = 0
        self._vlm_tokens_cache_read = 0
        self._vlm_tokens_cache_create = 0
        self._vlm_tokens_output = 0
        self._vlm_cost_usd = 0.0

    def record_frame(self) -> None:
        with self._lock:
            self._frame_ts.append(time.monotonic())

    def record_motion(self, count: int = 1) -> None:
        with self._lock:
            self._motion_events += count

    def record_motion_kinematic_rejected(self, kind: str, count: int = 1) -> None:
        """kind: 'velocity' or 'persistence'. Silently ignore unknown
        kinds so we can add more gate flavors later without a hard
        error on stale writer code."""
        with self._lock:
            if kind == "velocity":
                self._motion_velocity_rejected += count
            elif kind == "persistence":
                self._motion_persistence_rejected += count

    def record_zone_motion(self, count: int = 1) -> None:
        with self._lock:
            self._zone_events += count

    def record_baseline_filtered(self) -> None:
        with self._lock:
            self._baseline_filtered += 1

    def record_vlm_call(self) -> None:
        with self._lock:
            self._vlm_calls += 1

    def record_vlm_rejected(self) -> None:
        with self._lock:
            self._vlm_rejected += 1

    def record_vlm_insect(self) -> None:
        """Count-only signal for VLM species='insect' verdicts. Insects
        are classified but never fire alerts (moth/wasp/fly are the #1
        night-time FP source for rodent detection; separating them from
        the rodent pipeline keeps rat/mouse alerts clean without spamming
        the alerts page with hundreds of moth rows)."""
        with self._lock:
            self._vlm_insect_classified += 1

    def record_vlm_tokens(self, model: str, input_tok: int, cache_read: int,
                          cache_create: int, output_tok: int) -> None:
        """Accumulate token counts and estimated cost for a completed VLM call.

        Pricing (USD per 1M tokens, Anthropic rate card):
          Sonnet-5:  input $3    / cache-read $0.30 / cache-write $3.75 / output $15
          Haiku-4.5: input $1    / cache-read $0.10 / cache-write $1.25 / output $5
          Opus-4.8:  input $15   / cache-read $1.50 / cache-write $18.75 / output $75
        Unknown model → assume Sonnet rates (conservative — most likely to be
        chosen by operator, avoids under-reporting).
        """
        rates_per_m = _MODEL_PRICING.get(model, _MODEL_PRICING["_default"])
        cost = (
            input_tok    * rates_per_m["input"]      +
            cache_read   * rates_per_m["cache_read"] +
            cache_create * rates_per_m["cache_write"] +
            output_tok   * rates_per_m["output"]
        ) / 1_000_000
        with self._lock:
            self._vlm_tokens_input        += input_tok
            self._vlm_tokens_cache_read   += cache_read
            self._vlm_tokens_cache_create += cache_create
            self._vlm_tokens_output       += output_tok
            self._vlm_cost_usd            += cost

    def record_alert(self, species: str, confidence: float, description: str,
                     snapshot: str | None = None,
                     track_id: int | None = None,
                     yolo_conf: float | None = None) -> None:
        with self._lock:
            self._alerts += 1
            self._vlm_confirmed_session += 1
            self._last_alert = {
                "species":     species,
                "confidence":  round(float(confidence), 3),
                "description": description,
                "ts":          time.time(),
                "snapshot":    snapshot,
                "camera_id":   self._camera_id,
            }
            camera_id = self._camera_id
        # Also push into the durable ring buffer for /alerts.
        _alerts.append(species, confidence, description,
                       snapshot=snapshot, track_id=track_id, yolo_conf=yolo_conf,
                       camera_id=camera_id)

    def set_backend(self, backend: str) -> None:
        with self._lock:
            self._backend = backend

    def set_camera(self, camera: str) -> None:
        with self._lock:
            self._camera = camera

    def set_detection_size(self, w: int, h: int) -> None:
        with self._lock:
            self._detection_size = (w, h)

    def seed_from_state(self, state: "StateDB") -> None:
        """Called once after StateDB opens — seeds the alerts_total counter
        and last_alert dict from persisted rows so /status reflects
        history across restarts, not just this session."""
        total = state.total_alerts()
        latest = state.latest_alert()
        with self._lock:
            self._alerts = total
            if latest:
                self._last_alert = {
                    "species":     latest.get("species"),
                    "confidence":  latest.get("confidence"),
                    "description": latest.get("description"),
                    "ts":          latest.get("ts"),
                    "snapshot":    latest.get("snapshot"),
                }
        logger.info("Stats seeded from DB: alerts_total=%d, last_alert=%s",
                    total, "yes" if latest else "no")

    def snapshot(self) -> dict:
        # Sample psutil OUTSIDE the lock — cpu_percent(interval=None) is fast
        # but non-blocking calls can still race; better to keep the lock scope
        # tight around field reads.
        cpu_pct = 0.0
        rss_mb = 0.0
        threads = 0
        if self._proc is not None:
            try:
                # cpu_percent() returns % of ONE core (0..N*100). Normalize by
                # cpu_count so 100% == "one full core saturated" in operator terms.
                # Peak tracking uses the raw multi-core value for interview parity
                # with `docker stats` (which reports N*100 too).
                _raw = self._proc.cpu_percent(interval=None)
                cpu_pct = _raw
                rss_mb = self._proc.memory_info().rss / (1024 * 1024)
                threads = self._proc.num_threads()
            except Exception:
                pass
        with self._lock:
            fps = 0.0
            if len(self._frame_ts) >= 2:
                span = self._frame_ts[-1] - self._frame_ts[0]
                if span > 0:
                    fps = (len(self._frame_ts) - 1) / span
            if cpu_pct > self._cpu_peak:
                self._cpu_peak = cpu_pct
            if rss_mb > self._rss_peak_mb:
                self._rss_peak_mb = rss_mb
            return {
                "fps":            round(fps, 1),
                "alerts_total":   self._alerts,
                "uptime_seconds": int(time.time() - self._start_ts),
                "backend":        self._backend,
                "camera":         self._camera,
                "camera_id":      self._camera_id,
                "zone_key":       self._zone_key,
                "detection_size": self._detection_size,
                "last_alert":     self._last_alert,
                # Gate funnel — reset on restart. Ratios tell you which stage
                # is doing the filtering work: motion → zone → baseline_pass → vlm → confirmed.
                "gate_funnel": {
                    # Kinematic-gate rejects sit BEFORE motion_events —
                    # they never reach the motion_events counter. Show
                    # them so the operator sees the pre-funnel filtering.
                    "motion_velocity_rejected":    self._motion_velocity_rejected,
                    "motion_persistence_rejected": self._motion_persistence_rejected,
                    "motion_events":     self._motion_events,
                    "zone_events":       self._zone_events,
                    "baseline_filtered": self._baseline_filtered,
                    "vlm_calls":         self._vlm_calls,
                    "vlm_rejected":      self._vlm_rejected,
                    "vlm_insect":        self._vlm_insect_classified,
                    "vlm_confirmed":     self._vlm_confirmed_session,
                },
                # Session-lifetime VLM token + cost tracking. cache_hit_rate
                # is the observability signal for "prompt caching is holding"
                # — should stay near 1.0 after the first call in a 5-min TTL
                # window. Cost is computed via _MODEL_PRICING; unknown models
                # fall back to Sonnet rates.
                "vlm_cost": {
                    "tokens_input":        self._vlm_tokens_input,
                    "tokens_cache_read":   self._vlm_tokens_cache_read,
                    "tokens_cache_create": self._vlm_tokens_cache_create,
                    "tokens_output":       self._vlm_tokens_output,
                    "cost_usd":            round(self._vlm_cost_usd, 4),
                    "cache_hit_rate": round(
                        self._vlm_tokens_cache_read /
                        max(1, self._vlm_tokens_cache_read + self._vlm_tokens_cache_create + self._vlm_tokens_input),
                        3,
                    ),
                },
                # Detector self-monitoring. cpu_pct is multi-core (0..N*100)
                # matching `docker stats`; num_cpus lets the UI show "X% of N cores".
                "resources": {
                    "cpu_pct":      round(cpu_pct, 1),
                    "cpu_peak_pct": round(self._cpu_peak, 1),
                    "num_cpus":     self._n_cpu,
                    "rss_mb":       round(rss_mb, 1),
                    "rss_peak_mb":  round(self._rss_peak_mb, 1),
                    "threads":      threads,
                    "available":    self._proc is not None,
                },
            }


_stats = Stats()
stats = _stats  # public alias


# ── Alert log (in-memory ring buffer) ───────────────────────────────────────

class AlertLog:
    """Persistent alert history backed by SQLite (see src/storage/state_db.py).

    Phase 1 of ADR 002 — this used to be an in-memory ring buffer; now it's a
    thin façade over StateDB so history survives detector restarts. Existing
    callers (Stats.record_alert, /api/alerts) get the same public API.

    The ``capacity`` kwarg is retained for backwards compat but no longer
    applies — SQLite has no ring semantic. Callers that want to cap the
    returned page use ``.list(limit=N)``.
    """

    def __init__(self, capacity: int = 500) -> None:
        # No local state — everything lives in StateDB. The instance still
        # exists as a public API surface for the pipeline.
        self._capacity = capacity   # unused; kept for compat
        self._state: "StateDB | None" = None

    def bind_state(self, state: "StateDB") -> None:
        """Wire this façade to a StateDB. Called by init_alert_log()."""
        self._state = state

    def append(self, species: str, confidence: float, description: str,
               snapshot: str | None = None,
               track_id: int | None = None,
               yolo_conf: float | None = None,
               camera_id: str = "yard") -> None:
        if self._state is None:
            return   # AlertLog wasn't init'd; no-op like before
        self._state.append_alert(
            species=species,
            confidence=confidence,
            description=description,
            snapshot=snapshot,
            track_id=track_id,
            yolo_conf=yolo_conf,
            is_rodent=species in ("rat", "mouse"),
            historical=False,
            camera_id=camera_id,
        )

    def list(self, limit: int = 200, species: str | None = None,
             camera_id: str | None = None) -> list[dict]:
        if self._state is None:
            return []
        return self._state.list_alerts(limit=limit, species=species, camera_id=camera_id)

    def total(self) -> int:
        return 0 if self._state is None else self._state.total_alerts()

    def latest(self) -> dict | None:
        return None if self._state is None else self._state.latest_alert()

    def backfill_from_disk(
        self,
        snapshot_dir: Path,
        from_date: str | None = None,
        to_date: str | None = None,
        camera_id: str | None = None,
        hour_start: int | None = None,
        hour_end: int | None = None,
        tz: str = "America/Los_Angeles",
    ) -> int:
        """Walk snapshots/ (recursively) and INSERT OR IGNORE each JPEG into
        alerts as a historical row. Idempotent — INSERT OR IGNORE keys off
        (ts, species, snapshot) so re-running is safe.

        Optional filters for the labeling-workflow selective backfill:
          from_date / to_date: "YYYY-MM-DD" inclusive; matches against the
                               snapshot directory (snapshots/YYYY-MM-DD/*.jpg).
          camera_id: 'yard' default (historical filename schema predates
                     multi-camera). Pass explicitly for rooftop-era files.
          hour_start / hour_end: hour-of-day filter, inclusive / exclusive.
                     Interpreted in `tz` (default America/Los_Angeles).
                     Wrap-around supported (e.g. 22 → 5 = 10 PM to 5 AM).
                     The filename's HH is UTC (the container writes
                     datetime.now() in a UTC container), so we convert
                     to `tz` before comparing — otherwise a 22-05 filter
                     targets the WRONG window (UTC≠PDT).
          tz:        IANA timezone name; the hour_start/hour_end range is
                     compared against the file's local hour in this zone.
        """
        if self._state is None or not snapshot_dir.exists():
            return 0
        pattern = re.compile(r'^([a-z_]+)_(\d{8})_(\d{6})\.jpg$', re.IGNORECASE)
        rows: list[dict] = []
        scanned = 0
        # Filename time is UTC (containers save datetime.now() in UTC).
        # Operator specifies hour_start/hour_end in LOCAL time (default
        # America/Los_Angeles) — the whole point of nighttime filtering
        # is nocturnal rodent activity in the property's local clock.
        try:
            from zoneinfo import ZoneInfo
            _utc = ZoneInfo("UTC")
            _local = ZoneInfo(tz)
        except Exception:
            logger.warning("backfill tz='%s' invalid; falling back to UTC comparison", tz)
            _utc = None
            _local = None

        def hour_in_range(hour: int) -> bool:
            if hour_start is None and hour_end is None:
                return True
            hs = hour_start if hour_start is not None else 0
            he = hour_end if hour_end is not None else 24
            if hs < he:
                return hs <= hour < he
            # Wrap-around case (e.g. 20 → 6): hour ≥ 20 OR hour < 6.
            return hour >= hs or hour < he

        for f in snapshot_dir.rglob('*.jpg'):
            # Date filter is against the parent dir (snapshots/YYYY-MM-DD/…).
            if from_date or to_date:
                day_dir = f.parent.name  # expect "YYYY-MM-DD"
                if from_date and day_dir < from_date:
                    continue
                if to_date and day_dir > to_date:
                    continue
            m = pattern.match(f.name)
            if not m:
                continue
            scanned += 1
            event_type, date, hms = m.groups()
            try:
                ts = datetime.strptime(f"{date}_{hms}", "%Y%m%d_%H%M%S").timestamp()
                relpath = str(f.relative_to(snapshot_dir)).replace('\\', '/')
            except ValueError:
                continue
            # Hour filter — reject files whose local hour (converted from
            # the filename's UTC time) doesn't fall in the range.
            if hour_start is not None or hour_end is not None:
                try:
                    file_hour_utc = int(hms[:2])
                except (IndexError, ValueError):
                    continue
                if _utc is not None and _local is not None:
                    # Reconstruct the UTC datetime, convert to local zone,
                    # then take that local hour for the range check.
                    from datetime import datetime as _dt
                    try:
                        dt_utc = _dt.strptime(f"{date}_{hms}", "%Y%m%d_%H%M%S").replace(tzinfo=_utc)
                        local_hour = dt_utc.astimezone(_local).hour
                    except ValueError:
                        continue
                    if not hour_in_range(local_hour):
                        continue
                else:
                    if not hour_in_range(file_hour_utc):
                        continue
            rows.append({
                "ts":          ts,
                "species":     event_type,     # e.g. "rodent" — no species detail available
                "confidence":  None,
                "description": "(loaded from disk — details not persisted before this restart)",
                "snapshot":    relpath,
                "track_id":    None,
                "yolo_conf":   None,
                "is_rodent":   1 if event_type == "rodent" else 0,
                "historical":  1,
                "camera_id":   camera_id or "yard",
            })
        inserted = self._state.append_alerts_bulk(rows)
        logger.info("AlertLog: backfill scanned %d JPEGs, %d new rows inserted from %s (from=%s to=%s cam=%s hours=%s-%s)",
                    scanned, inserted, snapshot_dir, from_date, to_date, camera_id, hour_start, hour_end)
        return inserted


_alerts = AlertLog(capacity=500)
_snapshot_dir: Path | None = None
_state_db: "StateDB | None" = None


def get_state_db() -> "StateDB | None":
    return _state_db


def init_alert_log(snapshot_dir: str, capacity: int = 500,
                   db_path: str | None = None) -> None:
    """Open the SQLite state store, backfill historical snapshots, and wire
    the AlertLog façade to it. Called once at pipeline startup.

    db_path defaults to data/state.db — override via STATE_DB_PATH env for
    tests or a non-default location.
    """
    global _alerts, _snapshot_dir, _state_db
    from src.storage.state_db import StateDB
    _snapshot_dir = Path(snapshot_dir).resolve()
    resolved_db_path = db_path or os.getenv("STATE_DB_PATH", "data/state.db")
    _state_db = StateDB(resolved_db_path)
    _alerts = AlertLog(capacity=capacity)
    _alerts.bind_state(_state_db)
    logger.info("AlertLog initialized (snapshots=%s, db=%s)", _snapshot_dir, resolved_db_path)
    # Backfill is a disaster-recovery path — re-imports JPEG snapshots
    # as alert rows when the DB was lost. Running it on every startup
    # was polluting the alerts view: each restart re-added rows for
    # snapshots the operator had already seen or deleted. Gate on
    # "DB is empty" so the recovery still fires cold but idempotent
    # restarts don't churn.
    existing = _state_db.total_alerts()
    if existing == 0:
        _alerts.backfill_from_disk(_snapshot_dir)
    else:
        logger.info(
            "AlertLog: skipping backfill — DB already has %d rows (backfill is disaster-recovery only)",
            existing,
        )
    # Seed the process-local Stats counter from the DB so /status shows the
    # true across-restart alert count on the very first poll.
    _stats.seed_from_state(_state_db)


# ── Zone holder (hot-reload from /api/zone) ─────────────────────────────────

class ZoneHolder:
    """Thread-safe polygon holder + persistence path.

    Pipeline reads .snapshot() each iteration and rebuilds ZoneFilter when the
    version increments. Save writes to the config file on disk AND bumps the
    version so the pipeline picks it up next frame — atomic-enough for this
    single-writer / single-reader setup.

    Coord policy: the polygon is stored on disk as **normalized 0..1 floats**
    when a det_w/det_h is set, so it stays valid when INPUT_WIDTH/HEIGHT change.
    Legacy YAML with absolute pixel coords is auto-detected on load and returned
    to callers unchanged (pipeline scales it based on the same heuristic).
    """

    def __init__(self, config_path: str, zone_key: str,
                 det_w: int | None = None, det_h: int | None = None) -> None:
        self._lock = threading.Lock()
        self._config_path = Path(config_path)
        self._zone_key = zone_key
        self._det_w = det_w   # required to normalize on save; if None, save as pixels
        self._det_h = det_h
        self._polygon: list[tuple[int, int]] = []
        self._version = 0
        self._last_mtime: float = 0.0
        self._reload_from_disk()

    def _reload_from_disk(self) -> None:
        try:
            with self._config_path.open(encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            raw = cfg.get("zones", {}).get(self._zone_key, {}).get("polygon", [])
            # If normalized floats in YAML, scale to pixel space using known dims
            # (fallback 1280×720 if not set). Callers see pixel coords.
            is_norm = all(all(v <= 1.5 for v in p) for p in raw) if raw else False
            if is_norm and self._det_w and self._det_h:
                self._polygon = [(int(round(x * self._det_w)), int(round(y * self._det_h))) for x, y in raw]
            else:
                self._polygon = [(int(x), int(y)) for x, y in raw]
            try:
                self._last_mtime = self._config_path.stat().st_mtime
            except OSError:
                self._last_mtime = 0.0
        except Exception:
            logger.exception("ZoneHolder: failed to load %s", self._config_path)
            self._polygon = []

    def snapshot(self) -> tuple[list[tuple[int, int]], int]:
        """Return (polygon, version). Cheap mtime check on every call —
        if the yaml on disk was modified by something OTHER than
        set_polygon (a git checkout, a manual edit, a backup restore),
        reload and bump the version so the pipeline hot-reload picks
        up the change. Guards against 'file changed under us' desync
        where in-memory polygon and on-disk yaml disagree."""
        with self._lock:
            try:
                mtime = self._config_path.stat().st_mtime
                if mtime > self._last_mtime + 0.01:  # 10ms tolerance for FS quirks
                    logger.info(
                        "ZoneHolder: yaml mtime advanced (%.2f → %.2f), reloading",
                        self._last_mtime, mtime,
                    )
                    self._reload_from_disk()
                    self._version += 1
            except OSError:
                pass  # file gone temporarily during atomic-write swap — skip this tick
            return list(self._polygon), self._version

    def set_polygon(self, polygon: list[tuple[int, int]], persist: bool = True) -> None:
        clean = [(int(x), int(y)) for x, y in polygon]
        if len(clean) < 3:
            raise ValueError("polygon needs at least 3 vertices")
        with self._lock:
            self._polygon = clean
            self._version += 1
            if persist:
                self._persist(clean)

    def _persist(self, polygon: list[tuple[int, int]]) -> None:
        """Rewrite the YAML with the new polygon as NORMALIZED coords when we
        know det_w/det_h — so it stays valid across INPUT_WIDTH/HEIGHT changes.
        Comments are LOST — accept the tradeoff for MVP.
        """
        try:
            with self._config_path.open(encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            if self._det_w and self._det_h:
                # Round to 4 decimal places — enough for sub-pixel precision on
                # any reasonable resolution (1/1280 ≈ 0.00078).
                to_write = [
                    [round(x / self._det_w, 4), round(y / self._det_h, 4)]
                    for x, y in polygon
                ]
            else:
                to_write = [[int(x), int(y)] for x, y in polygon]
            cfg.setdefault("zones", {}).setdefault(self._zone_key, {})["polygon"] = to_write
            tmp = self._config_path.with_suffix(self._config_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=None)
            tmp.replace(self._config_path)
            # Update mtime watermark AFTER our own write so snapshot()'s
            # mtime check doesn't trigger a spurious reload for a
            # change we just made in memory.
            try:
                self._last_mtime = self._config_path.stat().st_mtime
            except OSError:
                pass
            logger.info("ZoneHolder: persisted %d-vertex polygon to %s (format=%s)",
                        len(polygon), self._config_path,
                        "normalized" if self._det_w else "pixel")
        except Exception:
            logger.exception("ZoneHolder: persist failed for %s", self._config_path)


_zones: ZoneHolder | None = None


def get_zones() -> ZoneHolder | None:
    return _zones


# ── OSD-mask holder (same pattern as ZoneHolder, for rectangles) ─────────────

class MaskHolder:
    """Thread-safe list of OSD-mask rectangles + persistence.

    Rectangles are [x1, y1, x2, y2] pixel coords at det_w×det_h. Stored on disk
    as normalized 0..1 floats when det dimensions are known (same policy as
    ZoneHolder). Pipeline polls .snapshot() each frame and rebuilds its
    osd_masks list when the version bumps.
    """

    def __init__(self, config_path: str,
                 det_w: int | None = None, det_h: int | None = None) -> None:
        self._lock = threading.Lock()
        self._config_path = Path(config_path)
        self._det_w = det_w
        self._det_h = det_h
        # Camera-scoped mask storage: yaml supports either a flat legacy list
        # under `osd_masks:` (single-camera deploys) or a dict keyed by
        # camera_id (multi-camera). CAMERA_ID env selects the sub-key when
        # the dict form is present.
        self._camera_id = os.getenv("CAMERA_ID", "yard")
        self._masks: list[tuple[int, int, int, int]] = []
        self._version = 0
        self._last_mtime: float = 0.0
        self._reload_from_disk()

    def _reload_from_disk(self) -> None:
        try:
            with self._config_path.open(encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            osd = cfg.get("osd_masks", []) or []
            # Backwards compat: flat list → yard's masks. Dict → per-camera.
            if isinstance(osd, dict):
                raw = osd.get(self._camera_id, []) or []
            else:
                raw = osd if self._camera_id == "yard" else []
            out: list[tuple[int, int, int, int]] = []
            for m in raw:
                if len(m) != 4:
                    continue
                if all(v <= 1.5 for v in m) and self._det_w and self._det_h:
                    out.append((
                        int(round(m[0] * self._det_w)),
                        int(round(m[1] * self._det_h)),
                        int(round(m[2] * self._det_w)),
                        int(round(m[3] * self._det_h)),
                    ))
                else:
                    out.append(tuple(int(v) for v in m))
            self._masks = out
            try:
                self._last_mtime = self._config_path.stat().st_mtime
            except OSError:
                self._last_mtime = 0.0
        except Exception:
            logger.exception("MaskHolder: failed to load %s", self._config_path)
            self._masks = []

    def snapshot(self) -> tuple[list[tuple[int, int, int, int]], int]:
        """Same mtime-watch pattern as ZoneHolder.snapshot() — pick up
        external yaml edits (git checkout, manual edit, backup restore)
        so in-memory state doesn't drift from what's on disk."""
        with self._lock:
            try:
                mtime = self._config_path.stat().st_mtime
                if mtime > self._last_mtime + 0.01:
                    logger.info(
                        "MaskHolder: yaml mtime advanced (%.2f → %.2f), reloading",
                        self._last_mtime, mtime,
                    )
                    self._reload_from_disk()
                    self._version += 1
            except OSError:
                pass  # atomic-write swap window — skip this tick
            return list(self._masks), self._version

    def set_masks(self, masks: list[list[int]], persist: bool = True) -> None:
        clean: list[tuple[int, int, int, int]] = []
        for m in masks:
            if len(m) != 4:
                continue
            x1, y1, x2, y2 = (int(v) for v in m)
            if x2 <= x1 or y2 <= y1:
                continue
            clean.append((x1, y1, x2, y2))
        with self._lock:
            self._masks = clean
            self._version += 1
            if persist:
                self._persist(clean)

    def _persist(self, masks: list[tuple[int, int, int, int]]) -> None:
        try:
            with self._config_path.open(encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            if self._det_w and self._det_h:
                to_write = [
                    [round(x1 / self._det_w, 4), round(y1 / self._det_h, 4),
                     round(x2 / self._det_w, 4), round(y2 / self._det_h, 4)]
                    for x1, y1, x2, y2 in masks
                ]
            else:
                to_write = [[int(x1), int(y1), int(x2), int(y2)] for x1, y1, x2, y2 in masks]
            # Migrate flat legacy `osd_masks: [...]` to dict form on first write
            # so the other camera's writes never clobber this one.
            existing = cfg.get("osd_masks", {})
            if isinstance(existing, list):
                # Legacy: existing masks belonged to the yard (default single-camera).
                existing = {"yard": existing}
            existing[self._camera_id] = to_write
            cfg["osd_masks"] = existing
            tmp = self._config_path.with_suffix(self._config_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=None)
            tmp.replace(self._config_path)
            try:
                self._last_mtime = self._config_path.stat().st_mtime
            except OSError:
                pass
            logger.info("MaskHolder: persisted %d masks (format=%s)",
                        len(masks), "normalized" if self._det_w else "pixel")
        except Exception:
            logger.exception("MaskHolder: persist failed for %s", self._config_path)


_masks: MaskHolder | None = None


def get_masks() -> MaskHolder | None:
    return _masks


def init_masks(config_path: str,
               det_w: int | None = None, det_h: int | None = None) -> MaskHolder:
    global _masks
    _masks = MaskHolder(config_path, det_w=det_w, det_h=det_h)
    return _masks


def init_zones(config_path: str, zone_key: str,
               det_w: int | None = None, det_h: int | None = None) -> ZoneHolder:
    """Called once by the pipeline at startup. Pass det_w/det_h so the holder
    can normalize polygon coords when persisting — makes the YAML resolution-
    independent so a future INPUT_WIDTH/HEIGHT change doesn't break the zone."""
    global _zones
    _zones = ZoneHolder(config_path, zone_key, det_w=det_w, det_h=det_h)
    return _zones


# Baseline gallery — grid of all camera × mode pairs so the operator can
# eyeball whether each baseline is a clean scene (no wildlife, no wind
# artifacts). Auto-refreshes so a fresh capture shows immediately.


# Inline SVG favicon — overhead rodent silhouette on a night-vision-green
# disc with two tiny bright eyeshine dots. Encapsulates the app's identity:
# overhead camera view + wildlife detection + IR-mode aesthetic. Scales
# cleanly to any browser-tab size without pixelation.
_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    # Dark night-mode disc background
    b'<circle cx="16" cy="16" r="16" fill="#0d1a15"/>'
    # Rodent body from above — tilted ellipse, IR-green fill
    b'<ellipse cx="14" cy="15" rx="5.5" ry="8" fill="#4d9" transform="rotate(-18 14 15)"/>'
    # Curving tail trailing to bottom-right
    b'<path d="M17.5 21.5 Q22 25 25 29" stroke="#4d9" stroke-width="1.8" '
    b'fill="none" stroke-linecap="round"/>'
    # Two bright eyeshine dots at the head end
    b'<circle cx="11.5" cy="9" r="1.4" fill="#fff"/>'
    b'<circle cx="14.5" cy="9" r="1.4" fill="#fff"/>'
    b'</svg>'
)


# ── Flask app ───────────────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        # Detector-side preview server; the operator UI moved to the web
        # sidecar's React app at /react/preview. Return a plaintext hint
        # so a curl to this port still explains what happened.
        return Response(
            "wildlife-detector: detector-side preview. Operator UI is at "
            "the web sidecar's /react/preview.\n",
            mimetype="text/plain",
        )

    @app.get("/status")
    def status():
        return jsonify(_stats.snapshot())

    @app.get("/api/zone")
    def get_zone():
        z = get_zones()
        if z is None:
            return jsonify({"polygon": []})
        poly, ver = z.snapshot()
        return jsonify({"polygon": poly, "version": ver})

    @app.post("/api/zone")
    def post_zone():
        z = get_zones()
        if z is None:
            return jsonify({"error": "zone editor not initialized"}), 503
        body = request.get_json(silent=True) or {}
        poly = body.get("polygon", [])
        try:
            z.set_polygon(poly, persist=True)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        _, ver = z.snapshot()
        return jsonify({"ok": True, "version": ver})

    @app.get("/api/masks")
    def get_masks_api():
        m = get_masks()
        if m is None:
            return jsonify({"masks": []})
        rects, ver = m.snapshot()
        return jsonify({"masks": rects, "version": ver})

    @app.post("/api/masks")
    def post_masks_api():
        m = get_masks()
        if m is None:
            return jsonify({"error": "mask editor not initialized"}), 503
        body = request.get_json(silent=True) or {}
        rects = body.get("masks", [])
        m.set_masks(rects, persist=True)
        _, ver = m.snapshot()
        return jsonify({"ok": True, "version": ver})

    @app.get("/snapshot")
    def snapshot():
        jpeg, _ = _latest.get_next(last_seen=-1, timeout=2.0)
        if not jpeg:
            return Response(b"", status=204)
        return Response(jpeg, mimetype="image/jpeg")

    @app.get("/alerts")
    def alerts_page():
        # Legacy single-process mode (main.py) doesn't ship the React shell
        # (only the containerized web_service.py does). Return a plaintext
        # redirect note so the operator knows where to look — the actual
        # 302 lives on web_service.py's /alerts route.
        from flask import redirect
        return redirect("/react/alerts", code=302)

    @app.get("/baselines")
    def baselines_page():
        # Legacy single-process mode (main.py) doesn't ship the React shell.
        # Redirect for parity with the containerized web sidecar; users on
        # main.py will land in a 404 (React only served by web_service.py)
        # — acceptable degradation for legacy dev-only mode.
        from flask import redirect
        return redirect("/react/baselines", code=302)

    @app.get("/favicon.ico")
    @app.get("/favicon.svg")
    def favicon():
        # Same SVG serves both — browsers accept SVG for /favicon.ico when the
        # content-type is set correctly. One route → one file to update.
        return Response(_FAVICON_SVG, mimetype="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/alerts")
    def api_alerts():
        try:
            limit = min(500, max(1, int(request.args.get("limit", "200"))))
        except ValueError:
            limit = 200
        species_filter = (request.args.get("species") or "").lower().strip() or None
        # Filter + limit push into SQL now that AlertLog is SQLite-backed
        # (Phase 1 of ADR 002). Total is a COUNT(*), not the local stats
        # counter — this makes it correct across restarts.
        items = _alerts.list(limit=limit, species=species_filter)
        return jsonify({
            "total": _alerts.total(),
            "items": items,
        })

    @app.get("/snapshots/<path:filename>")
    def serve_snapshot(filename: str):
        if _snapshot_dir is None:
            abort(404)
        # send_from_directory guards against ../ traversal
        return send_from_directory(_snapshot_dir, filename, max_age=3600)

    @app.get("/api/baseline")
    def api_baseline_meta():
        b = get_baseline()
        if b is None:
            return jsonify({"exists": False})
        return jsonify(b.snapshot())

    @app.post("/api/baseline/capture")
    def api_baseline_capture():
        b = get_baseline()
        if b is None:
            return jsonify({"error": "baseline not initialized"}), 503
        # Pull the most-recent RAW frame (no overlays baked in).
        jpeg, _ = _latest_raw.get_next(last_seen=-1, timeout=2.0)
        try:
            b.capture(jpeg)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, **b.snapshot()})

    @app.post("/api/baseline/clear")
    def api_baseline_clear():
        b = get_baseline()
        if b is None:
            return jsonify({"error": "baseline not initialized"}), 503
        b.clear()
        return jsonify({"ok": True, **b.snapshot()})

    @app.get("/api/baseline.jpg")
    def api_baseline_jpeg():
        """Serve the requested baseline slot. Query ?mode=day or ?mode=night;
        defaults to whichever slot exists (day preferred)."""
        b = get_baseline()
        if b is None:
            return Response(b"", status=404)
        requested = (request.args.get("mode") or "").lower()
        # snapshot_bytes returns (jpeg, version, mode_used) — 3-tuple.
        if requested in ("day", "night"):
            jpeg, _ver, _mode = b.snapshot_bytes(mode=requested)
        else:
            # No mode given → try day first, then night.
            jpeg, _ver, _mode = b.snapshot_bytes(mode="day")
            if not jpeg:
                jpeg, _ver, _mode = b.snapshot_bytes(mode="night")
        if not jpeg:
            return Response(b"", status=404)
        return Response(jpeg, mimetype="image/jpeg")

    @app.get("/stream")
    def stream():
        boundary = b"--frame"

        def generate():
            last_seen = -1
            while True:
                jpeg, ver = _latest.get_next(last_seen, timeout=5.0)
                if not jpeg:
                    time.sleep(0.5)
                    continue
                last_seen = ver
                yield boundary + b"\r\n" \
                      b"Content-Type: image/jpeg\r\n" \
                      b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" \
                      + jpeg + b"\r\n"

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    return app


class _NoGetAccessLogs(logging.Filter):
    """Drop werkzeug INFO records for GET requests (all polling); keep POSTs.

    All state-changing HTTP calls are POST in this app — /api/baseline/capture,
    /api/baseline/clear, /api/zone — so filtering out GETs leaves the useful
    'user did something' access lines intact while killing the /status +
    /api/baseline polling chatter that fires every second.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return '"GET ' not in msg


def start_in_thread(host: str = "0.0.0.0", port: int = 8100) -> None:
    """Start the Flask preview server on a daemon thread."""
    app = create_app()

    # Werkzeug HTTP access-log policy — three modes via env:
    #   PREVIEW_HTTP_LOGS=all   → keep every request (including polling GETs)
    #   PREVIEW_HTTP_LOGS=none  → silence everything (WARNING+)
    #   PREVIEW_HTTP_LOGS=<unset or anything else, default> → hide GETs, keep POSTs
    import os as _os
    mode = _os.getenv("PREVIEW_HTTP_LOGS", "posts").lower()
    wz = logging.getLogger("werkzeug")
    if mode == "all":
        wz.setLevel(logging.INFO)
    elif mode == "none":
        wz.setLevel(logging.WARNING)
    else:
        wz.setLevel(logging.INFO)
        wz.addFilter(_NoGetAccessLogs())

    def _run():
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    t = threading.Thread(target=_run, name="preview-http", daemon=True)
    t.start()
    logger.info("Preview server listening on http://%s:%d", host, port)
