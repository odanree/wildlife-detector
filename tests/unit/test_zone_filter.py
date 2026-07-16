"""Basic zone filter smoke tests."""
from __future__ import annotations

from src.detection.object_detector import Detection
from src.detection.zone_filter import ZoneFilter


def _det(track_id: int, cx: int, cy: int) -> Detection:
    half = 30
    return Detection(
        track_id=track_id, class_name="cat", confidence=0.9,
        bbox=(cx - half, cy - half, cx + half, cy + half),
    )


SQUARE_ZONE = [[100, 100], [500, 100], [500, 500], [100, 500]]


class TestZoneFilter:
    def setup_method(self):
        self.zf = ZoneFilter(zones={"yard_zone": SQUARE_ZONE})

    def test_inside_passes(self):
        d = _det(1, 300, 300)
        assert self.zf.filter([d], "yard_zone") == [d]

    def test_outside_rejected(self):
        d = _det(2, 50, 50)
        assert self.zf.filter([d], "yard_zone") == []
