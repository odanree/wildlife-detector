"""Unit tests for the sun-based zone-polygon mode picker.

Note: baseline picking is intentionally NOT tested here for sun mode
— this PR deliberately keeps baseline slot selection on the
pre-existing brightness path. Sun only chooses which zone polygon
fires; VLM prompt content, eyeshine gate, and baseline-diff
sensitivity all continue to read `_baseline_cache[0][1]`, which stays
brightness-driven. See Fable's #187 pass 1 review for the load-bearing
rationale.

Env reads happen at call time inside `_detect_sun_polygon_mode`, so
`monkeypatch.setenv` works without an import reload — no test-order
state leakage.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.web import preview


@pytest.fixture(autouse=True)
def _reset_fallback_flag(monkeypatch):
    """One-shot WARN suppression uses a module-level flag; reset it
    per-test so each test sees a fresh log opportunity."""
    monkeypatch.setattr(preview, "_sun_fallback_warned", False, raising=False)


# ── Fallback paths (should return "night" without raising) ───────────


def test_returns_night_when_coords_unset(monkeypatch):
    monkeypatch.delenv("SUN_LAT", raising=False)
    monkeypatch.delenv("SUN_LON", raising=False)
    assert preview._detect_sun_polygon_mode() == "night"


def test_returns_night_when_coord_is_set_but_empty(monkeypatch):
    """Set-but-empty is a common Docker/CI trap — env_file passes empty
    string for a var referenced without a default. Old code did
    `float(os.getenv('SUN_LAT', '') or 0)` which silently mapped empty
    to zero (Gulf of Guinea equator). Fable's F review pass 2 flagged
    the trap explicitly; the picker must now WARN + fall back."""
    monkeypatch.setenv("SUN_LAT", "")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    assert preview._detect_sun_polygon_mode() == "night"

    # Also check the other side — SUN_LON empty should not accept
    # SUN_LAT=33.7 as "half configured, roll with equator lon."
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "  ")   # whitespace also empty
    assert preview._detect_sun_polygon_mode() == "night"


def test_returns_night_when_coords_are_zero_zero(monkeypatch):
    monkeypatch.setenv("SUN_LAT", "0")
    monkeypatch.setenv("SUN_LON", "0")
    assert preview._detect_sun_polygon_mode() == "night"


def test_returns_night_on_unparseable_coords(monkeypatch):
    monkeypatch.setenv("SUN_LAT", "not a number")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    assert preview._detect_sun_polygon_mode() == "night"


def test_polar_latitude_returns_night_without_raising(monkeypatch):
    """North Pole in December — sun is well below the horizon all day.
    `astral.sun.sun()` would raise ValueError here; `elevation()`
    correctly returns a negative altitude → 'night' with no exception."""
    monkeypatch.setenv("SUN_LAT", "90")
    monkeypatch.setenv("SUN_LON", "0")

    # Freeze to Dec 21 UTC noon — polar night at the North Pole.
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 12, 21, 12, 0, 0, tzinfo=timezone.utc)
            return base.astimezone(tz) if tz else base
    import datetime as _dt_mod
    monkeypatch.setattr(_dt_mod, "datetime", FrozenDatetime)

    assert preview._detect_sun_polygon_mode() == "night"


def test_polar_latitude_summer_returns_day_without_raising(monkeypatch):
    """North Pole in June — midnight sun. `elevation()` returns
    positive altitude → 'day' without raising."""
    monkeypatch.setenv("SUN_LAT", "90")
    monkeypatch.setenv("SUN_LON", "0")

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
            return base.astimezone(tz) if tz else base
    import datetime as _dt_mod
    monkeypatch.setattr(_dt_mod, "datetime", FrozenDatetime)

    assert preview._detect_sun_polygon_mode() == "day"


def test_warn_once_flag_suppresses_repeat_fallback_logs(monkeypatch, caplog):
    """Fallback path is on the render hot path — a WARN every frame
    would spam. First fallback in a process lifetime logs; subsequent
    ones stay quiet."""
    import logging
    monkeypatch.delenv("SUN_LAT", raising=False)
    monkeypatch.delenv("SUN_LON", raising=False)

    with caplog.at_level(logging.WARNING, logger="src.web.preview"):
        preview._detect_sun_polygon_mode()  # should WARN
        preview._detect_sun_polygon_mode()  # should NOT
        preview._detect_sun_polygon_mode()  # should NOT

    warn_msgs = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warn_msgs) == 1, (
        f"expected exactly one WARN across three fallbacks, got "
        f"{len(warn_msgs)}: {[r.message for r in warn_msgs]}"
    )


# ── Happy paths (LA coords, timezone-independent) ───────────────────


def _freeze_now(monkeypatch, when_utc: datetime) -> None:
    """Patch `datetime.datetime` so `datetime.now(tz)` returns `when_utc`
    projected into the requested tz. Sun elevation is a function of the
    UTC instant, so we always drive with a UTC anchor."""
    assert when_utc.tzinfo is not None, "test bug: pass a tz-aware datetime"

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when_utc.astimezone(tz) if tz else when_utc.replace(tzinfo=None)

    import datetime as _dt_mod
    monkeypatch.setattr(_dt_mod, "datetime", FrozenDatetime)


def test_la_noon_summer_returns_day(monkeypatch):
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    # 12:00 PDT = 19:00 UTC. Timezone-independent computation.
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 19, 0, 0, tzinfo=timezone.utc))
    assert preview._detect_sun_polygon_mode() == "day"


def test_la_midnight_summer_returns_night(monkeypatch):
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    # 00:00 PDT = 07:00 UTC.
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 7, 0, 0, tzinfo=timezone.utc))
    assert preview._detect_sun_polygon_mode() == "night"


def test_la_just_before_sunrise_returns_night(monkeypatch):
    """05:30 PDT on Sep 10 in Orange County — before sunrise."""
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 12, 30, 0, tzinfo=timezone.utc))
    assert preview._detect_sun_polygon_mode() == "night"


def test_la_just_after_sunrise_returns_day(monkeypatch):
    """08:00 PDT on Sep 10 in Orange County — after sunrise."""
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 15, 0, 0, tzinfo=timezone.utc))
    assert preview._detect_sun_polygon_mode() == "day"


def test_utc_configured_container_still_computes_sun(monkeypatch):
    """No SUN_TZ / TZ=UTC on the container must not force silent
    'night' fallback. Sun elevation is timezone-independent; the
    previous SUN_TZ guard was dead logic that Fable pass 2 caught."""
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.delenv("SUN_TZ", raising=False)
    monkeypatch.setenv("TZ", "UTC")
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 19, 0, 0, tzinfo=timezone.utc))
    # LA noon PDT ⇒ sun clearly up ⇒ "day" regardless of container TZ.
    assert preview._detect_sun_polygon_mode() == "day"


# ── Split invariant: baseline stays brightness, polygon follows sun ──


def test_split_baseline_brightness_and_polygon_sun_are_independent(monkeypatch):
    """Pins the load-bearing invariant from Fable's F review: for a
    crawlspace-shaped config (DAY_NIGHT_BRIGHTNESS_THRESHOLD=255 forces
    baseline to always be 'night'), sun-mode polygon picking can still
    return 'day' during daylight — and the two decisions are made by
    independent code paths.

    Setattr on the module global directly (rather than importlib.reload)
    per Fable pass 3 — the brightness function reads the module-level
    threshold at each call, so monkeypatch.setattr auto-restores after
    the test and doesn't leak state to the rest of the session."""
    monkeypatch.setattr(preview, "_DAY_NIGHT_THRESHOLD", 255)

    # Even an "obviously bright" JPEG can't cross threshold=255.
    import numpy as np
    import cv2
    bright = np.full((100, 100), 254, dtype=np.uint8)
    _, jpeg_buf = cv2.imencode(".jpg", bright)
    bright_jpeg = jpeg_buf.tobytes()
    assert preview._detect_brightness_mode(bright_jpeg) == "night"

    # And at LA noon, sun path returns day for the polygon.
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 19, 0, 0, tzinfo=timezone.utc))
    assert preview._detect_sun_polygon_mode() == "day"

    # The two functions do not share state — different discriminators
    # for different concerns.


