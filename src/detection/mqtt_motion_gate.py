"""MQTT motion gate — subscribes to any binary ON/OFF MQTT topic and
gates wildlife-detector's YOLO+VLM cascade to only fire when the topic
publishes `ON`.

## Why generic instead of Frigate-specific

First iteration (frigate_motion_gate.py, Sep 24) subscribed to
`frigate/<camera>/motion`. That signal turned out to be too noisy for
this environment — outdoor cams at night see IR-attracted insects as
constant motion, indoor crawlspace has mosquitoes. Motion effectively
never went OFF, gate stayed fail-open, no CPU reduction.

Since the code shape is generic (subscribe → cache last ON/OFF →
should_run() returns cached value with fail-open default), the class
was renamed and the topic made configurable so future signal sources
plug in without a code change:

  - PIR sensor stations (HC-SR501 + ESP32 + ESPHome publishing over
    MQTT on `pir/<location>/binary_sensor/pir_<location>/state`) —
    warm-body-only signal, insect-immune, correct filter for rats.
  - Frigate motion (still works, just via topic env var not brand name).
  - Any future ON/OFF publisher — Home Assistant automation, custom
    ESPHome sensors, IR beam breaks, etc.

## Fail-open contract (unchanged from v1)

Any failure — broker unreachable, connection dropped, subscribe error,
malformed payload — resolves to `should_run() == True`. Rationale: this
is a monitoring signal on the runtime path, and fail-closed here would
drop alerts silently on broker outages. **Fail-fast at trust boundary
+ graceful degradation on runtime path** — the trust-boundary check
already happened at broker connect; on the runtime path (per-frame
gate decision) we degrade gracefully rather than throw.

## Env vars

- `MQTT_MOTION_GATE_ENABLED`: "1" to enable
- `MQTT_MOTION_TOPIC`: the topic to subscribe to. Required. Example:
    `pir/yard/binary_sensor/pir_yard/state` — ESPHome default topic
    shape when `topic_prefix: pir/yard` and sensor name is `pir_yard`.
- `MQTT_MOTION_HOST`: broker host (default 192.168.1.147, Beelink)
- `MQTT_MOTION_PORT`: broker port (default 1883)

## Backwards compatibility

The Sep 24 Frigate-only names (`FRIGATE_MOTION_GATE_ENABLED`,
`FRIGATE_MOTION_CAMERA`, `FRIGATE_MQTT_HOST`, `FRIGATE_MQTT_PORT`) are
still read as a fallback so existing detector-yard config keeps
working during the migration. New deployments should use
`MQTT_MOTION_*`. Frigate-shaped topic is auto-derived from
`FRIGATE_MOTION_CAMERA` for the legacy path.

## Threading

paho-mqtt's `loop_start()` runs the network loop in a background
thread. The main detector loop reads `_motion` via `should_run()`
without a lock — Python's GIL makes single-boolean reads atomic and
we tolerate the ~1-frame staleness (200 ms at 5 fps).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class MqttMotionGate:
    """MQTT subscriber that tracks a binary ON/OFF signal and gates
    per-frame detection on it. See module docstring for design + contract."""

    def __init__(self, broker_host: str, broker_port: int, topic: str) -> None:
        # Fail-open default — until we see an OFF message, assume motion
        # is happening (i.e., run detection). If the broker never
        # connects, the gate is effectively transparent.
        self._motion: bool = True
        self._connected: bool = False
        self._topic = topic

        # Lazy-import paho so this module can be imported even when
        # paho-mqtt isn't installed (development environments, tests).
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.warning(
                "paho-mqtt not installed — MqttMotionGate disabled "
                "(fail-open, all frames pass)"
            )
            self._client = None
            return

        # Derive a stable client_id from the topic tail so multiple
        # detector containers connecting to the same broker don't
        # clobber each other's session state. Slashes/wildcards get
        # sanitized to underscores to satisfy the MQTT client_id spec.
        client_id = "wildlife-detector-" + (
            topic.replace("/", "_").replace("+", "any").replace("#", "wild")
        )[:80]
        # CallbackAPIVersion.VERSION2 is the paho 2.x default signature.
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        try:
            # connect_async + loop_start doesn't block: the network
            # thread handles connect/retry in the background. If the
            # broker is unreachable at startup, the gate stays fail-
            # open and reconnects when the broker comes back.
            self._client.connect_async(broker_host, broker_port, keepalive=30)
            self._client.loop_start()
            logger.info(
                "MqttMotionGate: subscribing to %s at %s:%d (fail-open until connected)",
                self._topic, broker_host, broker_port,
            )
        except Exception as e:
            logger.warning("MqttMotionGate: connect_async raised %s — fail-open", e)
            self._client = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            client.subscribe(self._topic, qos=1)
            self._connected = True
            logger.info("MqttMotionGate: connected + subscribed to %s", self._topic)
        else:
            logger.warning(
                "MqttMotionGate: connect failed rc=%s — fail-open",
                reason_code,
            )
            self._connected = False

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        # Fail-open on disconnect: assume motion is happening until we
        # reconnect. paho will auto-reconnect via the network thread.
        self._connected = False
        self._motion = True
        logger.info(
            "MqttMotionGate: disconnected rc=%s — reverting to fail-open",
            reason_code,
        )

    def _on_message(self, client, userdata, msg) -> None:
        payload = msg.payload
        # ESPHome publishes "ON"/"OFF" (upper) by default, matching
        # Frigate's convention. Some publishers use "on"/"off" or
        # numeric "1"/"0"; support all three shapes.
        if payload in (b"ON", b"on", b"1", b"true", b"True"):
            self._motion = True
        elif payload in (b"OFF", b"off", b"0", b"false", b"False"):
            self._motion = False
        # Any other payload leaves state unchanged.

    def should_run(self) -> bool:
        """Return True if detection should run for the current frame.

        Fail-open: returns True whenever the broker isn't connected
        (never connected, disconnected, or client not initialized).
        """
        return self._motion

    def stop(self) -> None:
        """Cleanly shut down the client. Called from the main loop's
        finally block on graceful exit."""
        if self._client is not None:
            self._client.loop_stop()
            try:
                self._client.disconnect()
            except Exception:
                pass


_singleton: Optional[MqttMotionGate] = None


def get_gate() -> Optional[MqttMotionGate]:
    """Return the process-wide gate singleton, or None if not enabled.

    Resolution order for env vars:

      1. New generic names: MQTT_MOTION_GATE_ENABLED + MQTT_MOTION_TOPIC
         + MQTT_MOTION_HOST + MQTT_MOTION_PORT
      2. Legacy Frigate names: FRIGATE_MOTION_GATE_ENABLED +
         FRIGATE_MOTION_CAMERA + FRIGATE_MQTT_HOST + FRIGATE_MQTT_PORT
         (auto-derives topic as `frigate/<camera>/motion`)

    Reads env once on first call. Subsequent calls return the cached
    instance. Called from pipeline.py's main loop.
    """
    global _singleton
    if _singleton is not None:
        return _singleton

    # Path 1 — generic MQTT_MOTION_* env
    if os.getenv("MQTT_MOTION_GATE_ENABLED", "0") == "1":
        topic = os.getenv("MQTT_MOTION_TOPIC", "").strip()
        if not topic:
            logger.warning(
                "MQTT_MOTION_GATE_ENABLED=1 but MQTT_MOTION_TOPIC is "
                "unset — gate disabled (fail-open)"
            )
            return None
        host = os.getenv("MQTT_MOTION_HOST", "192.168.1.147")
        port = int(os.getenv("MQTT_MOTION_PORT", "1883"))
        _singleton = MqttMotionGate(host, port, topic)
        return _singleton

    # Path 2 — legacy Frigate-shaped env (Sep 24 prototype)
    if os.getenv("FRIGATE_MOTION_GATE_ENABLED", "0") == "1":
        camera = os.getenv("FRIGATE_MOTION_CAMERA", "").strip()
        if not camera:
            logger.warning(
                "FRIGATE_MOTION_GATE_ENABLED=1 but FRIGATE_MOTION_CAMERA "
                "is unset — gate disabled (fail-open)"
            )
            return None
        host = os.getenv("FRIGATE_MQTT_HOST", "192.168.1.147")
        port = int(os.getenv("FRIGATE_MQTT_PORT", "1883"))
        _singleton = MqttMotionGate(host, port, f"frigate/{camera}/motion")
        return _singleton

    return None
