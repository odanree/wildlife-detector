"""Unit tests for build_nvr_playback_url — covers the Dahua default,
the Hikvision family flag, and the per-camera env override layer used
by the two-NVR shape introduced 2026-09-11."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from src.stream.playback_url import build_nvr_playback_url


@pytest.fixture(autouse=True)
def _clean_nvr_env(monkeypatch):
    """Every test starts from a scrubbed slate — the module reads envs
    on each call, so lingering per-camera vars from earlier tests would
    silently steer this one."""
    for k in list(os.environ):
        if k.startswith(("NVR_", "AMCREST_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NVR_TZ", "America/Los_Angeles")
    yield


ALERT_TS = datetime(2026, 9, 11, 15, 30, 0, tzinfo=timezone.utc).timestamp()


class TestDahuaDefault:
    def test_uses_amcrest_env_and_cam_playback_shape(self, monkeypatch):
        monkeypatch.setenv("AMCREST_HOST", "192.168.1.148")
        monkeypatch.setenv("AMCREST_USER", "admin")
        monkeypatch.setenv("AMCREST_PASS", "windows98")

        url = build_nvr_playback_url(
            timestamp=ALERT_TS, pre_roll_seconds=15, nvr_channel=5,
        )

        assert "rtsp://admin:windows98@192.168.1.148:554" in url
        assert "/cam/playback?" in url
        assert "channel=5" in url
        assert "subtype=0" in url
        # Amcrest uses NVR-local wallclock, not UTC — with NVR_TZ=Pacific
        # a 15:30 UTC alert becomes 08:30 Pacific.
        assert "starttime=2026_09_11_08_30_00".replace("2026_09_11_08_30_00",
                                                        "2026_09_11_08_29_45") in url  # 15s pre-roll → 08:29:45


class TestHikvisionFamily:
    def test_uses_streaming_tracks_shape_and_utc_iso(self, monkeypatch):
        monkeypatch.setenv("AMCREST_HOST", "192.168.1.148")
        monkeypatch.setenv("AMCREST_USER", "admin")
        monkeypatch.setenv("AMCREST_PASS", "windows98")
        # Per-camera Annke overrides for this camera
        monkeypatch.setenv("NVR_HOST_CRAWLSPACE_INSIDE", "192.168.1.130")
        monkeypatch.setenv("NVR_USER_CRAWLSPACE_INSIDE", "admin")
        monkeypatch.setenv("NVR_PASS_CRAWLSPACE_INSIDE", "Windows98")
        monkeypatch.setenv("NVR_FAMILY_CRAWLSPACE_INSIDE", "hikvision")

        url = build_nvr_playback_url(
            timestamp=ALERT_TS, pre_roll_seconds=15, nvr_channel=1,
            camera_id="crawlspace_inside",
        )

        assert "rtsp://admin:Windows98@192.168.1.130:554" in url
        assert "/Streaming/tracks/101/" in url
        # UTC ISO-8601 without separators: YYYYMMDDTHHMMSSZ
        assert "starttime=20260911T152945Z" in url
        # endtime is alert + 2 min post-roll (see build_nvr_playback_url).
        assert "endtime=20260911T153200Z" in url
        assert "/cam/playback" not in url
        assert "subtype=0" not in url

    def test_family_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("NVR_HOST_CAM", "192.168.1.130")
        monkeypatch.setenv("NVR_USER_CAM", "u")
        monkeypatch.setenv("NVR_PASS_CAM", "p")
        monkeypatch.setenv("NVR_FAMILY_CAM", "HIKVISION")

        url = build_nvr_playback_url(
            timestamp=ALERT_TS, nvr_channel=1, camera_id="cam",
        )
        assert "/Streaming/tracks/" in url


class TestPerCameraOverride:
    def test_missing_override_falls_back_to_amcrest(self, monkeypatch):
        monkeypatch.setenv("AMCREST_HOST", "192.168.1.148")
        monkeypatch.setenv("AMCREST_USER", "admin")
        monkeypatch.setenv("AMCREST_PASS", "windows98")
        # No NVR_HOST_YARD — yard should ride the fleet default.

        url = build_nvr_playback_url(
            timestamp=ALERT_TS, nvr_channel=5, camera_id="yard",
        )
        assert "192.168.1.148" in url
        assert "windows98" in url

    def test_partial_override_only_replaces_named_keys(self, monkeypatch):
        """A camera can override just HOST while inheriting AMCREST creds."""
        monkeypatch.setenv("AMCREST_HOST", "192.168.1.148")
        monkeypatch.setenv("AMCREST_USER", "admin")
        monkeypatch.setenv("AMCREST_PASS", "fleetpass")
        monkeypatch.setenv("NVR_HOST_SPECIAL", "192.168.1.200")
        # No NVR_USER_SPECIAL / NVR_PASS_SPECIAL — inherit fleet creds.

        url = build_nvr_playback_url(
            timestamp=ALERT_TS, nvr_channel=1, camera_id="special",
        )
        assert "192.168.1.200" in url
        assert "admin:fleetpass" in url

    def test_empty_camera_id_uses_amcrest_only(self, monkeypatch):
        """No camera_id → no per-camera lookup attempted at all."""
        monkeypatch.setenv("AMCREST_HOST", "192.168.1.148")
        monkeypatch.setenv("AMCREST_USER", "admin")
        monkeypatch.setenv("AMCREST_PASS", "pw")

        url = build_nvr_playback_url(timestamp=ALERT_TS, nvr_channel=1)
        assert "192.168.1.148" in url
        assert "/cam/playback" in url  # default family = dahua


class TestDahuaTimestampFormatting:
    """Regression: Dahua expects the NVR's local clock, not UTC."""

    def test_uses_local_time_from_nvr_tz(self, monkeypatch):
        monkeypatch.setenv("AMCREST_HOST", "10.0.0.5")
        monkeypatch.setenv("AMCREST_USER", "u")
        monkeypatch.setenv("AMCREST_PASS", "p")

        # ALERT_TS is 2026-09-11 15:30 UTC = 08:30 Pacific (PDT, UTC-7)
        url = build_nvr_playback_url(
            timestamp=ALERT_TS, pre_roll_seconds=0, nvr_channel=1,
        )
        # No pre-roll → starttime at 08:30 Pacific sharp.
        assert "starttime=2026_09_11_08_30_00" in url
