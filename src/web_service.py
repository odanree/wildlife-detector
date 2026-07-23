"""Web sidecar entrypoint (Phase 2 of ADR 002).

Runs a Flask UI on 0.0.0.0:8100 that talks to the detector process over
a small internal HTTP (see src/web/detector_api.py). Reads state that
lives on disk directly (state.db, config/detection.yaml, snapshots/,
data/baseline_*.jpg); proxies live data (frames, stats) via HTTP.

Restart independence — bouncing this process does NOT touch the detector
or the RTSP connection. Prompt-tuning iteration lives in the detector's
process; UI iteration lives here.

Usage:
    python -m src.web_service                 # reads env for detector URL/token
    python -m src.web_service --detector http://127.0.0.1:8101 --token abc123

Environment:
    DETECTOR_INTERNAL_URL   base URL of the detector's internal HTTP
                            (default http://127.0.0.1:8101)
    INTERNAL_API_TOKEN      shared bearer token — must match the detector's
    STATE_DB_PATH           path to the SQLite state store (default data/state.db)
                            read as multi-reader
    PREVIEW_HOST            bind host for THIS Flask (default 0.0.0.0)
    PREVIEW_PORT            bind port (default 8100)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import time
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory, abort

load_dotenv()

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            "logs/web.log", encoding="utf-8",
            maxBytes=10 * 1024 * 1024, backupCount=5,
        ),
    ],
)
logger = logging.getLogger("web_service")

# Quiet chatty client loggers — httpx logs one INFO line per request, which
# at 15 fps floods the log (~54K lines/hour) and rolls the 10 MB file every
# ~20 minutes. Keep werkzeug at INFO so browser-facing access logs stay visible.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── Detector proxy client ───────────────────────────────────────────────────

class DetectorClient:
    """Thin httpx wrapper around the detector's internal HTTP.

    One shared instance per Flask app. Uses a keep-alive connection pool so
    the MJPEG stream can hit the frame endpoint at 15 fps without paying TCP
    handshake cost per request.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._auth_headers = {"Authorization": f"Bearer {token}"} if token else {}
        # 30s pool timeout is way more than any single call needs; long-polls
        # respect the ``timeout=`` query param separately.
        self._client = httpx.Client(base_url=self._base_url, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def status(self) -> dict:
        r = self._client.get("/internal/status", timeout=3.0)
        r.raise_for_status()
        return r.json()

    def health(self) -> bool:
        try:
            r = self._client.get("/internal/health", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    def frame(self, since: int = -1, timeout: float = 5.0,
              raw: bool = False) -> tuple[bytes, int, int]:
        """Long-poll for a JPEG. Returns (bytes, http_status, version)."""
        path = "/internal/raw" if raw else "/internal/frame"
        r = self._client.get(
            path, params={"since": since, "timeout": timeout},
            timeout=timeout + 2.0,
        )
        ver = int(r.headers.get("X-Frame-Version", "0"))
        return r.content, r.status_code, ver

    def post_command(self, path: str, json_body: dict | None = None,
                     params: dict | None = None) -> tuple[dict, int]:
        r = self._client.post(
            path, json=json_body, params=params,
            headers=self._auth_headers, timeout=10.0,
        )
        try:
            body = r.json()
        except Exception:
            body = {"error": "detector returned non-JSON", "text": r.text[:200]}
        return body, r.status_code


class DetectorRegistry:
    """Multi-camera routing: keyed by camera_id, populated from DETECTOR_URLS.

    Each detector's /internal/status carries its own camera_id; on first probe
    we learn the mapping so URLs like ?camera=rooftop route to the right
    backend. Falls back to positional camera_id (cam0, cam1, ...) if the
    detector is unreachable at probe time — the client will keep retrying.
    """

    def __init__(self, urls: list[str], token: str) -> None:
        self._token = token
        self._clients: dict[str, DetectorClient] = {}
        self._url_by_id: dict[str, str] = {}
        self._default_id: str | None = None
        for idx, url in enumerate(urls):
            client = DetectorClient(url.strip(), token)
            camera_id = self._probe_camera_id(client) or f"cam{idx}"
            self._clients[camera_id] = client
            self._url_by_id[camera_id] = url.strip()
            if self._default_id is None:
                self._default_id = camera_id
            logger.info("DetectorRegistry: registered '%s' → %s", camera_id, url)

    @staticmethod
    def _probe_camera_id(client: DetectorClient) -> str | None:
        try:
            s = client.status()
            return s.get("camera_id")
        except Exception:
            return None

    def resolve(self, camera_id: str | None) -> DetectorClient:
        """Return client for the given camera; falls back to default (first
        detector in DETECTOR_URLS) if unknown."""
        if camera_id and camera_id in self._clients:
            return self._clients[camera_id]
        return self._clients[self._default_id]  # type: ignore[index]

    @property
    def camera_ids(self) -> list[str]:
        return list(self._clients.keys())

    @property
    def default(self) -> str:
        return self._default_id or ""

    def close_all(self) -> None:
        for c in self._clients.values():
            c.close()


# ── State readers (SQLite + YAML + disk) ────────────────────────────────────

_SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "snapshots")).resolve()
_DATA_DIR = Path("data")
_DETECTION_CFG = Path("config/detection.yaml")


