"""Detector's internal HTTP surface (Phase 2 of ADR 002).

Runs on 127.0.0.1:8101 by default. Bearer-token-gated on POSTs. Does NOT
serve any UI — just the minimal endpoints the web sidecar needs to fetch
frames + status and to submit configuration commands.

The detector still owns:
  - The in-memory frame holders (LatestFrame + LatestRawFrame)
  - The Stats counter (process-local)
  - The ZoneHolder / MaskHolder / Baseline / AlertLog singletons

Web sidecar reads:
  - alerts → SQLite state.db directly (both processes have read access)
  - zone / masks → config/detection.yaml directly
  - baseline JPEGs → data/baseline_{day,night}.jpg on disk
  - frames / status → via THIS internal HTTP

Web sidecar writes only via POST commands to this HTTP surface. The
bearer token comes from the shared INTERNAL_API_TOKEN env var — both
processes must see the same value.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time

from flask import Flask, Response, jsonify, request

from src.web import preview

logger = logging.getLogger(__name__)


# Bearer token for POST endpoints. If unset in the environment, we generate
# one at startup and log it — dev-friendly default, no accidental prod
# deployment with a blank token.
_INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN") or secrets.token_urlsafe(24)


def _authorized(req) -> bool:
    """Constant-time bearer check on the Authorization header."""
    hdr = req.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        return False
    return secrets.compare_digest(hdr[7:], _INTERNAL_API_TOKEN)


def _require_auth():
    """Flask before_request-style guard for POST endpoints."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    return None


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/internal/status")
    def status():
        """Detector process stats. Web sidecar polls this ~1/sec."""
        return jsonify(preview._stats.snapshot())

    @app.get("/internal/frame")
    def frame():
        """Long-poll for the latest annotated JPEG.

        Query params:
            since=<int>   version returned in a previous fetch. Server waits
                          until version > since (max ``timeout`` seconds)
                          before returning. Enables efficient MJPEG streaming
                          without busy-polling.

        Response headers include ``X-Frame-Version`` so the client knows
        which version it just received.
        """
        try:
            last_seen = int(request.args.get("since", "-1"))
        except ValueError:
            last_seen = -1
        try:
            timeout = float(request.args.get("timeout", "5.0"))
        except ValueError:
            timeout = 5.0
        jpeg, version = preview._latest.get_next(last_seen=last_seen, timeout=timeout)
        if not jpeg:
            return Response(b"", status=204, headers={"X-Frame-Version": str(version)})
        return Response(
            jpeg, mimetype="image/jpeg",
            headers={"X-Frame-Version": str(version)},
        )

    @app.get("/internal/raw")
    def raw_frame():
        """Long-poll for the latest RAW (unannotated) JPEG. Used by the
        baseline-capture flow so overlays don't get baked into the reference."""
        try:
            last_seen = int(request.args.get("since", "-1"))
        except ValueError:
            last_seen = -1
        try:
            timeout = float(request.args.get("timeout", "2.0"))
        except ValueError:
            timeout = 2.0
        jpeg, version = preview._latest_raw.get_next(last_seen=last_seen, timeout=timeout)
        if not jpeg:
            return Response(b"", status=204, headers={"X-Frame-Version": str(version)})
        return Response(
            jpeg, mimetype="image/jpeg",
            headers={"X-Frame-Version": str(version)},
        )

    # ── Command endpoints (bearer-token-gated) ──────────────────────────────

    @app.post("/internal/zone")
    def post_zone():
        auth_err = _require_auth()
        if auth_err:
            return auth_err
        z = preview.get_zones()
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

    @app.post("/internal/masks")
    def post_masks():
        auth_err = _require_auth()
        if auth_err:
            return auth_err
        m = preview.get_masks()
        if m is None:
            return jsonify({"error": "mask editor not initialized"}), 503
        body = request.get_json(silent=True) or {}
        rects = body.get("masks", [])
        m.set_masks(rects, persist=True)
        _, ver = m.snapshot()
        return jsonify({"ok": True, "version": ver})

    @app.post("/internal/baseline/capture")
    def post_baseline_capture():
        auth_err = _require_auth()
        if auth_err:
            return auth_err
        b = preview.get_baseline()
        if b is None:
            return jsonify({"error": "baseline not initialized"}), 503
        # Pull the most-recent RAW frame (no overlays baked in).
        jpeg, _ = preview._latest_raw.get_next(last_seen=-1, timeout=2.0)
        try:
            mode = b.capture(jpeg)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "captured_mode": mode, **b.snapshot()})

    @app.post("/internal/baseline/clear")
    def post_baseline_clear():
        auth_err = _require_auth()
        if auth_err:
            return auth_err
        b = preview.get_baseline()
        if b is None:
            return jsonify({"error": "baseline not initialized"}), 503
        mode = (request.args.get("mode") or request.get_json(silent=True) or {}).get("mode") \
               if isinstance(request.get_json(silent=True), dict) else request.args.get("mode")
        b.clear(mode=mode)
        return jsonify({"ok": True, **b.snapshot()})

    @app.get("/internal/health")
    def health():
        """Trivial liveness check — no auth, no state peek. Web sidecar polls
        this to know if the detector is up."""
        return jsonify({"ok": True, "uptime_s": int(time.time() - preview._stats._start_ts)})

    return app


def start_in_thread(host: str = "127.0.0.1", port: int = 8101) -> None:
    """Start the detector's internal HTTP on a daemon thread.

    NEVER bind to 0.0.0.0 here — this surface is unauthenticated for reads
    and the write endpoints only have a shared-secret bearer. Loopback only.
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "Detector internal HTTP starting on %s — this surface should be "
            "loopback-only. Fix DETECTOR_INTERNAL_HOST.", host,
        )
    app = create_app()

    # Quiet werkzeug — internal API doesn't need per-request access logs.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def _run():
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    t = threading.Thread(target=_run, name="detector-internal-http", daemon=True)
    t.start()
    logger.info("Detector internal HTTP listening on http://%s:%d", host, port)
    logger.info("Internal API bearer token: %s%s",
                _INTERNAL_API_TOKEN[:6], "…" if len(_INTERNAL_API_TOKEN) > 6 else "")
    logger.info("Web sidecar must set INTERNAL_API_TOKEN to the same value.")


def get_token() -> str:
    """Callers in the same process (e.g. all-in-one main.py) can read the
    token to configure themselves."""
    return _INTERNAL_API_TOKEN
