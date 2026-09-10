"""Unit tests for the baseline mode picker dispatch and sun-based
day/night determination.

The picker is the piece that decides whether to route the pipeline
through `polygon` (night) or `polygon_day` (day) for cameras where
sun position — not scene brightness — is the right signal (e.g.
indoor crawlspace under permanent IR illumination).
"""
from __future__ import annotations

import importlib
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


# ── Sun-mode plumbing ────────────────────────────────────────────────


def _reload_preview():
    """Re-import so module-level env reads pick up monkeypatched vars."""
    import src.web.preview as preview
    importlib.reload(preview)
    return preview


def test_sun_mode_returns_night_when_coords_unset(monkeypatch):
    """No SUN_LAT/SUN_LON → safe fallback is 'night'. This matches the
    pre-fix crawlspace force-night behavior for operators who opt in
    but forget to fill in the coordinates."""
    monkeypatch.delenv("SUN_LAT", raising=False)
    monkeypatch.delenv("SUN_LON", raising=False)
    preview = _reload_preview()
    assert preview._detect_sun_mode() == "night"


def test_sun_mode_returns_night_at_zero_zero(monkeypatch):
    """Explicit (0, 0) coords (default fallback in the env parse) also
    route to the safe 'night' fallback."""
    monkeypatch.setenv("SUN_LAT", "0")
    monkeypatch.setenv("SUN_LON", "0")
    preview = _reload_preview()
    assert preview._detect_sun_mode() == "night"


def test_sun_mode_returns_night_on_bad_coords(monkeypatch):
    """Garbage in SUN_LAT/SUN_LON must not raise — the picker runs on
    the render hot path and any exception would take detection down."""
    monkeypatch.setenv("SUN_LAT", "not a number")
    monkeypatch.setenv("SUN_LON", "also not a number")
    preview = _reload_preview()
    assert preview._detect_sun_mode() == "night"


def test_sun_mode_day_at_noon_pacific(monkeypatch):
    """Freeze `datetime.now` inside the preview module to a fixed noon
    Pacific — sun is up over LA, expect 'day'."""
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")
    preview = _reload_preview()

    fixed_noon = datetime(2026, 9, 10, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_noon.astimezone(tz) if tz else fixed_noon

    monkeypatch.setattr("src.web.preview.datetime", FrozenDatetime, raising=False)
    # datetime is imported inside _detect_sun_mode, not at module scope,
    # so patch the datetime module lookup instead.
    import datetime as _dt_mod
    monkeypatch.setattr(_dt_mod, "datetime", FrozenDatetime)

    assert preview._detect_sun_mode() == "day"


def test_sun_mode_night_at_midnight_pacific(monkeypatch):
    """Midnight local time — sun is well below the horizon, expect 'night'."""
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")
    preview = _reload_preview()

    fixed_midnight = datetime(
        2026, 9, 10, 0, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"),
    )

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_midnight.astimezone(tz) if tz else fixed_midnight

    import datetime as _dt_mod
    monkeypatch.setattr(_dt_mod, "datetime", FrozenDatetime)

    assert preview._detect_sun_mode() == "night"


# ── Dispatch ─────────────────────────────────────────────────────────


def test_detect_mode_dispatches_to_sun_when_configured(monkeypatch):
    """BASELINE_MODE_SOURCE=sun routes through _detect_sun_mode
    regardless of what JPEG is supplied."""
    monkeypatch.setenv("BASELINE_MODE_SOURCE", "sun")
    monkeypatch.setenv("SUN_LAT", "0")  # forces 'night' fallback
    monkeypatch.setenv("SUN_LON", "0")
    preview = _reload_preview()

    # An obviously bright JPEG would be classified 'day' by the
    # brightness path — sun path should ignore the pixels entirely
    # and return 'night' because SUN_LAT/SUN_LON are zeroed.
    fake_bright_jpeg = b"\xff" * 4096
    assert preview._detect_mode(fake_bright_jpeg) == "night"


def test_detect_mode_defaults_to_brightness(monkeypatch):
    """Unset BASELINE_MODE_SOURCE preserves the original brightness
    routing — legacy cameras must not be silently switched to sun mode."""
    monkeypatch.delenv("BASELINE_MODE_SOURCE", raising=False)
    preview = _reload_preview()

    # Empty JPEG returns 'day' from the brightness path's fast return —
    # sun path with zero coords would return 'night', so this proves
    # dispatch went through brightness.
    assert preview._detect_mode(b"") == "day"