def test_returns_night_on_nan_inf_and_out_of_range_coords(monkeypatch):
    """`float()` happily parses NaN/Inf/1e400, and astral silently
    returns plausible-looking garbage for latitude=95. Fable pass 3
    flagged as the same shape as the empty-string trap — the guard
    must reject these explicitly."""
    for lat_raw in ("nan", "inf", "1e400", "-inf"):
        monkeypatch.setenv("SUN_LAT", lat_raw)
        monkeypatch.setenv("SUN_LON", "-117.8677")
        monkeypatch.setattr(preview, "_sun_fallback_warned", False)
        assert preview._detect_sun_polygon_mode() == "night", f"lat_raw={lat_raw}"

    # Out-of-range latitude (astral just returns garbage).
    monkeypatch.setenv("SUN_LAT", "95.0")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.setattr(preview, "_sun_fallback_warned", False)
    assert preview._detect_sun_polygon_mode() == "night"

    # Out-of-range longitude.
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "200.0")
    monkeypatch.setattr(preview, "_sun_fallback_warned", False)
    assert preview._detect_sun_polygon_mode() == "night"


# NOTE on the -0.833° threshold: the code uses
# `elevation(..., with_refraction=False)` with `alt > -0.833`. The -0.833°
# figure = 0.567° atmospheric refraction correction + 0.267° solar
# semi-diameter, and matches astral's own sunrise()/sunset() to within
# ~seconds (measured Sep 10, LA coords: sunrise flip at astral_sunrise
# − 7s). A tight boundary test isn't feasible here — monkey-patching
# `datetime.datetime` to freeze `now()` pollutes astral's internal
# datetime usage and corrupts the elevation computation itself
# (verified: at -30s from astral's sunrise, direct-call astral returns
# geom=-0.895° (night), but the same call under a monkey-patched clock
# returns geom=-0.816° (day) because astral's internal `datetime.combine`
# resolves against the patched class). The right fix would be
# freezegun or a `now` parameter on the picker — deferred as a
# nice-to-have; existing pre-sunrise/post-sunrise tests cover the
# ±30-min accuracy needed for polygon selection.
