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

    @app.post("/internal/slew/presets")
    def post_slew_presets():
        """Persist per-preset polygons to detection.yaml under
        slew.<CAMERA_ID>.presets. Preserves scalar fields (enabled,
        camera_id, home_preset, etc.) unless the caller overwrites them
        explicitly. Also resets the SelfSlewController singleton so the
        running pipeline picks up the new polygons on the next positive
        detection — no detector restart needed.

        Pattern: **write-through cache with reset on invalidate** — yaml
        is the source of truth; the in-memory controller is a cache
        that's dropped whenever the yaml changes.
        """
        auth_err = _require_auth()
        if auth_err:
            return auth_err
        import yaml as _yaml
        camera_id = os.getenv("CAMERA_ID", "")
        if not camera_id:
            return jsonify({"error": "CAMERA_ID env not set"}), 500
        cfg_path = os.getenv("DETECTION_CFG", "config/detection.yaml")
        body = request.get_json(silent=True) or {}
        presets_in = body.get("presets")
        if presets_in is None or not isinstance(presets_in, list):
            return jsonify({"error": "body must include 'presets' list"}), 400

        # Validate: each entry needs name (str) + preset (int) + polygon
        # (list of [x,y] pairs, 3+ points, values in [0,1]).
        cleaned = []
        for i, p in enumerate(presets_in):
            if not isinstance(p, dict):
                return jsonify({"error": f"preset[{i}] must be an object"}), 400
            try:
                name = str(p.get("name") or f"preset_{i}")
                preset_num = int(p.get("preset"))
                poly = p.get("polygon") or []
                if len(poly) < 3:
                    return jsonify({"error": f"preset[{i}] polygon needs 3+ points"}), 400
                poly_clean = []
                for pt in poly:
                    x, y = float(pt[0]), float(pt[1])
                    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                        return jsonify({"error": f"preset[{i}] point ({x},{y}) out of [0,1]"}), 400
                    poly_clean.append([x, y])
                cleaned.append({"name": name, "preset": preset_num, "polygon": poly_clean})
            except (TypeError, ValueError, KeyError) as e:
                return jsonify({"error": f"preset[{i}] invalid: {e}"}), 400

        # Load-modify-save, preserving other slew.<camera_id> fields.
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = _yaml.safe_load(fh) or {}
        except FileNotFoundError:
            cfg = {}
        slew_block = cfg.setdefault("slew", {}).setdefault(camera_id, {})
        slew_block["presets"] = cleaned
        for k in ("enabled", "camera_id", "home_preset",
                  "return_home_after_s", "lockout_seconds", "transition_pause_s"):
            if k in body:
                slew_block[k] = body[k]

        tmp = cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _yaml.safe_dump(cfg, fh, sort_keys=False)
        os.replace(tmp, cfg_path)

        try:
            from src.stream.self_slew import reset_controller
            reset_controller()
        except Exception:
            logger.exception("slew: reset_controller failed after preset save")
        return jsonify({"ok": True, "count": len(cleaned)})

    @app.post("/internal/ptz/preset")
    def post_ptz_preset():
        """Manually command a PTZ preset. Used by the slew-preset editor
        so the operator can 'go here' to verify what a given preset
        looks like before drawing its polygon.

        Body: {"preset": <int>, "camera_id": <int-optional>}
        camera_id defaults to the PTZ target from this detector's
        slew.<CAMERA_ID>.camera_id config.
        """
        auth_err = _require_auth()
        if auth_err:
            return auth_err
        import yaml as _yaml
        body = request.get_json(silent=True) or {}
        try:
            preset = int(body.get("preset"))
        except (TypeError, ValueError):
            return jsonify({"error": "body must include integer 'preset'"}), 400
        cam_id = body.get("camera_id")
        if cam_id is None:
            our_cam = os.getenv("CAMERA_ID", "")
            cfg_path = os.getenv("DETECTION_CFG", "config/detection.yaml")
            try:
                with open(cfg_path, encoding="utf-8") as fh:
                    cfg = _yaml.safe_load(fh) or {}
                cam_id = ((cfg.get("slew") or {}).get(our_cam) or {}).get("camera_id", 2)
            except FileNotFoundError:
                cam_id = 2
        try:
            cam_id = int(cam_id)
        except (TypeError, ValueError):
            return jsonify({"error": "camera_id must be int"}), 400
        from src.stream.ptz import ptz_preset as _ptz_preset
        ok = _ptz_preset(camera_id=cam_id, preset=preset)
        return jsonify({"ok": ok, "camera_id": cam_id, "preset": preset})

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
        # Optional ?mode=day|night override — bypasses the brightness auto-picker
        # which misclassifies IR-lit foliage as "day". The BaselineHolder's
        # capture() defaults to auto when mode is missing; pass through as-is.
        mode_override = (request.args.get("mode") or "").lower().strip()
        try:
            if mode_override in ("day", "night"):
                mode = b.capture(jpeg, mode=mode_override)
            else:
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
        # Query-string wins, JSON body falls back. Prior version had a
        # broken precedence expression that called `.get("mode")` on a
        # bare string when both were supplied → AttributeError. Also
        # called get_json twice per request. Issue #31.
        mode = request.args.get("mode") or (request.get_json(silent=True) or {}).get("mode")
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