# ── SSE counts pub-sub ─────────────────────────────────────────────────
# Server-side polls the DB at _COUNTS_POLL_INTERVAL_S and pushes to N
# subscribers over persistent SSE connections. Client tabs no longer
# individually poll /api/alerts/counts every 5s.
#
# Fixed server-side cost regardless of client count — DB is queried
# once per interval, result fanned out. Bounded per-subscriber queues
# (maxsize=8) drop old updates rather than block the poller if a slow
# client falls behind.
_COUNTS_POLL_INTERVAL_S = float(os.getenv("COUNTS_POLL_INTERVAL_S", "3"))
_counts_cache: dict[str, int] = {}
_counts_lock = threading.Lock()
_counts_subscribers: list["queue.Queue[dict]"] = []


def _new_counts_subscriber() -> "queue.Queue[dict]":
    q: "queue.Queue[dict]" = queue.Queue(maxsize=8)
    with _counts_lock:
        _counts_subscribers.append(q)
    return q


def _drop_counts_subscriber(q: "queue.Queue[dict]") -> None:
    with _counts_lock:
        try:
            _counts_subscribers.remove(q)
        except ValueError:
            pass


def _publish_counts(counts: dict[str, int]) -> None:
    """Fan-out a counts snapshot to all live subscribers. Best-effort:
    subscribers with full queues get their oldest message dropped so the
    poller never blocks on a slow client."""
    msg = {"type": "counts", "counts": counts}
    with _counts_lock:
        for q in list(_counts_subscribers):
            try:
                q.put_nowait(msg)
            except queue.Full:
                # Drop oldest to make room — slow client falls further
                # behind on rapid changes but doesn't stall the poller.
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except (queue.Empty, queue.Full):
                    pass


def _start_counts_poller(state, camera_ids_fn) -> threading.Thread:
    """Daemon thread that polls per-camera counts from the DB and
    publishes to SSE subscribers only when they change. Started once
    at app init; no cleanup needed (daemon).

    state: StateDB instance (has total_alerts method).
    camera_ids_fn: callable returning current list of camera_ids (function
      rather than a captured list so a future dynamic-camera-set change
      is picked up without restarting the poller)."""
    def _loop():
        global _counts_cache
        while True:
            try:
                new_counts = {
                    cam: state.total_alerts(camera_id=cam)
                    for cam in camera_ids_fn()
                }
                with _counts_lock:
                    changed = new_counts != _counts_cache
                    if changed:
                        _counts_cache = new_counts
                if changed:
                    _publish_counts(new_counts)
            except Exception:
                logger.exception("counts-poller: iteration failed")
            time.sleep(_COUNTS_POLL_INTERVAL_S)
    t = threading.Thread(target=_loop, name="counts-poller", daemon=True)
    t.start()
    return t


def _baseline_paths(camera_id: str) -> tuple[Path, Path]:
    """Return (day_path, night_path) for a given camera. Yard uses the legacy
    'baseline_{day,night}.jpg' names (no camera in the stem) for backwards
    compat with the pre-multi-camera on-disk layout. Other cameras get
    'baseline_<camera>_{day,night}.jpg'."""
    if not camera_id or camera_id == "yard":
        return _DATA_DIR / "baseline_day.jpg", _DATA_DIR / "baseline_night.jpg"
    return (
        _DATA_DIR / f"baseline_{camera_id}_day.jpg",
        _DATA_DIR / f"baseline_{camera_id}_night.jpg",
    )


def _read_zone_polygon(zone_key: str | None = None) -> tuple[list, int]:
    """Read polygon coords from YAML directly. Returns (polygon, mtime_version).

    The mtime is used as a coarse version so we can tell the browser 'poly
    changed, redraw' without a full pubsub. Coords are returned in pixel
    space at the detector's current detection resolution (fetched via
    detector's /status)."""
    if not _DETECTION_CFG.exists():
        return [], 0
    try:
        cfg = yaml.safe_load(_DETECTION_CFG.read_text(encoding="utf-8")) or {}
        # Priority: explicit zone_key arg > yaml top-level default.
        key = zone_key or cfg.get("zone_key", "yard_zone")
        raw = cfg.get("zones", {}).get(key, {}).get("polygon", [])
        version = int(_DETECTION_CFG.stat().st_mtime)
        return raw, version
    except Exception:
        logger.exception("read_zone_polygon failed")
        return [], 0


def _read_osd_masks(camera_id: str = "yard") -> tuple[list, int]:
    """Read the mask list for one camera. Handles both legacy flat-list form
    (all masks belonged to yard) and per-camera dict form."""
    if not _DETECTION_CFG.exists():
        return [], 0
    try:
        cfg = yaml.safe_load(_DETECTION_CFG.read_text(encoding="utf-8")) or {}
        osd = cfg.get("osd_masks", []) or []
        if isinstance(osd, dict):
            raw = osd.get(camera_id, []) or []
        else:
            raw = osd if camera_id == "yard" else []
        version = int(_DETECTION_CFG.stat().st_mtime)
        return raw, version
    except Exception:
        logger.exception("read_osd_masks failed")
        return [], 0


