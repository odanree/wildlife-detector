"""Alert dispatcher — headless (no dashboard).

For each positive detection:
  1. Save an annotated JPEG snapshot to SNAPSHOT_DIR.
  2. Fire a Home Assistant webhook if configured (HA_WEBHOOK_URL + HA_TOKEN).
  3. Fire a generic HTTP POST if configured (ALERT_WEBHOOK_URL).
  4. Log a structured DECISION line for grep-based observability.

Cooldown is per event_type (e.g. "rodent") — same event fires at most once
per cooldown_seconds window across all channels.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import httpx
import numpy as np

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(
        self,
        config: dict,
        ha_webhook_base: str = "",
        ha_token: str = "",
    ) -> None:
        self._cfg = config
        self._ha_base = ha_webhook_base.rstrip("/")
        self._ha_token = ha_token
        self._snapshot_dir = Path(config.get("snapshot_dir", "snapshots"))
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._last_fire: dict[str, float] = {}
        # 2026-10-01: camera_id snapshotted at init to disambiguate
        # filenames across concurrent detector containers sharing
        # ./snapshots. Without this, two cameras firing "other" events
        # within the same wall-clock second produced the same
        # f"{event_type}_{YYYYMMDD_HHMMSS}.jpg" filename and raced on
        # write — observed 2026-10-01 03:04:52 where garage_ptz and
        # crawlspace both wrote other_20261001_030452.jpg, leaving a
        # corrupt composite JPEG that the alerts UI then rendered as a
        # cross-camera overlay. Read from env (always present in detector
        # containers, see docker-compose.yml CAMERA_ID: <cam>) rather
        # than through config so this works without touching callers.
        self._camera_id = (os.environ.get("CAMERA_ID") or "").strip() or "cam"

    def send(
        self,
        event_type: str,
        vlm_result: dict,
        frame: np.ndarray,
        bbox: tuple[int, int, int, int] | None = None,
        yolo_conf: float | None = None,
    ) -> Path | None:
        snapshot_path = None
        if self._cfg.get("save_snapshot", True):
            snapshot_path = self._save_snapshot(event_type, frame, bbox, yolo_conf=yolo_conf, result=vlm_result)

        # bypass_min_confidence: set by pipeline's VLM-reject-override path
        # (conf=0.5 by design, "fire for human review"). Without this, the
        # override alerts were silently suppressed by the min_conf gate and
        # only snapshots landed. Sandbox on 2026-08-09 yard cat clip showed
        # 24 overrides fire but 0 ALERT log lines / 0 HA webhook / 0 DB row.
        _bypass = bool(vlm_result.get("bypass_min_confidence", False))
        min_conf = float(self._cfg.get("min_confidence", 0.70))
        if not _bypass and vlm_result.get("confidence", 0.0) < min_conf:
            logger.debug("Alert suppressed (%s) — confidence %.2f below %.2f",
                         event_type, vlm_result.get("confidence", 0.0), min_conf)
            return snapshot_path

        # bypass_cooldown: same rationale as bypass_min_confidence — the
        # VLM-reject-override path fires for human labeling, and dropping
        # 12/14 events to cooldown defeats the purpose. Positive VLM
        # verdicts still cooldown-throttled to avoid spam on continuous
        # tracks. Sandbox on 2026-08-09 yard cat clip: 14 overrides fired
        # but only 2 landed as ALERT lines due to 120s cooldown.
        _bypass_cooldown = bool(vlm_result.get("bypass_cooldown", False))
        cooldown = self._cfg.get("cooldown_seconds", {}).get(event_type, 120)
        now = time.monotonic()
        if not _bypass_cooldown and now - self._last_fire.get(event_type, 0.0) < cooldown:
            logger.debug("Alert suppressed (%s) — cooldown active", event_type)
            return snapshot_path
        self._last_fire[event_type] = now

        payload = {
            "event_type":  event_type,
            "timestamp":   datetime.now().isoformat(timespec="seconds"),
            "species":     vlm_result.get("species", "unknown"),
            "confidence":  vlm_result.get("confidence", 0.0),
            "description": vlm_result.get("description", ""),
            "snapshot":    snapshot_path.name if snapshot_path else None,
            "yolo_confidence": yolo_conf,
        }

        if self._cfg.get("home_assistant", {}).get("enabled"):
            threading.Thread(target=self._fire_ha, args=(event_type, payload), daemon=True).start()
        if self._cfg.get("generic_webhook", {}).get("enabled"):
            threading.Thread(target=self._fire_generic, args=(payload,), daemon=True).start()

        logger.info("ALERT %s species=%s conf=%.2f desc=%r",
                    event_type, payload["species"], payload["confidence"], payload["description"])
        return snapshot_path

    def _save_snapshot(
        self,
        event_type: str,
        frame: np.ndarray,
        bbox: tuple[int, int, int, int] | None,
        yolo_conf: float | None,
        result: dict,
    ) -> Path | None:
        try:
            out = frame.copy()
            fh, fw = out.shape[:2]
            # Diagnostic: report the frame shape + bbox at write time so we can
            # spot resolution/coord mismatches that put boxes 'outside the zone'.
            logger.info(
                "snapshot save: frame=%dx%d bbox=%s species=%s conf=%.2f",
                fw, fh, list(bbox) if bbox else None,
                result.get("species", "?"), result.get("confidence", 0.0),
            )
            if bbox is not None:
                x1, y1, x2, y2 = bbox
                color = (0, 0, 255)
                cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
                label = f"{result.get('species', '?')} {result.get('confidence', 0):.0%}"
                cv2.putText(out, label, (x1, max(y1 - 6, 14)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
            now = datetime.now()
            ts = now.strftime("%Y%m%d_%H%M%S")
            # Archive by date — snapshots land in snapshots/YYYY-MM-DD/ so the
            # top-level directory doesn't grow unbounded and the AlertLog
            # backfill can walk one day at a time.
            day_dir = self._snapshot_dir / now.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            # 2026-10-01: camera_id suffix prevents cross-container
            # filename collisions when two detectors fire the same
            # event_type within one wall-clock second. Suffix (not
            # prefix) so existing frontend logic that string-replaces
            # ".jpg" → ".thumb.jpg" still works, and existing path
            # lookups keyed by event_type_<ts> prefix still match via
            # glob. See _save_snapshot header comment for the bug.
            path = day_dir / f"{event_type}_{ts}_{self._camera_id}.jpg"
            cv2.imwrite(str(path), out, [cv2.IMWRITE_JPEG_QUALITY, 85])
            # Sibling thumbnail-annotated variant: same image but the
            # bbox is a solid red block. Operator's rapid-labeling flow
            # reads the thumbnail strip to pre-position attention on
            # where the next detection is; a 2px outline on a 1280→200
            # downscale renders sub-pixel and vanishes. A solid fill
            # survives the downscale as a red patch. Main lightbox still
            # loads the outline version. Same base name plus `.thumb.jpg`
            # so the frontend can infer the URL by string replace.
            if bbox is not None:
                thumb_path = day_dir / f"{event_type}_{ts}_{self._camera_id}.thumb.jpg"
                thumb_out = frame.copy()
                cv2.rectangle(thumb_out, (x1, y1), (x2, y2), (0, 0, 255), -1)
                cv2.imwrite(str(thumb_path), thumb_out, [cv2.IMWRITE_JPEG_QUALITY, 75])
            return path
        except Exception:
            logger.exception("Failed to save snapshot for %s", event_type)
            return None

    def _fire_ha(self, event_type: str, payload: dict) -> None:
        # STATE_DRY_RUN silences outbound integrations — replay / sandbox
        # runs shouldn't page Home Assistant on synthetic detections.
        if os.environ.get("STATE_DRY_RUN", "0") == "1":
            return
        webhook_id = self._cfg.get("home_assistant", {}).get(f"{event_type}_webhook_id")
        if not webhook_id or not self._ha_base:
            return
        url = f"{self._ha_base}/api/webhook/{webhook_id}"
        headers = {"Authorization": f"Bearer {self._ha_token}"} if self._ha_token else {}
        try:
            with httpx.Client(timeout=5.0) as c:
                c.post(url, json=payload, headers=headers).raise_for_status()
        except Exception:
            logger.warning("HA webhook %s failed", webhook_id, exc_info=True)

    def _fire_generic(self, payload: dict) -> None:
        if os.environ.get("STATE_DRY_RUN", "0") == "1":
            return
        cfg = self._cfg.get("generic_webhook", {})
        url = cfg.get("url")
        if not url:
            return
        headers = cfg.get("headers", {}) or {}
        try:
            with httpx.Client(timeout=5.0) as c:
                c.post(url, json=payload, headers=headers).raise_for_status()
        except Exception:
            logger.warning("Generic webhook failed", exc_info=True)
