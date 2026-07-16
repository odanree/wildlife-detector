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

        min_conf = float(self._cfg.get("min_confidence", 0.70))
        if vlm_result.get("confidence", 0.0) < min_conf:
            logger.debug("Alert suppressed (%s) — confidence %.2f below %.2f",
                         event_type, vlm_result.get("confidence", 0.0), min_conf)
            return snapshot_path

        cooldown = self._cfg.get("cooldown_seconds", {}).get(event_type, 120)
        now = time.monotonic()
        if now - self._last_fire.get(event_type, 0.0) < cooldown:
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
            if bbox is not None:
                x1, y1, x2, y2 = bbox
                color = (0, 0, 255)
                cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
                label = f"{result.get('species', '?')} {result.get('confidence', 0):.0%}"
                cv2.putText(out, label, (x1, max(y1 - 6, 14)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self._snapshot_dir / f"{event_type}_{ts}.jpg"
            cv2.imwrite(str(path), out, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return path
        except Exception:
            logger.exception("Failed to save snapshot for %s", event_type)
            return None

    def _fire_ha(self, event_type: str, payload: dict) -> None:
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
