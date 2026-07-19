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
        self._zone_events = 0          # at least one motion/YOLO det landed in zone
        self._baseline_filtered = 0    # zone det skipped VLM (pixel-diff below threshold)
        self._vlm_calls = 0            # VLM invocation submitted
        self._vlm_rejected = 0         # VLM returned wildlife_detected=False
        self._vlm_confirmed_session = 0  # VLM confirmed a wildlife event THIS session
                                        # (distinct from self._alerts which is DB-seeded lifetime total)

    def record_frame(self) -> None:
        with self._lock:
            self._frame_ts.append(time.monotonic())

    def record_motion(self, count: int = 1) -> None:
        with self._lock:
            self._motion_events += count

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
                    "motion_events":     self._motion_events,
                    "zone_events":       self._zone_events,
                    "baseline_filtered": self._baseline_filtered,
                    "vlm_calls":         self._vlm_calls,
                    "vlm_rejected":      self._vlm_rejected,
                    "vlm_confirmed":     self._vlm_confirmed_session,
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

    def backfill_from_disk(self, snapshot_dir: Path) -> int:
        """Walk snapshots/ (recursively) and INSERT OR IGNORE each JPEG into
        alerts as a historical row. Idempotent — running on every startup
        re-imports any files added while the detector was down without
        duplicating existing rows (uniqueness enforced on (ts, species, snapshot)).
        """
        if self._state is None or not snapshot_dir.exists():
            return 0
        pattern = re.compile(r'^([a-z_]+)_(\d{8})_(\d{6})\.jpg$', re.IGNORECASE)
        rows: list[dict] = []
        for f in snapshot_dir.rglob('*.jpg'):
            m = pattern.match(f.name)
            if not m:
                continue
            event_type, date, hms = m.groups()
            try:
                ts = datetime.strptime(f"{date}_{hms}", "%Y%m%d_%H%M%S").timestamp()
                relpath = str(f.relative_to(snapshot_dir)).replace('\\', '/')
            except ValueError:
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
            })
        inserted = self._state.append_alerts_bulk(rows)
        logger.info("AlertLog: backfill scanned %d JPEGs, %d new rows inserted from %s",
                    len(rows), inserted, snapshot_dir)
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
    _alerts.backfill_from_disk(_snapshot_dir)
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
        except Exception:
            logger.exception("ZoneHolder: failed to load %s", self._config_path)
            self._polygon = []

    def snapshot(self) -> tuple[list[tuple[int, int]], int]:
        with self._lock:
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
        except Exception:
            logger.exception("MaskHolder: failed to load %s", self._config_path)
            self._masks = []

    def snapshot(self) -> tuple[list[tuple[int, int, int, int]], int]:
        with self._lock:
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


# ── HTML ────────────────────────────────────────────────────────────────────

