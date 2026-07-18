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
import logging
import os
import signal
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


# ── State readers (SQLite + YAML + disk) ────────────────────────────────────

_SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "snapshots")).resolve()
_DATA_DIR = Path("data")
_BASELINE_DAY = _DATA_DIR / "baseline_day.jpg"
_BASELINE_NIGHT = _DATA_DIR / "baseline_night.jpg"
_DETECTION_CFG = Path("config/detection.yaml")


def _read_zone_polygon() -> tuple[list, int]:
    """Read polygon coords from YAML directly. Returns (polygon, mtime_version).

    The mtime is used as a coarse version so we can tell the browser 'poly
    changed, redraw' without a full pubsub. Coords are returned in pixel
    space at the detector's current detection resolution (fetched via
    detector's /status)."""
    if not _DETECTION_CFG.exists():
        return [], 0
    try:
        cfg = yaml.safe_load(_DETECTION_CFG.read_text(encoding="utf-8")) or {}
        zone_key = cfg.get("zone_key", "yard_zone")
        raw = cfg.get("zones", {}).get(zone_key, {}).get("polygon", [])
        version = int(_DETECTION_CFG.stat().st_mtime)
        return raw, version
    except Exception:
        logger.exception("read_zone_polygon failed")
        return [], 0


def _read_osd_masks() -> tuple[list, int]:
    if not _DETECTION_CFG.exists():
        return [], 0
    try:
        cfg = yaml.safe_load(_DETECTION_CFG.read_text(encoding="utf-8")) or {}
        raw = cfg.get("osd_masks", []) or []
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


def _baseline_meta() -> dict:
    """Compute baseline metadata from disk (both processes see the same
    files, so no IPC needed for this read)."""
    def _slot(path: Path) -> dict:
        if not path.exists():
            return {"exists": False, "ts": 0, "bytes": 0}
        st = path.stat()
        return {"exists": True, "ts": st.st_mtime, "bytes": st.st_size}
    day = _slot(_BASELINE_DAY)
    night = _slot(_BASELINE_NIGHT)
    return {
        "exists": day["exists"] or night["exists"],
        "version": int(day["ts"] + night["ts"]),   # coarse but stable
        "day": day,
        "night": night,
    }


# ── Flask app ───────────────────────────────────────────────────────────────

def create_app(detector: DetectorClient) -> Flask:
    """Build the web-sidecar Flask app. Reuses the HTML constants from the
    existing preview.py so the UI is byte-identical to the all-in-one path."""
    # Import HTML strings from the existing module — the UI is unchanged.
    from src.web import preview
    # Open a read-only StateDB handle for the alerts endpoint. The detector
    # is the sole writer; we only need SELECTs.
    from src.storage.state_db import StateDB
    _state = StateDB(os.getenv("STATE_DB_PATH", "data/state.db"))

    app = Flask(__name__)

    # ── Pages ───────────────────────────────────────────────────────────────

    @app.get("/")
    def index():
        return Response(preview._INDEX_HTML, mimetype="text/html")

    @app.get("/alerts")
    def alerts_page():
        return Response(preview._ALERTS_HTML, mimetype="text/html")

    # ── Frames / stream (proxy to detector) ────────────────────────────────

    @app.get("/snapshot")
    def snapshot():
        jpeg, status, ver = detector.frame(since=-1, timeout=2.0)
        if not jpeg:
            return Response(b"", status=204)
        return Response(jpeg, mimetype="image/jpeg")

    @app.get("/stream")
    def stream():
        """MJPEG that pulls fresh frames from the detector via long-poll."""
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

    # ── Status (proxy to detector) ─────────────────────────────────────────

    @app.get("/status")
    def status():
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
        try:
            limit = min(500, max(1, int(request.args.get("limit", "200"))))
        except ValueError:
            limit = 200
        species_filter = (request.args.get("species") or "").lower().strip() or None
        items = _state.list_alerts(limit=limit, species=species_filter)
        return jsonify({
            "total": _state.total_alerts(),
            "items": items,
        })

    @app.get("/snapshots/<path:filename>")
    def serve_snapshot(filename: str):
        # send_from_directory blocks ../ traversal safely.
        return send_from_directory(_SNAPSHOT_DIR, filename, max_age=3600)

    # ── Zone (direct YAML read; write via detector command) ────────────────

    @app.get("/api/zone")
    def get_zone():
        # Need det_w/det_h from detector's status to scale normalized coords.
        try:
            st = detector.status()
            det_w, det_h = st.get("detection_size", [1280, 720])
        except Exception:
            det_w, det_h = 1280, 720
        raw, ver = _read_zone_polygon()
        return jsonify({
            "polygon": _scale_normalized_polygon(raw, det_w, det_h),
            "version": ver,
        })

    @app.post("/api/zone")
    def post_zone():
        body = request.get_json(silent=True) or {}
        result, status_code = detector.post_command("/internal/zone", json_body=body)
        return jsonify(result), status_code

    # ── OSD masks (direct YAML read; write via detector command) ───────────

    @app.get("/api/masks")
    def get_masks():
        try:
            st = detector.status()
            det_w, det_h = st.get("detection_size", [1280, 720])
        except Exception:
            det_w, det_h = 1280, 720
        raw, ver = _read_osd_masks()
        return jsonify({
            "masks": _scale_normalized_masks(raw, det_w, det_h),
            "version": ver,
        })

    @app.post("/api/masks")
    def post_masks():
        body = request.get_json(silent=True) or {}
        result, status_code = detector.post_command("/internal/masks", json_body=body)
        return jsonify(result), status_code

    # ── Baseline (direct disk read; write via detector command) ────────────

    @app.get("/api/baseline")
    def api_baseline_meta():
        return jsonify(_baseline_meta())

    @app.get("/api/baseline.jpg")
    def api_baseline_jpeg():
        requested = (request.args.get("mode") or "").lower()
        if requested == "night":
            path = _BASELINE_NIGHT
        elif requested == "day":
            path = _BASELINE_DAY
        else:
            path = _BASELINE_DAY if _BASELINE_DAY.exists() else _BASELINE_NIGHT
        if not path.exists():
            return Response(b"", status=404)
        return Response(path.read_bytes(), mimetype="image/jpeg")

    @app.post("/api/baseline/capture")
    def post_baseline_capture():
        result, status_code = detector.post_command("/internal/baseline/capture")
        return jsonify(result), status_code

    @app.post("/api/baseline/clear")
    def post_baseline_clear():
        params = {"mode": request.args.get("mode")} if request.args.get("mode") else None
        result, status_code = detector.post_command("/internal/baseline/clear", params=params)
        return jsonify(result), status_code

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
    ap.add_argument("--detector",
                    default=os.getenv("DETECTOR_INTERNAL_URL", "http://127.0.0.1:8101"))
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

    detector = DetectorClient(args.detector, args.token)
    logger.info("Web sidecar starting — will proxy to detector at %s", args.detector)
    logger.info("Web sidecar listening on http://%s:%d", args.host, args.port)

    app = create_app(detector)
    try:
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    finally:
        detector.close()
        logger.info("Web sidecar exiting")
        os._exit(0)


if __name__ == "__main__":
    main()
