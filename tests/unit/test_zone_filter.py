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


class TestZoneFilterDegenerateFallback:
    """Regression for #195. Empty / 1-vertex / 2-vertex polygons must fall
    back to full-frame instead of crashing cv2.pointPolygonTest. Reproduces
    the crash observed during the detector-crawlspace-inside cutover
    (PR #194) when the new zone hadn't been drawn in detection.yaml yet."""

    def test_empty_polygon_passes_all_through(self):
        zf = ZoneFilter(zones={"new_camera_zone": []})
        detections = [_det(1, 100, 100), _det(2, 500, 500)]
        assert zf.filter(detections, "new_camera_zone") == detections

    def test_single_vertex_polygon_passes_all_through(self):
        zf = ZoneFilter(zones={"degenerate": [[42, 42]]})
        detections = [_det(3, 200, 200)]
        assert zf.filter(detections, "degenerate") == detections

    def test_two_vertex_polygon_passes_all_through(self):
        zf = ZoneFilter(zones={"line_not_polygon": [[0, 0], [100, 100]]})
        detections = [_det(4, 50, 50)]
        assert zf.filter(detections, "line_not_polygon") == detections

    def test_three_vertex_polygon_actually_filters(self):
        """Sanity: the >=3 threshold is a real boundary, not a passthrough."""
        triangle = [[100, 100], [500, 100], [300, 500]]
        zf = ZoneFilter(zones={"triangle_zone": triangle})
        inside = _det(5, 300, 200)
        outside = _det(6, 50, 50)
        assert zf.filter([inside, outside], "triangle_zone") == [inside]