def _scale_normalized_polygon(raw: list, det_w: int, det_h: int) -> list:
    """Same auto-detect scaling as pipeline._scale_polygon — normalize floats
    to detection pixels or return absolute pixels unchanged."""
    if not raw:
        return []
    if all(all(v <= 1.5 for v in p) for p in raw):
        return [[int(round(x * det_w)), int(round(y * det_h))] for x, y in raw]
    return [[int(x), int(y)] for x, y in raw]


def _scale_normalized_masks(raw: list, det_w: int, det_h: int) -> list:
    out = []
    for m in raw:
        if len(m) != 4:
            continue
        if all(v <= 1.5 for v in m):
            out.append([
                int(round(m[0] * det_w)), int(round(m[1] * det_h)),
                int(round(m[2] * det_w)), int(round(m[3] * det_h)),
            ])
        else:
            out.append([int(v) for v in m])
    return out


def _baseline_meta(camera_id: str = "yard") -> dict:
    """Compute baseline metadata from disk (both processes see the same
    files, so no IPC needed for this read). camera_id picks which pair of
    JPEGs to inspect — see _baseline_paths()."""
    def _slot(path: Path) -> dict:
        if not path.exists():
            return {"exists": False, "ts": 0, "bytes": 0}
        st = path.stat()
        return {"exists": True, "ts": st.st_mtime, "bytes": st.st_size}
    day_path, night_path = _baseline_paths(camera_id)
    day = _slot(day_path)
    night = _slot(night_path)
    return {
        "exists": day["exists"] or night["exists"],
        "version": int(day["ts"] + night["ts"]),   # coarse but stable
        "day": day,
        "night": night,
    }


# ── Flask app ───────────────────────────────────────────────────────────────