_INDEX_HTML = r"""<!doctype html>
<html>
<head>
  <title>wildlife-detector — live preview</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #0e0e10; color: #ddd; font-family: -apple-system, "Segoe UI", sans-serif; }
    header { display: flex; gap: 24px; padding: 8px 16px; font-size: 13px; border-bottom: 1px solid #2a2a30; background: #16161a; align-items: center; flex-wrap: wrap; }
    header .title { font-weight: 600; }
    header .stat { color: #9aa; font-variant-numeric: tabular-nums; }
    header .stat b { color: #ddd; margin-left: 4px; font-weight: 500; }
    header .backend { color: #6bd; }
    header .backend.mock { color: #d94; }
    header .last-alert { color: #f66; }
    header .toolbar { margin-left: auto; display: flex; gap: 8px; }
    header button { background: #26262c; color: #ddd; border: 1px solid #3a3a40; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
    header button:hover { background: #33333a; }
    header button.active { background: #2a6cbf; border-color: #2a6cbf; }
    header button.warn { background: #4b2020; border-color: #6c3030; }
    #wrap { position: relative; display: inline-block; margin: 0 auto; padding: 0; line-height: 0; }
    /* Secondary pane — always shows the OTHER camera below the main pane.
       Independent zoom; no zone/mask editor overlay (edit on active only). */
    #secondary-wrap { position: relative; display: inline-block; margin: 12px auto 0; padding: 0; line-height: 0; }
    #secondary-header { display: flex; align-items: center; gap: 8px; padding: 4px 8px;
                       background: #1c1c22; border: 1px solid #26262c; border-bottom: none;
                       font-size: 12px; color: #9aa; }
    #secondary-header b { color: #eef; }
    #secondary-header button { padding: 1px 8px; font-size: 12px;
                               background: #26262c; color: #eef; border: 1px solid #444;
                               border-radius: 3px; cursor: pointer; }
    #secondary-header button:hover { background: #303038; }
    #secondary-stream { display: block; height: auto; user-select: none; border: 1px solid #26262c; }
    /* Floating edit-mode banner — position:fixed so it stays visible no matter
       where the user's viewport is scrolled. Only shown while editing zone or
       masks, since those actions live on the primary (top) pane which may be
       off-screen when the user clicked the action button on the bottom pane. */
    #edit-banner {
      position: fixed; top: 0; left: 0; right: 0; z-index: 1000;
      padding: 8px 16px; background: #ffb400; color: #1a1a1e;
      font: bold 13px/1.4 -apple-system, "Segoe UI", sans-serif;
      display: none; align-items: center; gap: 12px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.4);
      cursor: pointer;
    }
    #edit-banner.visible { display: flex; }
    #edit-banner .banner-hint { flex: 1; }
    #edit-banner .banner-scroll { text-decoration: underline; }
    /* Render at native source resolution — no max-width cap. Wider than the
       viewport → horizontal scroll (acceptable on wide displays). Set
       PREVIEW_FIT=contain in .env to fall back to responsive scaling. */
    #stream-img { display: block; height: auto; user-select: none; }
    #zone-svg { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; }
    #zone-svg.editing { pointer-events: auto; cursor: crosshair; }
    /* No fill on the saved polygon — the preview needs to be visible under it.
       Editing mode gets a very faint fill so partial polygons are easier to see. */
    #zone-svg .poly { fill: none; stroke: rgba(0, 200, 255, 0.8); stroke-width: 2; stroke-dasharray: 4 4; }
    #zone-svg .poly.editing { fill: rgba(255, 180, 0, 0.06); stroke: rgba(255, 180, 0, 0.95); stroke-dasharray: none; }
    #zone-svg circle { fill: #ffb400; stroke: #000; stroke-width: 1; cursor: grab; }
    #zone-svg circle:hover { fill: #fff; }
    #zone-svg circle.dragging { cursor: grabbing; }
    #zone-svg .mask { fill: rgba(240, 50, 50, 0.20); stroke: rgba(240, 50, 50, 0.85); stroke-width: 2; }
    #zone-svg .mask.editing { stroke-dasharray: 4 4; }
    #zone-svg .mask-delete { fill: #f22; stroke: #000; stroke-width: 1; cursor: pointer; }
    #zone-svg .mask-delete:hover { fill: #fff; }
    #zone-svg .mask-delete-x { fill: #fff; font-family: sans-serif; font-size: 20px; font-weight: bold;
                               pointer-events: none; text-anchor: middle; dominant-baseline: central; }
    #main { display: flex; flex-direction: column; align-items: center; padding: 12px; overflow-x: auto; }
    footer { padding: 8px 16px; font-size: 12px; color: #666; border-top: 1px solid #2a2a30; text-align: center; }
    kbd { background: #26262c; padding: 1px 6px; border-radius: 3px; font-size: 11px; color: #bbc; }
  </style>
</head>
<body>
  <div id="edit-banner" onclick="window.scrollTo({top: 0, behavior: 'auto'})">
    <span class="banner-hint"><b id="edit-banner-mode">Editing</b> on <b id="edit-banner-camera">—</b> · scroll up to draw and save</span>
    <span class="banner-scroll">↑ Scroll up</span>
  </div>
  <header>
    <span class="title">wildlife-detector</span>
    <span class="stat">camera
      <select id="s-cam-select" style="background:#26262c;color:#eef;border:1px solid #444;border-radius:3px;padding:2px 6px;margin-left:4px;font:inherit">
        <option>loading…</option>
      </select>
    </span>
    <span class="stat">FPS <b id="s-fps">–</b></span>
    <span class="stat">backend <b class="backend" id="s-backend">–</b></span>
    <span class="stat">alerts <b id="s-alerts">–</b></span>
    <span class="stat">uptime <b id="s-uptime">–</b></span>
    <span class="stat">camera <b id="s-camera">–</b></span>
    <span class="last-alert" id="s-last-alert"></span>
    <span class="stat" id="s-baseline">baseline <b>–</b></span>
    <!-- Detector process metrics from Stats.resources (psutil).
         Peak values track since detector startup so the operator can spot
         spikes even if the current reading has settled. Gate funnel counters
         next to them show the pre-VLM filtering ratios. -->
    <span class="stat" title="Detector CPU % / one full core = 100%">
      cpu <b id="s-cpu">–</b><span style="color:#666"> / peak <b id="s-cpu-peak">–</b></span>
    </span>
    <span class="stat" title="Detector RSS memory (MB) — current / peak since start">
      mem <b id="s-mem">–</b><span style="color:#666"> / peak <b id="s-mem-peak">–</b></span>
    </span>
    <span class="stat" title="Gate funnel — motion→zone→baseline-passed→vlm→confirmed. Ratios show which stage is filtering.">
      gate <b id="s-gate" style="font-family:ui-monospace,monospace">– → – → – → – → –</b>
    </span>
    <div class="toolbar">
      <button id="btn-draw-zone"  title="Clear the polygon and draw a new one from scratch">Draw zone</button>
      <button id="btn-tweak-zone" title="Keep the current polygon and edit its vertices">Tweak</button>
      <button id="btn-reset-zone" class="warn" title="Clear all vertices — draw a new polygon from scratch" style="display:none">Clear</button>
      <button id="btn-cancel-zone" title="Discard unsaved changes" style="display:none">Cancel</button>
      <button id="btn-draw-mask"  title="Draw rectangles over OSD text / camera-drawn overlays that should be excluded from detection">Draw OSD mask</button>
      <button id="btn-save-mask"  title="Save the current masks and hot-reload" style="display:none">Save masks</button>
      <button id="btn-cancel-mask" title="Discard mask edits" style="display:none">Cancel</button>
      <button id="btn-baseline"   title="Capture the current frame as the 'known-clean' reference — do this when nothing is in the scene">Capture baseline</button>
      <button id="btn-view-baseline" title="View the current baseline reference image" style="display:none">View</button>
      <button id="btn-clear-baseline" class="warn" title="Delete the baseline (pipeline will fall back to single-frame classification)" style="display:none">Clear baseline</button>
      <button id="btn-snapshot" title="Download the current annotated frame as JPEG">Snapshot</button>
      <a href="/alerts" style="background:#26262c;color:#ddd;border:1px solid #3a3a40;padding:4px 10px;border-radius:4px;font-size:12px;text-decoration:none;">Alerts →</a>
      <span style="border-left:1px solid #3a3a40;padding-left:8px;display:flex;gap:4px;align-items:center;">
        <button onclick="setZoom(-0.1)" title="Shrink preview">−</button>
        <span id="s-zoom" style="color:#9aa;font-size:12px;min-width:40px;text-align:center;">1.00×</span>
        <button onclick="setZoom(0.1)" title="Enlarge preview">+</button>
      </span>
    </div>
  </header>
  <div id="main">
    <div id="wrap">
      <img id="stream-img" src="/stream" alt="live stream" data-original-src="/stream" />
      <!-- viewBox is set at runtime from /status detection_size so the SVG
           coordinate space always matches whatever INPUT_WIDTH/HEIGHT the
           pipeline is running at. Otherwise clicking to draw a polygon would
           map to the wrong pixel space if the user downscales in .env. -->
      <svg id="zone-svg" viewBox="0 0 1280 720" preserveAspectRatio="none"></svg>
    </div>
    <!-- Secondary pane — always shows the OTHER camera stacked below. JS
         swaps its src to whichever camera isn't currently active in the
         header dropdown. Zone/mask editing stays on the primary above.
         Hidden until we've probed the camera roster and confirmed there's
         more than one detector to show. -->
    <div id="secondary-wrap" style="display:none;">
      <div id="secondary-header">
        <b id="secondary-cam-name">—</b>
        <span style="flex:1"></span>
        <!-- Editor actions on the secondary camera. Since only one pane can
             be in edit mode at a time, clicking any of these first promotes
             the secondary → primary, then triggers the corresponding primary
             toolbar button. Preserves the existing editor state machine. -->
        <button onclick="secondaryAction('btn-draw-zone')" title="Draw zone on this camera">Draw zone</button>
        <button onclick="secondaryAction('btn-draw-mask')" title="Draw OSD mask on this camera">Draw OSD mask</button>
        <button onclick="secondaryCaptureBaseline('day')"   title="Capture as DAY baseline (visible-light mode)">Cap day</button>
        <button onclick="secondaryCaptureBaseline('night')" title="Capture as NIGHT baseline (IR mode)">Cap night</button>
        <span style="border-left:1px solid #3a3a40;padding-left:8px;margin-left:4px;">
          <button onclick="setSecondaryZoom(-0.1)" title="Shrink secondary">−</button>
          <span id="secondary-zoom" style="min-width:44px;text-align:center;display:inline-block;">1.00×</span>
          <button onclick="setSecondaryZoom(0.1)" title="Enlarge secondary">+</button>
        </span>
        <button onclick="promoteSecondary()" title="Swap this camera into the primary pane">Swap</button>
      </div>
      <img id="secondary-stream" src="" alt="secondary stream" />
    </div>
  </div>
  <footer id="footer">
    <span id="mode-hint">
      YOLO <span style="color:#4d9">green</span> · motion <span style="color:#cc4">yellow</span> · alert <span style="color:#f66">red</span> · zone <span style="color:#6cf">cyan</span>
    </span>
  </footer>
<script>
(function() {
  // ── Camera selection (multi-camera routing) ───────────────────────────
  // The active camera_id gets appended as ?camera=<id> to every backend
  // request (/status, /stream, /api/zone, /api/masks, /api/baseline*).
  // Persisted in localStorage so a browser refresh keeps your selection.
  let activeCamera = localStorage.getItem('activeCamera') || '';
  function camParam() { return activeCamera ? '?camera=' + encodeURIComponent(activeCamera) : ''; }
  function camAppend(url) {
    if (!activeCamera) return url;
    return url + (url.includes('?') ? '&' : '?') + 'camera=' + encodeURIComponent(activeCamera);
  }
  async function loadCameras() {
    try {
      const r = await fetch('/api/cameras');
      if (!r.ok) return;
      const j = await r.json();
      const sel = document.getElementById('s-cam-select');
      sel.innerHTML = '';
      j.cameras.forEach(id => {
        const opt = document.createElement('option');
        opt.value = id; opt.textContent = id;
        sel.appendChild(opt);
      });
      // Restore saved selection if valid, else use server default.
      if (!j.cameras.includes(activeCamera)) activeCamera = j.default || j.cameras[0] || '';
      sel.value = activeCamera;
      // Stash the roster so dropdown handler + secondary refresh both see it.
      window.__cameraRoster = j.cameras;
      sel.addEventListener('change', () => {
        activeCamera = sel.value;
        localStorage.setItem('activeCamera', activeCamera);
        // Load the zoom preference saved for this specific camera.
        if (window.__reloadZoomForCamera) window.__reloadZoomForCamera();
        // Force stream reload with new camera param — MJPEG connection is sticky.
        const img = document.getElementById('stream-img');
        if (img) img.src = camAppend('/stream') + '&t=' + Date.now();
        // Immediately refetch camera-scoped state so old polygon/masks don't
        // linger visually. loadZone/loadMasks are defined further down but
        // hoisted-referenced here via the module scope.
        if (typeof loadZone   === 'function') loadZone();
        if (typeof loadMasks  === 'function') loadMasks();
        // Secondary pane now shows whichever camera the dropdown didn't pick.
        refreshSecondary(window.__cameraRoster || []);
      });
      // Initial fill of the secondary pane.
      refreshSecondary(j.cameras);
    } catch (e) { console.warn('loadCameras failed', e); }
  }
  // ── Status polling ────────────────────────────────────────────────────
  // Detection resolution — set at first /status poll so the zone editor SVG
  // maps clicks to the right pixel space when INPUT_WIDTH/HEIGHT change.
  let detW = 1280, detH = 720;
  // Display zoom — per-camera because yard and rooftop have different aspect
  // ratios and typical viewing distances (2K rooftop wants smaller factor to
  // fit; 1088x612 yard often wants 1.5x). localStorage keyed by camera_id;
  // a global 'previewZoom' key migrates in as the 'default' fallback on
  // first load so existing users don't lose their preference.
  function zoomKey(camId) { return 'previewZoom:' + (camId || 'default'); }
  function loadZoomFor(camId) {
    const scoped = localStorage.getItem(zoomKey(camId));
    if (scoped !== null) return parseFloat(scoped);
    // Legacy migration: single 'previewZoom' key from before multi-camera.
    return parseFloat(localStorage.getItem('previewZoom') || '1.0');
  }
  let previewZoom = loadZoomFor(activeCamera);
  function applyZoom() {
    const dw = Math.round(detW * previewZoom);
    const dh = Math.round(detH * previewZoom);
    const img  = document.getElementById('stream-img');
    const wrap = document.getElementById('wrap');
    if (img)  { img.style.width  = dw + 'px'; img.style.height = dh + 'px'; }
    if (wrap) { wrap.style.width = dw + 'px'; wrap.style.height = dh + 'px'; }
    const z = document.getElementById('s-zoom');
    if (z) z.textContent = previewZoom.toFixed(2) + '×';
  }
  window.setZoom = function(delta) {
    previewZoom = Math.max(0.5, Math.min(3.0, previewZoom + delta));
    localStorage.setItem(zoomKey(activeCamera), String(previewZoom));
    applyZoom();
  };
  // Called when camera dropdown changes — reload zoom for the new camera.
  window.__reloadZoomForCamera = function() {
    previewZoom = loadZoomFor(activeCamera);
    applyZoom();
  };

  // ── Secondary pane (the OTHER camera, stacked below the main one) ─────
  // Independent zoom persistence keyed by its camera_id, own /status poll
  // for detection_size so the image scale is right when INPUT_WIDTH differs
  // per camera. No zone/mask overlays here — edit those on the primary pane.
  let secondaryCamera = '';
  let secondaryDetW = 1280, secondaryDetH = 720;
  let secondaryZoom = 1.0;
  function applySecondaryZoom() {
    const dw = Math.round(secondaryDetW * secondaryZoom);
    const dh = Math.round(secondaryDetH * secondaryZoom);
    const img = document.getElementById('secondary-stream');
    if (img) { img.style.width = dw + 'px'; img.style.height = dh + 'px'; }
    const z = document.getElementById('secondary-zoom');
    if (z) z.textContent = secondaryZoom.toFixed(2) + '×';
  }
  window.setSecondaryZoom = function(delta) {
    secondaryZoom = Math.max(0.5, Math.min(3.0, secondaryZoom + delta));
    localStorage.setItem(zoomKey(secondaryCamera), String(secondaryZoom));
    applySecondaryZoom();
  };
  window.promoteSecondary = function() {
    // Swap primary ↔ secondary. Same effect as picking the secondary camera
    // from the dropdown, wrapped in a single click for convenience.
    const sel = document.getElementById('s-cam-select');
    if (!sel || !secondaryCamera) return;
    sel.value = secondaryCamera;
    sel.dispatchEvent(new Event('change'));
  };
  window.secondaryCaptureBaseline = async function(modeOverride) {
    // Baseline capture doesn't need editing — just a POST to the right camera.
    // No swap, no scroll jump, no reflow.
    //
    // modeOverride: 'day' | 'night' | undefined
    //   - Force which slot the capture goes into. Auto-picker uses frame
    //     brightness and can misclassify IR-bright foliage as day; passing
    //     the mode explicitly is the reliable way to save the right file.
    if (!secondaryCamera) return;
    let url = '/api/baseline/capture?camera=' + encodeURIComponent(secondaryCamera);
    if (modeOverride === 'day' || modeOverride === 'night') url += '&mode=' + modeOverride;
    try {
      const r = await fetch(url, { method: 'POST' });
      const j = await r.json();
      if (r.ok && j.ok) {
        alert('Captured ' + secondaryCamera + ' baseline (' + (j.captured_mode || 'auto') + ')');
      } else {
        alert('Capture failed: ' + (j.error || r.status));
      }
    } catch (e) { alert('Capture failed: ' + e); }
  };
  window.secondaryAction = async function(targetBtnId) {
    // Draw zone / OSD mask on the secondary camera. The editor is coupled to
    // the primary pane, so we swap camera → wait for the new camera's zone/mask
    // state to fully load → THEN call the editor entry directly. Avoids racing
    // a button click against async /api/zone reloads, which was making the
    // click look like a swap-only no-op when loadZone resolved after btn.click().
    if (!secondaryCamera) return;
    const wasPrimary = activeCamera;
    if (secondaryCamera === wasPrimary) return;
    const main = document.getElementById('main');
    if (main) main.style.minHeight = main.offsetHeight + 'px';

    // Do the promotion synchronously and drive loadZone/loadMasks ourselves
    // so we can await them, rather than relying on the change handler's
    // fire-and-forget calls.
    const sel = document.getElementById('s-cam-select');
    if (sel) sel.value = secondaryCamera;
    activeCamera = secondaryCamera;
    localStorage.setItem('activeCamera', activeCamera);
    if (window.__reloadZoomForCamera) window.__reloadZoomForCamera();
    const img = document.getElementById('stream-img');
    if (img) img.src = camAppend('/stream') + '&t=' + Date.now();
    refreshSecondary(window.__cameraRoster || []);
    if (typeof loadZone === 'function') await loadZone();
    if (typeof loadMasks === 'function') await loadMasks();

    // State is fully loaded — activate the editor directly, no button-click
    // indirection needed.
    if (targetBtnId === 'btn-draw-zone' && window.__enterZoneEdit) {
      window.__enterZoneEdit();
    } else if (targetBtnId === 'btn-draw-mask' && window.__enterMaskEdit) {
      window.__enterMaskEdit();
    }
    // Instant jump to the primary pane's toolbar so the user actually sees
    // the editor now active (button label = "Save zone", cursor = crosshair).
    window.scrollTo({ top: 0, behavior: 'auto' });
    setTimeout(() => { if (main) main.style.minHeight = ''; }, 500);
  };
  function pickSecondaryCamera(cameras, active) {
    // Deterministic: first camera in the roster that isn't the active one.
    return cameras.find(c => c !== active) || '';
  }
  async function refreshSecondary(cameras) {
    if (!cameras || cameras.length < 2) {
      // Single-camera deploy — hide the secondary pane entirely.
      const w = document.getElementById('secondary-wrap');
      if (w) w.style.display = 'none';
      return;
    }
    secondaryCamera = pickSecondaryCamera(cameras, activeCamera);
    if (!secondaryCamera) return;
    secondaryZoom = loadZoomFor(secondaryCamera);
    document.getElementById('secondary-cam-name').textContent = secondaryCamera;
    document.getElementById('secondary-wrap').style.display = '';
    const img = document.getElementById('secondary-stream');
    if (img) img.src = '/stream?camera=' + encodeURIComponent(secondaryCamera) + '&t=' + Date.now();
    // Probe secondary /status for detection_size so the zoom math is right.
    try {
      const r = await fetch('/status?camera=' + encodeURIComponent(secondaryCamera));
      if (r.ok) {
        const s = await r.json();
        if (s.detection_size && s.detection_size[0] && s.detection_size[1]) {
          secondaryDetW = s.detection_size[0];
          secondaryDetH = s.detection_size[1];
        }
      }
    } catch (e) { console.warn('secondary status probe failed', e); }
    applySecondaryZoom();
  }
  async function pollStatus() {
    try {
      const r = await fetch(camAppend('/status'));
      if (!r.ok) return;
      const s = await r.json();
      if (s.detection_size && s.detection_size[0] && s.detection_size[1]) {
        const [w, h] = s.detection_size;
        if (w !== detW || h !== detH) {
          detW = w; detH = h;
          document.getElementById('zone-svg').setAttribute('viewBox', `0 0 ${w} ${h}`);
          applyZoom();
        }
      }
      document.getElementById('s-fps').textContent = s.fps.toFixed(1);
      document.getElementById('s-backend').textContent = s.backend;
      document.getElementById('s-backend').className = 'backend ' + s.backend;
      document.getElementById('s-alerts').textContent = s.alerts_total;
      document.getElementById('s-uptime').textContent = fmtDuration(s.uptime_seconds);
      document.getElementById('s-camera').textContent = s.camera;
      const la = document.getElementById('s-last-alert');
      if (s.last_alert) {
        const ago = Math.max(0, Math.floor(Date.now()/1000 - s.last_alert.ts));
        la.textContent = `⚠ ${s.last_alert.species} ${(s.last_alert.confidence*100).toFixed(0)}% (${ago}s ago)`;
      } else {
        la.textContent = '';
      }
      // Detector process metrics — cpu_pct is multi-core (0..N*100), same as
      // `docker stats`. Show current + peak so a settled reading still lets
      // the operator see prior spikes.
      const res = s.resources || {};
      if (res.available) {
        document.getElementById('s-cpu').textContent      = (res.cpu_pct      || 0).toFixed(0) + '%';
        document.getElementById('s-cpu-peak').textContent = (res.cpu_peak_pct || 0).toFixed(0) + '%';
        document.getElementById('s-mem').textContent      = (res.rss_mb       || 0).toFixed(0) + 'MB';
        document.getElementById('s-mem-peak').textContent = (res.rss_peak_mb  || 0).toFixed(0) + 'MB';
      }
      // Gate funnel — motion → zone → baseline-passed → vlm → confirmed.
      // Session counters, reset on detector restart. Ratios show which stage
      // is doing the filtering work.
      const g = s.gate_funnel || {};
      if (Object.keys(g).length) {
        // baseline-passed = zone_events - baseline_filtered
        const baselinePassed = Math.max(0, (g.zone_events || 0) - (g.baseline_filtered || 0));
        document.getElementById('s-gate').textContent =
          `${g.motion_events||0} → ${g.zone_events||0} → ${baselinePassed} → ${g.vlm_calls||0} → ${g.vlm_confirmed||0}`;
      }
    } catch (e) { /* ignore */ }
  }
  function fmtDuration(sec) {
    const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
    if (h) return `${h}h${m}m`;
    if (m) return `${m}m${s}s`;
    return `${s}s`;
  }
  setInterval(pollStatus, 1000);
  // Load camera roster FIRST so the stream URL below has the ?camera= tag baked in
  // before the browser subscribes to the sticky MJPEG connection.
  loadCameras().then(() => {
    const img = document.getElementById('stream-img');
    if (img && activeCamera) img.src = camAppend('/stream');
    pollStatus();
  });

  // ── Zone editor ───────────────────────────────────────────────────────
  const svg = document.getElementById('zone-svg');
  const btnDraw = document.getElementById('btn-draw-zone');
  const btnTweak = document.getElementById('btn-tweak-zone');
  const btnReset = document.getElementById('btn-reset-zone');
  const btnCancel = document.getElementById('btn-cancel-zone');
  const modeHint = document.getElementById('mode-hint');
  // savedPolygon = last polygon loaded from the server; polygon = working copy.
  // Cancel restores polygon = savedPolygon so unsaved edits disappear.
  let editing = false, polygon = [], savedPolygon = [], dragIdx = -1;
  // Which button entered edit mode — needed to know which button toggles back to Save.
  let editEntry = null;   // 'draw' | 'tweak' | null

  async function loadZone() {
    const r = await fetch(camAppend('/api/zone'));
    if (!r.ok) return;
    const j = await r.json();
    polygon = j.polygon || [];
    savedPolygon = polygon.map(p => [p[0], p[1]]);   // deep copy
    render();
  }
  function render() {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (polygon.length >= 2) {
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
      path.setAttribute('points', polygon.map(p => `${p[0]},${p[1]}`).join(' '));
      path.setAttribute('class', 'poly' + (editing ? ' editing' : ''));
      svg.appendChild(path);
    }
    if (editing) {
      polygon.forEach((p, i) => {
        const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        c.setAttribute('cx', p[0]);
        c.setAttribute('cy', p[1]);
        c.setAttribute('r', 8);
        c.dataset.idx = i;
        c.addEventListener('mousedown', e => {
          e.stopPropagation();
          dragIdx = i;
          c.classList.add('dragging');
        });
        c.addEventListener('contextmenu', e => {
          e.preventDefault();
          if (polygon.length > 3) {
            polygon.splice(i, 1);
            render();
          }
        });
        svg.appendChild(c);
      });
    }
  }
  function svgCoords(evt) {
    const rect = svg.getBoundingClientRect();
    const x = (evt.clientX - rect.left) * (detW / rect.width);
    const y = (evt.clientY - rect.top)  * (detH / rect.height);
    return [Math.round(x), Math.round(y)];
  }
  svg.addEventListener('mousedown', evt => {
    if (!editing || dragIdx !== -1) return;
    // Click on empty area → add vertex
    const p = svgCoords(evt);
    polygon.push(p);
    render();
  });
  svg.addEventListener('mousemove', evt => {
    if (dragIdx === -1) return;
    polygon[dragIdx] = svgCoords(evt);
    render();
  });
  window.addEventListener('mouseup', () => {
    if (dragIdx !== -1) {
      dragIdx = -1;
      render();
    }
  });

  async function saveZone() {
    try {
      const r = await fetch(camAppend('/api/zone'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ polygon }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert('Save failed: ' + (err.error || r.status));
        return false;
      }
      return true;
    } catch (e) {
      alert('Save failed: ' + e.message);
      return false;
    }
  }

  function activeEditButton() {
    return editEntry === 'draw' ? btnDraw : btnTweak;
  }
  function enterEditMode(kind) {
    editing = true;
    editEntry = kind;
    // Swap the entry button to a Save action; hide the other primary button.
    const active = activeEditButton();
    const other  = kind === 'draw' ? btnTweak : btnDraw;
    active.textContent = 'Save zone';
    active.classList.add('active');
    other.style.display = 'none';
    btnReset.style.display = '';
    btnCancel.style.display = '';
    svg.classList.add('editing');
    if (kind === 'draw') {
      polygon = [];   // start from scratch — this is the fix for "legacy vertices stuck"
      render();
      modeHint.innerHTML = 'Click to place vertices in order · drag to move · right-click to remove (min 3)';
    } else {
      modeHint.innerHTML = 'Drag existing vertices to move · click empty area to add · right-click to remove (min 3)';
    }
  }
  function exitEditMode() {
    editing = false;
    btnDraw.textContent  = 'Draw zone';
    btnTweak.textContent = 'Tweak';
    btnDraw.classList.remove('active');
    btnTweak.classList.remove('active');
    btnDraw.style.display = '';
    btnTweak.style.display = '';
    btnReset.style.display = 'none';
    btnCancel.style.display = 'none';
    svg.classList.remove('editing');
    editEntry = null;
    modeHint.innerHTML = 'YOLO <span style="color:#4d9">green</span> · motion <span style="color:#cc4">yellow</span> · alert <span style="color:#f66">red</span> · zone <span style="color:#6cf">cyan</span>';
    if (window.__hideEditBanner) window.__hideEditBanner();
  }

  async function toggleSave() {
    if (!editing) return;
    if (polygon.length < 3) { alert('Need at least 3 vertices'); return; }
    const ok = await saveZone();
    if (!ok) return;
    savedPolygon = polygon.map(p => [p[0], p[1]]);
    exitEditMode();
  }
  btnDraw.addEventListener('click',  () => { if (editing) toggleSave(); else enterEditMode('draw'); });
  btnTweak.addEventListener('click', () => { if (editing) toggleSave(); else enterEditMode('tweak'); });

  btnReset.addEventListener('click', () => {
    // Wipe the working polygon so the user can draw from scratch.
    // NOT persisted until Save zone — Cancel still reverts to savedPolygon.
    polygon = [];
    dragIdx = -1;
    render();
  });

  btnCancel.addEventListener('click', () => {
    // Discard unsaved edits — restore whatever came back from the server.
    polygon = savedPolygon.map(p => [p[0], p[1]]);
    dragIdx = -1;
    render();
    exitEditMode();
  });

  document.getElementById('btn-snapshot').addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = '/snapshot';
    a.download = `wildlife-${Date.now()}.jpg`;
    a.click();
  });

  // ── OSD mask editor ──────────────────────────────────────────────────
  const btnDrawMask   = document.getElementById('btn-draw-mask');
  const btnSaveMask   = document.getElementById('btn-save-mask');
  const btnCancelMask = document.getElementById('btn-cancel-mask');
  let maskEditing = false;
  let masks = [];           // working list of {x1,y1,x2,y2}
  let savedMasks = [];      // last-saved copy for Cancel restore
  let drawStart = null;     // {x, y} while dragging a new rectangle
  let currentDraw = null;   // {x1,y1,x2,y2} preview during drag

  async function loadMasks() {
    try {
      const r = await fetch(camAppend('/api/masks'));
      if (!r.ok) return;
      const j = await r.json();
      masks = (j.masks || []).map(m => ({x1: m[0], y1: m[1], x2: m[2], y2: m[3]}));
      savedMasks = masks.map(m => ({...m}));
      renderAll();
    } catch (e) { /* ignore */ }
  }

  function renderMasks() {
    // Called from renderAll(); appends mask <rect> + delete handles to the SVG.
    const all = maskEditing ? [...masks] : masks;
    for (const m of all) {
      const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      rect.setAttribute('x', Math.min(m.x1, m.x2));
      rect.setAttribute('y', Math.min(m.y1, m.y2));
      rect.setAttribute('width',  Math.abs(m.x2 - m.x1));
      rect.setAttribute('height', Math.abs(m.y2 - m.y1));
      rect.setAttribute('class', 'mask' + (maskEditing ? ' editing' : ''));
      svg.appendChild(rect);
    }
    if (maskEditing) {
      // Delete handles per mask + optional in-progress preview
      masks.forEach((m, i) => {
        const cx = Math.max(m.x1, m.x2);
        const cy = Math.min(m.y1, m.y2);
        const delBg = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        delBg.setAttribute('cx', cx);
        delBg.setAttribute('cy', cy);
        delBg.setAttribute('r', 12);
        delBg.setAttribute('class', 'mask-delete');
        delBg.dataset.maskIdx = i;
        svg.appendChild(delBg);
        const delX = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        delX.setAttribute('x', cx);
        delX.setAttribute('y', cy);
        delX.setAttribute('class', 'mask-delete-x');
        delX.textContent = '×';
        svg.appendChild(delX);
      });
      if (currentDraw) {
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', Math.min(currentDraw.x1, currentDraw.x2));
        rect.setAttribute('y', Math.min(currentDraw.y1, currentDraw.y2));
        rect.setAttribute('width',  Math.abs(currentDraw.x2 - currentDraw.x1));
        rect.setAttribute('height', Math.abs(currentDraw.y2 - currentDraw.y1));
        rect.setAttribute('class', 'mask editing');
        svg.appendChild(rect);
      }
    }
  }

  // Wrap the existing polygon render() to also draw masks
  const _origRender = render;
  window.renderAll = function() {
    _origRender();
    renderMasks();
  };
  render = renderAll;

  // Exposed so the secondary-pane action buttons can trigger edit modes
  // directly after a promote, without depending on button-click round-trips
  // that could race with async /api/zone loads.
  window.__enterZoneEdit = () => { enterEditMode('draw'); showEditBanner('zone'); };
  window.__enterMaskEdit = () => { enterMaskEditMode(); showEditBanner('mask'); };
  function showEditBanner(mode) {
    const b = document.getElementById('edit-banner');
    if (!b) return;
    document.getElementById('edit-banner-mode').textContent =
      mode === 'mask' ? 'Editing OSD mask' : 'Editing zone';
    document.getElementById('edit-banner-camera').textContent = activeCamera || 'primary';
    b.classList.add('visible');
  }
  window.__hideEditBanner = function() {
    const b = document.getElementById('edit-banner');
    if (b) b.classList.remove('visible');
  };

  function enterMaskEditMode() {
    if (editing) exitEditMode();   // exit zone edit if it was open
    maskEditing = true;
    btnDrawMask.style.display = 'none';
    btnSaveMask.style.display = '';
    btnCancelMask.style.display = '';
    svg.classList.add('editing');
    modeHint.innerHTML = 'Drag to draw a mask rectangle · click × to delete · Save to persist';
    renderAll();
  }
  function exitMaskEditMode() {
    maskEditing = false;
    drawStart = null;
    currentDraw = null;
    btnDrawMask.style.display = '';
    btnSaveMask.style.display = 'none';
    btnCancelMask.style.display = 'none';
    if (!editing) svg.classList.remove('editing');
    modeHint.innerHTML = 'YOLO <span style="color:#4d9">green</span> · motion <span style="color:#cc4">yellow</span> · alert <span style="color:#f66">red</span> · zone <span style="color:#6cf">cyan</span> · masks <span style="color:#f66">red-fill</span>';
    if (window.__hideEditBanner) window.__hideEditBanner();
    renderAll();
  }

  btnDrawMask.addEventListener('click', enterMaskEditMode);
  btnCancelMask.addEventListener('click', () => {
    masks = savedMasks.map(m => ({...m}));
    exitMaskEditMode();
  });
  btnSaveMask.addEventListener('click', async () => {
    try {
      const payload = masks.map(m => [
        Math.min(m.x1, m.x2), Math.min(m.y1, m.y2),
        Math.max(m.x1, m.x2), Math.max(m.y1, m.y2),
      ]);
      const r = await fetch(camAppend('/api/masks'), {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({masks: payload}),
      });
      if (!r.ok) { alert('Mask save failed: ' + r.status); return; }
      savedMasks = masks.map(m => ({...m}));
      exitMaskEditMode();
    } catch (e) { alert('Mask save failed: ' + e.message); }
  });

  // Mask drag-to-draw + delete-click, event delegation on the SVG
  svg.addEventListener('mousedown', evt => {
    if (!maskEditing) return;
    // Click on delete handle?
    const del = evt.target.closest('.mask-delete');
    if (del) {
      const idx = parseInt(del.dataset.maskIdx, 10);
      masks.splice(idx, 1);
      renderAll();
      evt.stopPropagation();
      return;
    }
    // Start drawing a rectangle
    const [x, y] = svgCoords(evt);
    drawStart = {x, y};
    currentDraw = {x1: x, y1: y, x2: x, y2: y};
    evt.stopPropagation();  // don't let zone editor eat this
  }, true);   // capture phase — beats zone editor's mousedown
  svg.addEventListener('mousemove', evt => {
    if (!maskEditing || !drawStart) return;
    const [x, y] = svgCoords(evt);
    currentDraw = {x1: drawStart.x, y1: drawStart.y, x2: x, y2: y};
    renderAll();
  });
  window.addEventListener('mouseup', () => {
    if (!maskEditing || !drawStart || !currentDraw) return;
    const w = Math.abs(currentDraw.x2 - currentDraw.x1);
    const h = Math.abs(currentDraw.y2 - currentDraw.y1);
    if (w >= 8 && h >= 8) {   // ignore tiny accidental clicks
      masks.push({...currentDraw});
    }
    drawStart = null;
    currentDraw = null;
    renderAll();
  });

  loadMasks();

  // ── Baseline capture ─────────────────────────────────────────────────
  const btnBaseline      = document.getElementById('btn-baseline');
  const btnViewBaseline  = document.getElementById('btn-view-baseline');
  const btnClearBaseline = document.getElementById('btn-clear-baseline');
  const sBaseline        = document.getElementById('s-baseline').querySelector('b');

  async function refreshBaseline() {
    try {
      const r = await fetch(camAppend('/api/baseline'));
      if (!r.ok) return;
      const j = await r.json();
      const day = j.day || {};
      const night = j.night || {};
      // Show a short status: 'day + night' when both, 'day only' when partial, 'none' when empty.
      if (day.exists && night.exists) {
        const dayAge = fmtDuration(Math.floor(Date.now()/1000 - day.ts));
        const nightAge = fmtDuration(Math.floor(Date.now()/1000 - night.ts));
        sBaseline.textContent = `day ${dayAge} · night ${nightAge}`;
        sBaseline.style.color = '#4d9';
        btnViewBaseline.style.display = '';
        btnClearBaseline.style.display = '';
      } else if (day.exists) {
        sBaseline.textContent = `day only (${fmtDuration(Math.floor(Date.now()/1000 - day.ts))})`;
        sBaseline.style.color = '#dd4';
        btnViewBaseline.style.display = '';
        btnClearBaseline.style.display = '';
      } else if (night.exists) {
        sBaseline.textContent = `night only (${fmtDuration(Math.floor(Date.now()/1000 - night.ts))})`;
        sBaseline.style.color = '#dd4';
        btnViewBaseline.style.display = '';
        btnClearBaseline.style.display = '';
      } else {
        sBaseline.textContent = 'none';
        sBaseline.style.color = '#d94';
        btnViewBaseline.style.display = 'none';
        btnClearBaseline.style.display = 'none';
      }
    } catch (e) { /* ignore */ }
  }
  btnBaseline.addEventListener('click', async () => {
    if (!confirm('Capture the current frame as the clean baseline?\nDo this when NOTHING you care about is in the scene — otherwise the reference will hide real detections.')) return;
    btnBaseline.disabled = true;
    try {
      const r = await fetch(camAppend('/api/baseline/capture'), { method: 'POST' });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) alert('Baseline capture failed: ' + (j.error || r.status));
      else refreshBaseline();
    } finally {
      btnBaseline.disabled = false;
    }
  });
  btnViewBaseline.addEventListener('click', () => {
    window.open('/api/baseline.jpg?t=' + Date.now(), '_blank');
  });
  btnClearBaseline.addEventListener('click', async () => {
    if (!confirm('Delete the baseline? The pipeline will fall back to single-frame classification (more false positives).')) return;
    await fetch(camAppend('/api/baseline/clear'), { method: 'POST' });
    refreshBaseline();
  });
  setInterval(refreshBaseline, 5000);
  refreshBaseline();

  loadZone();
})();
</script>
</body>
</html>
"""


