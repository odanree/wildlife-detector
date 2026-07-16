"""Unit tests for the slew-to-zone dispatcher."""
from __future__ import annotations

import time
from unittest.mock import patch

from src.stream.slew import SlewDispatcher, ZonePreset, _point_in_polygon


LEFT_ZONE = [(0, 0), (640, 0), (640, 720), (0, 720)]
RIGHT_ZONE = [(640, 0), (1280, 0), (1280, 720), (640, 720)]


class TestPointInPolygon:
    def test_inside(self):
        assert _point_in_polygon(320, 360, LEFT_ZONE) is True

    def test_outside(self):
        assert _point_in_polygon(700, 360, LEFT_ZONE) is False

    def test_degenerate_polygon(self):
        assert _point_in_polygon(0, 0, [(0, 0), (1, 1)]) is False


class TestZoneLookup:
    def test_first_match_wins(self):
        d = SlewDispatcher(zones=[
            ZonePreset(name="left", polygon=LEFT_ZONE, preset=1),
            ZonePreset(name="right", polygon=RIGHT_ZONE, preset=2),
        ])
        assert d.zone_for_point(100, 100).preset == 1
        assert d.zone_for_point(1000, 100).preset == 2

    def test_no_match_returns_none(self):
        d = SlewDispatcher(zones=[
            ZonePreset(name="left", polygon=LEFT_ZONE, preset=1),
        ])
        assert d.zone_for_point(1000, 100) is None


class TestSlewLockout:
    def _dispatcher(self, lockout=1.0):
        return SlewDispatcher(
            zones=[ZonePreset(name="left", polygon=LEFT_ZONE, preset=7)],
            camera_id=1,
            lockout_seconds=lockout,
        )

    def test_first_slew_fires(self):
        d = self._dispatcher()
        with patch("src.stream.slew.ptz_preset", return_value=True) as mock_ptz:
            ok = d.slew_to_bbox(bbox=(100, 100, 200, 200), frame_width=1280, frame_height=720,
                                event_key=("rodent", 42))
            assert ok is True
            mock_ptz.assert_called_once_with(1, preset=7)

    def test_second_slew_within_lockout_suppressed(self):
        d = self._dispatcher(lockout=1.0)
        with patch("src.stream.slew.ptz_preset", return_value=True) as mock_ptz:
            d.slew_to_bbox(bbox=(100, 100, 200, 200), frame_width=1280, frame_height=720,
                           event_key=("rodent", 42))
            ok = d.slew_to_bbox(bbox=(100, 100, 200, 200), frame_width=1280, frame_height=720,
                                event_key=("rodent", 42))
            assert ok is False
            assert mock_ptz.call_count == 1

    def test_different_event_keys_do_not_share_lockout(self):
        d = self._dispatcher()
        with patch("src.stream.slew.ptz_preset", return_value=True) as mock_ptz:
            d.slew_to_bbox(bbox=(100, 100, 200, 200), frame_width=1280, frame_height=720,
                           event_key=("rodent", 1))
            d.slew_to_bbox(bbox=(100, 100, 200, 200), frame_width=1280, frame_height=720,
                           event_key=("rodent", 2))
            assert mock_ptz.call_count == 2

    def test_lockout_expires(self):
        d = self._dispatcher(lockout=0.05)
        with patch("src.stream.slew.ptz_preset", return_value=True) as mock_ptz:
            d.slew_to_bbox(bbox=(100, 100, 200, 200), frame_width=1280, frame_height=720,
                           event_key=("rodent", 42))
            time.sleep(0.08)
            ok = d.slew_to_bbox(bbox=(100, 100, 200, 200), frame_width=1280, frame_height=720,
                                event_key=("rodent", 42))
            assert ok is True
            assert mock_ptz.call_count == 2

    def test_ptz_failure_rolls_back_lockout(self):
        d = self._dispatcher(lockout=10.0)
        with patch("src.stream.slew.ptz_preset", return_value=False):
            ok = d.slew_to_bbox(bbox=(100, 100, 200, 200), frame_width=1280, frame_height=720,
                                event_key=("rodent", 42))
            assert ok is False
        with patch("src.stream.slew.ptz_preset", return_value=True) as mock_ptz2:
            ok = d.slew_to_bbox(bbox=(100, 100, 200, 200), frame_width=1280, frame_height=720,
                                event_key=("rodent", 42))
            assert ok is True
            mock_ptz2.assert_called_once()

    def test_no_matching_zone_is_noop(self):
        d = self._dispatcher()
        with patch("src.stream.slew.ptz_preset") as mock_ptz:
            ok = d.slew_to_bbox(bbox=(900, 100, 1000, 200), frame_width=1280, frame_height=720,
                                event_key=("rodent", 42))
            assert ok is False
            mock_ptz.assert_not_called()


class TestMaybeSlew:
    def test_disabled_by_default(self, monkeypatch):
        # SLEW_ENABLED unset → maybe_slew is a no-op even for a valid detection.
        monkeypatch.delenv("SLEW_ENABLED", raising=False)
        from src.stream.slew import maybe_slew, reset_dispatcher
        reset_dispatcher()
        with patch("src.stream.slew.ptz_preset") as mock_ptz:
            ok = maybe_slew(bbox=(100, 100, 200, 200), event_key=("rodent", 1),
                            frame_width=1280, frame_height=720)
            assert ok is False
            mock_ptz.assert_not_called()
