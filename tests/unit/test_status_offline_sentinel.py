"""Contract test for the /status offline sentinel.

Guarantees the sentinel returned when a detector's internal HTTP is
unreachable exposes the same key structure as the happy-path snapshot
from `Stats.snapshot()`. Without this, a future field added to
`Stats.snapshot()` can silently omit a key on the sentinel path,
crashing the frontend chips (ResourceChip/GateFunnelChip/CostChip)
with "Cannot read properties of undefined" and unmounting the whole
React tree — the 2026-09-26 preview-blank regression.

The test walks both dicts recursively and asserts the sentinel's key
set at every level is a SUPERSET of the snapshot's. Extra keys on the
sentinel are fine (forward-compat); missing keys are the bug.
"""
from __future__ import annotations

from src.web.preview import Stats
from src.web_service import offline_status_sentinel


def _key_shape(d: dict) -> dict:
    """Return only the KEY structure of a nested dict — values are
    replaced with `_key_shape` of themselves if dict, else None. This
    lets us compare shape without caring about scalar values."""
    return {k: _key_shape(v) if isinstance(v, dict) else None for k, v in d.items()}


def _assert_superset(sentinel: dict, snapshot: dict, path: str = "") -> None:
    """Recursively assert every key in `snapshot` exists in `sentinel`.
    Nested dicts recurse. Non-dict values only need the key present."""
    for key, snap_val in snapshot.items():
        loc = f"{path}.{key}" if path else key
        assert key in sentinel, (
            f"offline sentinel is missing key `{loc}` that Stats.snapshot() "
            f"emits. Add it to offline_status_sentinel() in src/web_service.py "
            f"— see the 2026-09-26 preview-blank regression."
        )
        if isinstance(snap_val, dict):
            assert isinstance(sentinel[key], dict), (
                f"offline sentinel has `{loc}` as {type(sentinel[key]).__name__} "
                f"but Stats.snapshot() emits it as a dict; the frontend expects "
                f"a dict here and will crash on property access otherwise."
            )
            _assert_superset(sentinel[key], snap_val, loc)


def test_offline_sentinel_covers_snapshot_keys():
    """The sentinel's key set must be a superset of Stats.snapshot()."""
    snapshot = Stats().snapshot()
    sentinel = offline_status_sentinel()
    _assert_superset(sentinel, snapshot)


def test_offline_sentinel_marks_resources_unavailable():
    """Frontend's ResourceChip renders "psutil unavailable" when
    `resources.available` is False. The sentinel must set it False so
    the chip degrades gracefully rather than pretending stale metrics
    are live."""
    sentinel = offline_status_sentinel()
    assert sentinel["resources"]["available"] is False


def test_offline_sentinel_backend_marker():
    """The sentinel's `backend` field is what the UI uses to show
    "detector offline" to the operator. Guard the exact string so a
    typo doesn't silently switch what the offline chip displays."""
    sentinel = offline_status_sentinel()
    assert sentinel["backend"] == "offline"