_ALERTS_HTML = r"""<!doctype html>
<html>
<head>
  <title>wildlife-detector — alerts</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #0e0e10; color: #ddd; font-family: -apple-system, "Segoe UI", sans-serif; }
    header { display: flex; gap: 16px; padding: 8px 16px; font-size: 13px; border-bottom: 1px solid #2a2a30; background: #16161a; align-items: center; }
    header a { color: #6bd; text-decoration: none; }
    header a:hover { text-decoration: underline; }
    header .title { font-weight: 600; }
    header .stat { color: #9aa; }
    header .stat b { color: #ddd; margin-left: 4px; }
    #tools { margin-left: auto; display: flex; gap: 8px; align-items: center; }
    #tools select, #tools button {
      background: #26262c; color: #ddd; border: 1px solid #3a3a40;
      padding: 4px 10px; border-radius: 4px; font-size: 12px;
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    thead { position: sticky; top: 0; background: #16161a; border-bottom: 1px solid #2a2a30; }
    th, td { padding: 8px 12px; text-align: left; vertical-align: middle; }
    th { font-weight: 500; color: #9aa; font-size: 12px; }
    tbody tr:hover { background: #191921; }
    tbody tr { border-bottom: 1px solid #1e1e24; }
    tbody tr.historical .species,
    tbody tr.historical .desc,
    tbody tr.historical .track,
    tbody tr.historical .conf { color: #667; }
    .badge-hist { display: inline-block; background: #33333a; color: #aab; font-size: 10px;
                  padding: 1px 5px; border-radius: 3px; margin-left: 6px; vertical-align: middle; }
    .badge-count { display: inline-block; background: #2a6cbf; color: #fff; font-size: 11px;
                  padding: 1px 6px; border-radius: 8px; margin-left: 6px; font-weight: 500; }
    .expand-btn { cursor: pointer; user-select: none; color: #6bd; font-size: 11px;
                  border: none; background: transparent; padding: 2px 4px; }
    .expand-btn:hover { color: #9df; }
    tr.child { background: #131318; }
    tr.child td { padding-top: 4px; padding-bottom: 4px; font-size: 12px; color: #999; }
    tr.child .thumb-cell { padding-left: 32px; }
    tr.child .thumb { width: 96px; }
    .thumb { width: 160px; height: auto; display: block; border-radius: 3px; }
    .thumb-cell { width: 176px; }
    .species { font-weight: 600; }
    .rodent { color: #f66; }
    .other { color: #9c6; }
    .conf { font-variant-numeric: tabular-nums; color: #ddd; }
    .conf-bar { display: inline-block; height: 4px; background: #3a3a40; border-radius: 2px; overflow: hidden; width: 60px; vertical-align: middle; margin-left: 6px; }
    .conf-bar > div { height: 100%; background: linear-gradient(90deg, #f66 0%, #fc6 50%, #6c6 100%); }
    .ts { color: #9aa; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .ts .rel { color: #667; font-size: 11px; margin-left: 4px; }
    .desc { color: #bbc; max-width: 480px; }
    .track { color: #667; font-size: 11px; font-variant-numeric: tabular-nums; }
    #empty { padding: 40px; text-align: center; color: #667; font-size: 14px; }
    footer { padding: 8px 16px; font-size: 12px; color: #667; border-top: 1px solid #2a2a30; text-align: center; }
  </style>
</head>
<body>
  <header>
    <a href="/" class="title" style="text-decoration:none;color:inherit;" title="Back to live preview">wildlife-detector — alerts</a>
    <span class="stat">total <b id="s-count">–</b></span>
    <span class="stat">shown <b id="s-shown">–</b></span>
    <div id="tools">
      <label style="color:#9aa;font-size:12px;">species
        <select id="filter-species">
          <option value="">all</option>
          <option value="rat">rat</option>
          <option value="mouse">mouse</option>
          <option value="raccoon">raccoon</option>
          <option value="opossum">opossum</option>
          <option value="cat">cat</option>
          <option value="dog">dog</option>
          <option value="squirrel">squirrel</option>
          <option value="bird">bird</option>
          <option value="other">other</option>
        </select>
      </label>
      <button id="btn-refresh" title="Refresh now (auto-refreshes every 5s)">Refresh</button>
      <label style="color:#9aa;font-size:12px;"><input type="checkbox" id="auto" checked /> auto</label>
      <label style="color:#9aa;font-size:12px;" title="Collapse consecutive same-track detections into one event row">
        <input type="checkbox" id="group" checked /> group
      </label>
      <a id="btn-close" href="/" title="Back to live preview (Esc)"
         style="background:#26262c;color:#ddd;border:1px solid #3a3a40;padding:4px 10px;border-radius:4px;font-size:14px;text-decoration:none;line-height:1;margin-left:8px;font-weight:600;">×</a>
    </div>
  </header>
  <table id="tbl">
    <thead>
      <tr>
        <th class="thumb-cell">Snapshot</th>
        <th>When</th>
        <th>Species</th>
        <th>Conf</th>
        <th>Description</th>
        <th>Track</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="empty" style="display:none;">No alerts. When one fires, or when the <code>snapshots/</code> folder has JPEGs from a prior session, they'll show up here.</div>
  <footer>Ring buffer capacity 500 · rolls oldest first · JPEGs on disk backfilled at startup (marked <span class="badge-hist">from disk</span> — confidence + description not persisted)</footer>
<script>
const RODENT = new Set(['rat', 'mouse']);
const rowsEl = document.getElementById('rows');
const emptyEl = document.getElementById('empty');
const filterEl = document.getElementById('filter-species');
const autoEl = document.getElementById('auto');
const groupEl = document.getElementById('group');
const countEl = document.getElementById('s-count');
const shownEl = document.getElementById('s-shown');
const GROUP_WINDOW_S = 60;   // consecutive same-track alerts within this window collapse
const expanded = new Set();  // group.head.id → open state

function fmtRelative(ts) {
  const d = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (d < 60) return `${d}s ago`;
  if (d < 3600) return `${Math.floor(d/60)}m ago`;
  if (d < 86400) return `${Math.floor(d/3600)}h ago`;
  return `${Math.floor(d/86400)}d ago`;
}
function fmtTs(ts) {
  const dt = new Date(ts * 1000);
  return dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
       + ' ' + dt.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
function groupItems(items) {
  // items are newest-first (server returns them in that order). Two grouping rules:
  //   LIVE alerts: same track_id + ts diff < GROUP_WINDOW_S → collapse.
  //   HISTORICAL alerts (from disk backfill, no track_id): ts diff < GROUP_WINDOW_S
  //     to the previous historical row → collapse (time-only, best-effort — we lost
  //     track_id when the previous process died, so we can't do better).
  const groups = [];
  for (const a of items) {
    const g = groups[groups.length - 1];
    let canGroup = false;
    if (g && Math.abs(g.head.ts - a.ts) < GROUP_WINDOW_S) {
      const bothHist = g.head.historical && a.historical;
      const bothLive = !g.head.historical && !a.historical
                       && g.head.track_id != null && a.track_id != null
                       && g.head.track_id === a.track_id;
      canGroup = bothHist || bothLive;
    }
    if (canGroup) g.children.push(a);
    else groups.push({ head: a, children: [] });
  }
  return groups;
}

function renderRow(a, extraCls = '', extraSpeciesBadge = '') {
  const isHist = !!a.historical;
  const rowCls = (isHist ? 'historical' : '') + ' ' + extraCls;
  const clsSpecies = isHist ? '' : (RODENT.has(a.species) ? 'rodent' : 'other');
  const confCell = a.confidence != null
    ? `${Math.round(a.confidence * 100)}%<span class="conf-bar"><div style="width:${Math.round(a.confidence * 100)}%"></div></span>`
    : `—`;
  const yolo = a.yolo_conf != null
    ? `<div class="track">YOLO ${Math.round(a.yolo_conf * 100)}%</div>` : '';
  const trackCell = a.track_id != null ? `#${a.track_id}` : '—';
  const speciesText = (a.species || '?')
    + (isHist ? '<span class="badge-hist">from disk</span>' : '')
    + extraSpeciesBadge;
  const thumb = a.snapshot
    ? `<a href="/snapshots/${encodeURIComponent(a.snapshot)}" target="_blank"><img class="thumb" src="/snapshots/${encodeURIComponent(a.snapshot)}" alt="snapshot" loading="lazy" /></a>`
    : `<div style="color:#667;font-size:11px;padding:12px;">no snapshot</div>`;
  return `<tr class="${rowCls.trim()}" data-id="${a.id}">
    <td class="thumb-cell">${thumb}</td>
    <td class="ts">${fmtTs(a.ts)}<span class="rel">${fmtRelative(a.ts)}</span></td>
    <td class="species ${clsSpecies}">${speciesText}</td>
    <td class="conf">${confCell}</td>
    <td class="desc">${(a.description || '').replace(/</g, '&lt;')}</td>
    <td class="track">${trackCell}${yolo}</td>
  </tr>`;
}

async function refresh() {
  try {
    const filter = filterEl.value;
    const url = filter ? `/api/alerts?species=${encodeURIComponent(filter)}&limit=200` : '/api/alerts?limit=200';
    const r = await fetch(url);
    if (!r.ok) return;
    const j = await r.json();
    const items = j.items || [];
    countEl.textContent = j.total || 0;
    shownEl.textContent = items.length;
    if (items.length === 0) {
      rowsEl.innerHTML = '';
      emptyEl.style.display = '';
      return;
    }
    emptyEl.style.display = 'none';

    if (!groupEl.checked) {
      // Grouping off — render every row flat.
      rowsEl.innerHTML = items.map(a => renderRow(a)).join('');
      return;
    }

    // Grouping on — collapse consecutive same-track alerts within GROUP_WINDOW_S
    const groups = groupItems(items);
    const html = [];
    for (const g of groups) {
      if (g.children.length === 0) {
        html.push(renderRow(g.head));
      } else {
        const isOpen = expanded.has(g.head.id);
        const badge = `<span class="badge-count">×${g.children.length + 1}</span>`
          + ` <button class="expand-btn" data-group="${g.head.id}">${isOpen ? '▼ hide' : '▶ show all'}</button>`;
        html.push(renderRow(g.head, '', badge));
        if (isOpen) {
          for (const child of g.children) {
            html.push(renderRow(child, 'child'));
          }
        }
      }
    }
    rowsEl.innerHTML = html.join('');
  } catch (e) { /* ignore */ }
}

// Expand/collapse handler — event delegation on the tbody
rowsEl.addEventListener('click', (evt) => {
  const btn = evt.target.closest('.expand-btn');
  if (!btn) return;
  evt.preventDefault();
  const gid = parseInt(btn.dataset.group, 10);
  if (expanded.has(gid)) expanded.delete(gid);
  else expanded.add(gid);
  refresh();
});
groupEl.addEventListener('change', refresh);
filterEl.addEventListener('change', refresh);
document.getElementById('btn-refresh').addEventListener('click', refresh);
setInterval(() => { if (autoEl.checked) refresh(); }, 5000);
// Esc → back to the live preview dashboard
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') window.location.href = '/';
});
refresh();
</script>
</body>
</html>
"""


# ── Flask app ───────────────────────────────────────────────────────────────

def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return Response(_INDEX_HTML, mimetype="text/html")

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
        return Response(_ALERTS_HTML, mimetype="text/html")

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