def create_app(registry: DetectorRegistry) -> Flask:
    """Build the web-sidecar Flask app. Serves the React SPA at /react/*
    (built by Vite into /app/static/react/) and proxies API + stream calls
    to the detector(s) in DETECTOR_URLS.

    Multi-camera routing: every camera-scoped endpoint accepts ?camera=<id>
    (default: first camera in DETECTOR_URLS). /status_all returns aggregated
    status across all registered cameras for the dashboard summary strip.

    The vanilla-JS operator UI was retired in PR 11c (strangler-fig
    complete). / redirects to /react/preview; preview.py's _INDEX_HTML
    has been deleted. preview.py still exports the small _FAVICON_SVG
    constant + the state holders used by pipeline.py.
    """
    from src.web import preview
    from src.storage.state_db import StateDB
    _state = StateDB(os.getenv("STATE_DB_PATH", "data/state.db"))

    # Kick off the counts SSE poller — fixed-cost DB reader that pushes
    # to all EventSource subscribers instead of each tab polling
    # /api/alerts/counts individually every 5s. Daemon thread; no
    # shutdown handling needed.
    _start_counts_poller(_state, lambda: registry.camera_ids)
    logger.info("counts-poller: started (interval=%.1fs)", _COUNTS_POLL_INTERVAL_S)

    app = Flask(__name__)

    def _pick(req) -> DetectorClient:
        """Route helper — parse ?camera=<id> and hand back the client."""
        return registry.resolve(req.args.get("camera"))

    # ── Pages ───────────────────────────────────────────────────────────────

    @app.get("/")
    def index():
        # Strangler-fig complete: the vanilla-JS operator UI is gone; the
        # React app at /react/preview is the only path. 302 (temporary)
        # so a rollback can be delivered by env flag if needed without
        # stale-caching /react in operator browsers.
        from flask import redirect
        return redirect("/react/preview", code=302)

    @app.get("/alerts")
    def alerts_page():
        # Cutover: /alerts is now served by the React app at /react/alerts.
        # 302 (temporary) rather than 301 (permanent) so we can reroute later
        # without stale-cache surprises in operator browsers. Existing
        # bookmarks continue to work; the address bar just updates. See
        # docs/prototype-to-production-blueprint.md phase 7 for context.
        from flask import redirect
        return redirect("/react/alerts", code=302)

    @app.get("/baselines")
    def baselines_page():
        # Cutover: /baselines is now served by React at /react/baselines.
        # 302 (temporary) so a rollback can be delivered without stale-
        # cache pain in operator browsers, though the vanilla-JS
        # /_INDEX_HTML that used to link here is gone as of PR 11c.
        from flask import redirect
        return redirect("/react/baselines", code=302)

    @app.get("/favicon.ico")
    @app.get("/favicon.svg")
    def favicon():
        return Response(preview._FAVICON_SVG, mimetype="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

    # ── React shell (PR 1 of frontend migration) ────────────────────────────
    # Vite builds to /app/static/react/ (see docker/web/Dockerfile stage 1).
    # Base path is "/react/" per vite.config.ts, so index.html references
    # assets as /react/assets/... — Flask needs to serve both the entry
    # HTML and the hashed assets under that prefix. Missing bundle (dev
    # run of Flask without the build step) returns a helpful 404 body
    # rather than a bare stack trace.
    import os as _os_mod
    _REACT_DIST = _os_mod.path.join(_os_mod.path.dirname(_os_mod.path.abspath(__file__)),
                                    "..", "static", "react")
    _REACT_DIST = _os_mod.path.abspath(_REACT_DIST)

    @app.get("/react/")
    @app.get("/react/<path:_p>")
    def react_shell(_p: str = ""):
        # Any subpath serves index.html (SPA-style routing). Vite-hashed
        # assets are matched by the more specific /react/assets route
        # below and won't fall through here.
        idx = _os_mod.path.join(_REACT_DIST, "index.html")
        if not _os_mod.path.exists(idx):
            return Response(
                "React bundle not built. Run `docker compose build web` "
                "or `cd frontend && npm run build`.",
                status=404, mimetype="text/plain",
            )
        with open(idx, "rb") as fh:
            return Response(fh.read(), mimetype="text/html")

    @app.get("/react/assets/<path:filename>")
    def react_assets(filename: str):
        from flask import send_from_directory
        assets_dir = _os_mod.path.join(_REACT_DIST, "assets")
        return send_from_directory(assets_dir, filename,
                                   max_age=31536000)  # hashed → immutable

    # ── Cameras roster (for UI dropdown) ────────────────────────────────────

    @app.get("/api/cameras")
    def api_cameras():
        return jsonify({
            "cameras": registry.camera_ids,
            "default": registry.default,
        })

    # ── Frames / stream (proxy to detector) ────────────────────────────────

    @app.get("/snapshot")
    def snapshot():
        detector = _pick(request)
        jpeg, status, ver = detector.frame(since=-1, timeout=2.0)
        if not jpeg:
            return Response(b"", status=204)
        return Response(jpeg, mimetype="image/jpeg")

    @app.get("/stream")
    def stream():
        """MJPEG that pulls fresh frames from the detector via long-poll."""
        detector = _pick(request)
        boundary = b"--frame"

        def generate():
            last_seen = -1
            while True:
                jpeg, http_status, ver = detector.frame(since=last_seen, timeout=5.0)
                if not jpeg:
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

    # ── Status (per-camera or aggregated) ──────────────────────────────────

    @app.get("/status")
    def status():
        """Per-camera status. ?camera=<id> switches which detector is queried."""
        detector = _pick(request)
        try:
            return jsonify(detector.status())
        except Exception:
            # Detector is down or unreachable — return a sentinel the UI can
            # render as 'detector offline' without breaking.
            return jsonify({
                "fps": 0.0,
                "alerts_total": 0,
                "uptime_seconds": 0,
                "backend": "offline",
                "camera": "detector unreachable",
                "detection_size": [0, 0],
                "last_alert": None,
            })

    # ── Alerts (direct SQLite read) ────────────────────────────────────────

    @app.get("/api/alerts")
    def api_alerts():
        """Unified alerts across all cameras. Filter with ?camera=<id> for
        per-camera view. Alerts are camera-tagged since the ADR-002 multi-cam
        migration; older rows default to camera_id='yard'."""
        try:
            limit = min(500, max(1, int(request.args.get("limit", "200"))))
        except ValueError:
            limit = 200
        species_filter = (request.args.get("species") or "").lower().strip() or None
        camera_filter = (request.args.get("camera") or "").strip() or None
        # scope=historical|live|all — used by labeling workflow to focus
        # on backfilled snapshots without live noise, or vice versa.
        scope = (request.args.get("scope") or "").strip().lower() or None
        if scope not in (None, "historical", "live", "all"):
            scope = None
        # label_filter=unlabeled|labeled|correct|incorrect|unclear|all
        # — the sifting + review flow: 'unlabeled' hides voted rows,
        # 'correct'/'incorrect'/'unclear' isolates verdicts for reviewing
        # your positive dataset or auditing FPs.
        lf = (request.args.get("label_filter") or "").strip().lower() or None
        _lf_valid = {"unlabeled", "labeled", "correct", "incorrect", "unclear"}
        if lf not in ({None, "all"} | _lf_valid):
            lf = None
        items = _state.list_alerts(
            limit=limit, species=species_filter,
            camera_id=camera_filter,
            scope=scope if scope in ("historical", "live") else None,
            label_filter=lf if lf in _lf_valid else None,
        )
        return jsonify({
            # Scope total to the same camera filter as items — otherwise
            # the header unread badge diffs a per-camera watermark against
            # an all-cameras counter and shows "you have 5 unread yard
            # alerts" when in fact 5 rooftop alerts fired.
            "total": _state.total_alerts(camera_id=camera_filter),
            "items": items,
        })

    @app.get("/api/alerts/counts")
    def api_alert_counts():
        """Per-camera alert counts in one call. Used by the header badge
        to sum unread across all visible panes without paying N HTTP
        round-trips per poll. Response shape: {"yard": 2290, "rooftop": 4790}."""
        counts = {}
        for cam in registry.camera_ids:
            counts[cam] = _state.total_alerts(camera_id=cam)
        return jsonify(counts)

    # ── Counts SSE — push, not poll ───────────────────────────────────
    # Server-side polls the DB every _COUNTS_POLL_INTERVAL_S and fans
    # out to N subscribers over one persistent SSE connection each.
    # Replaces the previous per-tab setInterval on useUnreadAlerts, which
    # generated 1 HTTP request per tab per poll interval. Now the DB is
    # polled at a fixed rate regardless of connected clients, and clients
    # get pushed updates only when counts actually change.
    #
    # Pub-sub via queue.Queue per subscriber — thread-safe, bounded
    # (maxsize=8 drops old updates rather than blocking the poller if a
    # slow client falls behind). Same shape the RTSP handler already
    # uses for frame fan-out.
    @app.get("/api/alerts/events")
    def api_alerts_events():
        """Server-sent events stream for alert counts + new-alert
        notifications. Emits SSE `data:` frames with JSON payloads.

        Frame types:
          {"type": "counts", "counts": {"yard": N, "rooftop": M}}
              — sent on connect (initial state) and whenever the DB-
              polled counts change.
          ": keepalive"  (SSE comment line) — sent every 25s so idle
              connections don't get killed by upstream proxies / LB
              idle timeouts.

        Client (EventSource) auto-reconnects on disconnect."""
        from flask import stream_with_context
        def stream():
            import json as _json
            q: "queue.Queue[dict]" = _new_counts_subscriber()
            try:
                # Initial snapshot — bring the fresh subscriber up to date
                # without waiting for the next change event.
                with _counts_lock:
                    initial = dict(_counts_cache)
                yield f"data: {_json.dumps({'type': 'counts', 'counts': initial})}\n\n"
                while True:
                    try:
                        # 25s heartbeat — under nginx / caddy 30s idle default.
                        msg = q.get(timeout=25.0)
                        yield f"data: {_json.dumps(msg)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            except GeneratorExit:
                # Client disconnected — clean up subscriber.
                pass
            finally:
                _drop_counts_subscriber(q)
        return Response(
            stream_with_context(stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # tell nginx not to buffer
            },
        )

    @app.post("/api/alerts/<int:alert_id>/label")
    def api_alert_label(alert_id: int):
        """Apply / clear a human label on an alert row.
        Body: {"verdict": "correct" | "incorrect" | "unclear" | null,
               "species": "real_mouse" | "FP:insect" | ..., "notes": "..."}
        verdict=null clears the label (undo)."""
        payload = request.get_json(silent=True) or {}
        verdict = payload.get("verdict")
        if verdict not in (None, "correct", "incorrect", "unclear"):
            return jsonify({"error": "verdict must be one of: correct, incorrect, unclear, or null"}), 400
        species = payload.get("species") or None
        notes = payload.get("notes") or None
        if not _state:
            return jsonify({"error": "state db unavailable"}), 503
        ok = _state.set_label(alert_id, verdict, species, notes)
        if not ok:
            return jsonify({"error": "alert not found"}), 404
        return jsonify({"ok": True, "alert_id": alert_id, "verdict": verdict, "species": species})

    @app.get("/api/labels/export.jsonl")
    def api_labels_export():
        """Export all labeled rows as JSONL (one JSON object per line) —
        ready for training-set consumption. Default behavior excludes
        operational rows the operator doesn't care about labeling:

          - species='human_heartbeat' — human-presence signals, neither
            wildlife nor a pipeline mistake. Filter with ?include_human=1
            to opt in (audit-the-exclusion-mask flow).
          - label_verdict='unclear' — explicitly-ambiguous rows. Filter
            with ?include_unclear=1 to include.

        Additional query params:
          camera=yard|rooftop     — restrict to one camera
          verdict=correct|incorrect — restrict to one verdict slice

        Returns the file as attachment for `curl -o labels.jsonl` piping.
        Each line contains: alert_id, snapshot (relative path under
        snapshots/), snapshot_url, verdict, label_species, detected_species,
        camera_id, ts, historical, confidence, description."""
        if not _state:
            return jsonify({"error": "state db unavailable"}), 503
        camera = (request.args.get("camera") or "").strip() or None
        verdict = (request.args.get("verdict") or "").strip() or None
        if verdict not in (None, "correct", "incorrect"):
            verdict = None
        include_human = request.args.get("include_human", "0") == "1"
        include_unclear = request.args.get("include_unclear", "0") == "1"
        exclude = None if include_human else ["human_heartbeat"]
        rows = _state.list_labeled_for_export(
            exclude_species=exclude,
            include_unclear=include_unclear,
            camera_id=camera,
            verdict=verdict,
        )
        # Build JSONL — one row per line so consumers can stream + line-split.
        import json as _json
        def _line(r):
            snap = r.get("snapshot")
            return _json.dumps({
                "alert_id":         r.get("id"),
                "snapshot":         snap,
                "snapshot_url":     f"/snapshots/{snap}" if snap else None,
                "verdict":          r.get("label_verdict"),
                "label_species":    r.get("label_species"),
                "detected_species": r.get("species"),
                "camera_id":        r.get("camera_id"),
                "ts":               r.get("ts"),
                "historical":       bool(r.get("historical")),
                "confidence":       r.get("confidence"),
                "description":      r.get("description"),
            }, separators=(",", ":"))
        body = "\n".join(_line(r) for r in rows)
        if body:
            body += "\n"
        response = app.response_class(body, mimetype="application/x-ndjson")
        response.headers["Content-Disposition"] = "attachment; filename=labels.jsonl"
        response.headers["X-Row-Count"] = str(len(rows))
        return response

    @app.post("/api/labels/backfill-from-disk")
    def api_labels_backfill():
        """Re-import snapshot JPEGs from disk as historical alert rows so
        they show up in the labeling UI. Idempotent (INSERT OR IGNORE).

        Body/query params (all optional):
          from_date  "YYYY-MM-DD" inclusive
          to_date    "YYYY-MM-DD" inclusive
          camera     tag inserted rows with this camera_id (default: 'yard')
          hour_start local hour, inclusive  (e.g. 22 for 10 PM)
          hour_end   local hour, exclusive  (e.g. 5 for 5 AM)
                     Wrap-around supported: hour_start=22 + hour_end=5 =
                     "10 PM through 5 AM" the nocturnal window rodents
                     actually move in.

        Returns {inserted, from_date, to_date, hour_start, hour_end}."""
        # Accept params from either JSON body or query string — the endpoint
        # is easy to hit from curl or from a future UI button.
        payload = request.get_json(silent=True) or {}
        def _pick(key, default=None, cast=None):
            v = payload.get(key)
            if v is None:
                v = request.args.get(key)
            if v is None or v == "":
                return default
            if cast:
                try:
                    return cast(v)
                except (TypeError, ValueError):
                    return default
            return v
        from_date  = _pick("from_date")
        to_date    = _pick("to_date")
        camera     = _pick("camera")
        hour_start = _pick("hour_start", cast=int)
        hour_end   = _pick("hour_end", cast=int)
        tz         = _pick("tz", default="America/Los_Angeles")
        # Prefer preview.py's initialized _snapshot_dir; fall back to the
        # SNAPSHOT_DIR env used elsewhere in this service. The web
        # container's preview.init flow doesn't always run before HTTP
        # traffic hits this endpoint.
        from src.web.preview import _alerts, _snapshot_dir
        from pathlib import Path
        snap_dir = _snapshot_dir if _snapshot_dir is not None else _SNAPSHOT_DIR
        if not snap_dir.exists():
            return jsonify({"error": f"snapshot dir not found: {snap_dir}"}), 503
        # AlertLog needs a bound StateDB before it can insert. If preview
        # setup didn't wire it, do it here so the endpoint works standalone.
        if _alerts._state is None and _state is not None:
            _alerts.bind_state(_state)
        n = _alerts.backfill_from_disk(
            snap_dir,
            from_date=from_date,
            to_date=to_date,
            camera_id=camera,
            hour_start=hour_start,
            hour_end=hour_end,
            tz=tz,
        )
        return jsonify({
            "inserted":   n,
            "from_date":  from_date,
            "to_date":    to_date,
            "camera":     camera,
            "hour_start": hour_start,
            "hour_end":   hour_end,
            "tz":         tz,
        })

    @app.post("/api/alerts/label-bulk")
    def api_alerts_label_bulk():
        """Apply the same label to N alerts in one call. Body shape:
        {"alert_ids": [1,2,3,...], "verdict": "correct"|"incorrect"|"unclear"|null,
         "species": "real_mouse"|"FP:insect"|null, "notes": null}
        Returns {updated: N}. Used by the mass-tag select-all UI."""
        payload = request.get_json(silent=True) or {}
        ids = payload.get("alert_ids") or []
        if not isinstance(ids, list) or not all(isinstance(x, int) for x in ids):
            return jsonify({"error": "alert_ids must be a list of integers"}), 400
        if len(ids) > 500:
            return jsonify({"error": "batch size capped at 500"}), 400
        verdict = payload.get("verdict")
        if verdict not in (None, "correct", "incorrect", "unclear"):
            return jsonify({"error": "verdict must be one of: correct, incorrect, unclear, or null"}), 400
        species = payload.get("species") or None
        notes = payload.get("notes") or None
        if not _state:
            return jsonify({"error": "state db unavailable"}), 503
        n = _state.set_labels_bulk(ids, verdict, species, notes)
        return jsonify({"updated": n, "verdict": verdict, "species": species})

    @app.get("/api/alerts/unlabeled")
    def api_alerts_unlabeled():
        """Return the newest N unlabeled alerts for the batch labeling page.
        Query params: limit (default 50, max 200), camera (optional filter)."""
        try:
            limit = min(200, max(1, int(request.args.get("limit", "50"))))
        except ValueError:
            limit = 50
        camera = (request.args.get("camera") or "").strip() or None
        if not _state:
            return jsonify({"items": [], "counts": {}})
        return jsonify({
            "items":  _state.list_unlabeled(limit=limit, camera_id=camera),
            "counts": _state.label_counts(),
        })

    @app.get("/api/alerts/<int:alert_id>/playback-url")
    def api_alert_playback(alert_id: int):
        """Return an RTSP NVR playback URL for the alert's timestamp so the
        operator can replay footage in VLC / mpv / any RTSP client. Uses
        the same URL builder as the live-preview seek-to-datetime feature.

        Query params:
          pre_roll   seconds of context before the alert ts (default 15)
          channel    override NVR channel; else uses NVR_CHANNEL_<CAMERA> env

        Response: {url, camera_id, ts, channel, pre_roll_seconds, note?}
        note surfaces caveats — e.g. "NVR channel not configured for camera".
        RTSP URLs open natively in VLC / mpv on most desktop OSes when the
        rtsp:// protocol handler is registered."""
        alert = _state.get_alert(alert_id) if _state else None
        if not alert:
            return jsonify({"error": "alert not found"}), 404

        camera_id = alert.get("camera_id") or ""
        ts = float(alert.get("ts", 0.0))
        if ts <= 0:
            return jsonify({"error": "alert has no timestamp"}), 400

        try:
            pre_roll = int(request.args.get("pre_roll", "15"))
        except ValueError:
            pre_roll = 15
        pre_roll = max(0, min(600, pre_roll))

        # Per-camera NVR channel via env: NVR_CHANNEL_YARD=6, NVR_CHANNEL_ROOFTOP=8
        # Falls back to URL-embedded channel then to '1'.
        env_channel = os.environ.get(f"NVR_CHANNEL_{camera_id.upper()}")
        try:
            channel_override = int(request.args.get("channel") or env_channel or 0)
        except ValueError:
            channel_override = 0
        channel: int | None = channel_override if channel_override > 0 else None

        # Web container has AMCREST_HOST/USER/PASS in its env (env_file:
        # .env in compose), so build_nvr_playback_url() gets host/creds
        # from env — no need to look up the detector's RTSP url here.
        # Empty base_url just makes the fallback regex extraction no-op.
        base_url = ""

        note = None
        if not env_channel and channel_override == 0:
            note = (f"NVR_CHANNEL_{camera_id.upper()} not set — playback URL may hit "
                    f"the wrong channel or return no data. Set the env var to the "
                    f"NVR channel this camera records to.")

        from src.stream.rtsp_handler import build_nvr_playback_url
        try:
            url = build_nvr_playback_url(
                timestamp=ts,
                base_rtsp_url=base_url,
                pre_roll_seconds=pre_roll,
                nvr_channel=channel,
            )
        except Exception as exc:
            return jsonify({"error": f"failed to build playback url: {exc}"}), 500

        return jsonify({
            "url":              url,
            "camera_id":        camera_id,
            "ts":               ts,
            "channel":          channel,
            "pre_roll_seconds": pre_roll,
            "note":             note,
        })

    @app.get("/snapshots/<path:filename>")
    def serve_snapshot(filename: str):
        # send_from_directory blocks ../ traversal safely.
        return send_from_directory(_SNAPSHOT_DIR, filename, max_age=3600)

    # ── Zone (direct YAML read; write via detector command) ────────────────

    @app.get("/api/zone")
    def get_zone():
        # Need det_w/det_h + zone_key from detector's status so the right
        # polygon is fetched (yard_zone vs rooftop_zone) and scaled correctly.
        detector = _pick(request)
        try:
            st = detector.status()
            det_w, det_h = st.get("detection_size", [1280, 720])
            zone_key = st.get("zone_key")   # None means fallback to yaml default
        except Exception:
            det_w, det_h = 1280, 720
            zone_key = None
        raw, ver = _read_zone_polygon(zone_key=zone_key)
        return jsonify({
            "polygon": _scale_normalized_polygon(raw, det_w, det_h),
            "version": ver,
        })

    @app.post("/api/zone")
    def post_zone():
        detector = _pick(request)
        body = request.get_json(silent=True) or {}
        result, status_code = detector.post_command("/internal/zone", json_body=body)
        return jsonify(result), status_code

    # ── OSD masks (direct YAML read; write via detector command) ───────────

    @app.get("/api/masks")
    def get_masks():
        detector = _pick(request)
        cam_id = request.args.get("camera") or registry.default
        try:
            st = detector.status()
            det_w, det_h = st.get("detection_size", [1280, 720])
        except Exception:
            det_w, det_h = 1280, 720
        raw, ver = _read_osd_masks(camera_id=cam_id)
        return jsonify({
            "masks": _scale_normalized_masks(raw, det_w, det_h),
            "version": ver,
        })

    @app.post("/api/masks")
    def post_masks():
        detector = _pick(request)
        body = request.get_json(silent=True) or {}
        result, status_code = detector.post_command("/internal/masks", json_body=body)
        return jsonify(result), status_code

    # ── Baseline (direct disk read; write via detector command) ────────────

    @app.get("/api/baseline")
    def api_baseline_meta():
        cam_id = request.args.get("camera") or registry.default
        return jsonify(_baseline_meta(camera_id=cam_id))

    @app.get("/api/baseline.jpg")
    def api_baseline_jpeg():
        cam_id = request.args.get("camera") or registry.default
        day_path, night_path = _baseline_paths(cam_id)
        requested = (request.args.get("mode") or "").lower()
        if requested == "night":
            path = night_path
        elif requested == "day":
            path = day_path
        else:
            path = day_path if day_path.exists() else night_path
        if not path.exists():
            return Response(b"", status=404)
        return Response(path.read_bytes(), mimetype="image/jpeg")

    @app.post("/api/baseline/capture")
    def post_baseline_capture():
        detector = _pick(request)
        # Forward ?mode=day|night if supplied so the UI can override the
        # brightness auto-picker (which misclassifies IR-lit foliage as day
        # on overhead cameras).
        params = {"mode": request.args.get("mode")} if request.args.get("mode") else None
        result, status_code = detector.post_command("/internal/baseline/capture", params=params)
        return jsonify(result), status_code

    @app.post("/api/baseline/clear")
    def post_baseline_clear():
        detector = _pick(request)
        params = {"mode": request.args.get("mode")} if request.args.get("mode") else None
        result, status_code = detector.post_command("/internal/baseline/clear", params=params)
        return jsonify(result), status_code

    # ── Tier-3 anti-pattern instrumentation: React Profiler sink ───────────
    #
    # Receives batched slow-commit events from the frontend's <Profiler>
    # wrapping (see frontend/src/util/perfSink.ts). Appends to a JSONL
    # file so we can attribute perf regressions to specific deploys via
    # the git SHA baked into the frontend at build time.
    #
    # Pattern: production-side telemetry sink with append-only durable
    # storage. Deliberately dumb — no per-event auth, no aggregation
    # in-line, no schema validation beyond "it must be JSON with events".
    # A downstream aggregator (offline / cron) reads the JSONL and
    # produces the SLO dashboard. Kept dumb so the hot path stays fast
    # and the sink never becomes the bottleneck.
    _PERF_LOG_PATH = Path(os.getenv("PERF_LOG_PATH", "data/perf-profile.jsonl"))
    _PERF_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    @app.post("/api/perf/profile")
    def post_perf_profile():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        events = body.get("events")
        if not isinstance(events, list) or not events:
            return jsonify({"ok": True, "wrote": 0})
        sha = body.get("sha", "unknown")
        ua = body.get("ua", "")
        received_at = time.time()
        try:
            with _PERF_LOG_PATH.open("a", encoding="utf-8") as f:
                for ev in events:
                    f.write(
                        json.dumps({"sha": sha, "ua": ua, "t": received_at, "ev": ev}) + "\n"
                    )
        except OSError as exc:
            logger.warning("perf sink write failed: %s", exc)
            return jsonify({"error": "sink unavailable"}), 503
        return jsonify({"ok": True, "wrote": len(events)})

    return app


# ── Entrypoint ──────────────────────────────────────────────────────────────

_shutdown = threading.Event()


def _signal_handler(signum, _frame):
    if _shutdown.is_set():
        logger.warning("Second Ctrl+C — force exit")
        os._exit(1)
    logger.info("Signal %d — shutting down", signum)
    _shutdown.set()


def main() -> None:
    ap = argparse.ArgumentParser(description="Wildlife detector web sidecar")
    # Multi-camera preferred: DETECTOR_URLS is a comma-separated list of
    # detector internal HTTP URLs. Falls back to the legacy single-detector
    # DETECTOR_INTERNAL_URL / --detector so old deployments keep working.
    ap.add_argument("--detector",
                    default=os.getenv("DETECTOR_INTERNAL_URL", "http://127.0.0.1:8101"))
    ap.add_argument("--detector-urls",
                    default=os.getenv("DETECTOR_URLS", ""),
                    help="Comma-separated list of detector URLs for multi-camera mode")
    ap.add_argument("--token", default=os.getenv("INTERNAL_API_TOKEN", ""))
    ap.add_argument("--host", default=os.getenv("PREVIEW_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.getenv("PREVIEW_PORT", "8100")))
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    if not args.token:
        logger.warning(
            "INTERNAL_API_TOKEN is empty — POST endpoints will fail against "
            "the detector's bearer check. Set INTERNAL_API_TOKEN to the same "
            "value the detector process logged at startup."
        )

    # Prefer DETECTOR_URLS (multi-camera) over single --detector fallback.
    urls = [u.strip() for u in args.detector_urls.split(",") if u.strip()]
    if not urls:
        urls = [args.detector]
    registry = DetectorRegistry(urls, args.token)
    logger.info("Web sidecar starting — cameras: %s (default '%s')",
                registry.camera_ids, registry.default)
    logger.info("Web sidecar listening on http://%s:%d", args.host, args.port)

    app = create_app(registry)
    try:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    finally:
        registry.close_all()
        logger.info("Web sidecar exiting")
        os._exit(0)


if __name__ == "__main__":
    main()
