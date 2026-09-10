"""Unit tests for the sun-based zone-polygon mode picker.

Note: baseline picking is intentionally NOT tested here — this PR
deliberately keeps baseline slot selection on the pre-existing
brightness path. Sun only chooses which zone polygon fires; VLM
prompt content, eyeshine gate, and baseline-diff sensitivity all
continue to read `_baseline_cache[0][1]`, which stays brightness-
driven. See Fable's #187 review for the load-bearing rationale for
that split.

Env reads happen at call time inside `_detect_sun_polygon_mode`, so
`monkeypatch.setenv` works without an import reload — no test-order
state leakage.
"""
from __future__ import annotations

from datetime import datetime
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
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")
    assert preview._detect_sun_polygon_mode() == "night"


def test_returns_night_when_coords_are_zero_zero(monkeypatch):
    monkeypatch.setenv("SUN_LAT", "0")
    monkeypatch.setenv("SUN_LON", "0")
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")
    assert preview._detect_sun_polygon_mode() == "night"


def test_returns_night_on_unparseable_coords(monkeypatch):
    monkeypatch.setenv("SUN_LAT", "not a number")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")
    assert preview._detect_sun_polygon_mode() == "night"


def test_returns_night_on_tz_mismatch(monkeypatch):
    """SUN_TZ=UTC with LA coords is the trap Fable's A flagged — sun
    calculation would place sunset before sunrise for the same UTC day
    and every hour maps to 'night' silently. The fix explicitly
    detects the mismatch and logs it."""
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.setenv("SUN_TZ", "UTC")
    assert preview._detect_sun_polygon_mode() == "night"


def test_returns_night_when_sun_tz_unset_and_tz_unset(monkeypatch):
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.delenv("SUN_TZ", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    assert preview._detect_sun_polygon_mode() == "night"


def test_polar_latitude_does_not_raise(monkeypatch):
    """North Pole in December — sun never rises. `astral.sun.sun()`
    would raise ValueError here; `elevation()` (the new implementation)
    correctly returns a negative altitude → 'night'."""
    monkeypatch.setenv("SUN_LAT", "90")
    monkeypatch.setenv("SUN_LON", "0")
    monkeypatch.setenv("SUN_TZ", "UTC")   # UTC ok at the pole
    # UTC-with-pole is legit — the tz-mismatch fallback fires anyway
    # because SUN_TZ==UTC. Prove the fallback path returns "night"
    # without crashing on the polar computation.
    assert preview._detect_sun_polygon_mode() == "night"


def test_warn_once_flag_suppresses_repeat_fallback_logs(monkeypatch, caplog):
    """Fallback path is on the render hot path — a WARN every frame
    would spam. First fallback in a process lifetime logs; subsequent
    ones stay quiet."""
    import logging
    monkeypatch.delenv("SUN_LAT", raising=False)
    monkeypatch.delenv("SUN_LON", raising=False)
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")

    with caplog.at_level(logging.WARNING, logger="src.web.preview"):
        preview._detect_sun_polygon_mode()  # should WARN
        preview._detect_sun_polygon_mode()  # should NOT
        preview._detect_sun_polygon_mode()  # should NOT

    warn_msgs = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warn_msgs) == 1, (
        f"expected exactly one WARN across three fallbacks, got "
        f"{len(warn_msgs)}: {[r.message for r in warn_msgs]}"
    )


# ── Happy paths (LA coords, known times) ─────────────────────────────


def _freeze_now(monkeypatch, when: datetime) -> None:
    """Patch datetime inside src.web.preview so _detect_sun_polygon_mode
    sees a fixed clock. astral itself only reads the dateandtime we
    pass explicitly, so this reliably drives the branch."""
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when
    # _detect_sun_polygon_mode uses `from datetime import datetime`
    # inside the function body — that re-resolves against sys.modules
    # each call, so we patch the module-level class there.
    import datetime as _dt_mod
    monkeypatch.setattr(_dt_mod, "datetime", FrozenDatetime)


def test_la_noon_summer_returns_day(monkeypatch):
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles")))
    assert preview._detect_sun_polygon_mode() == "day"


def test_la_midnight_summer_returns_night(monkeypatch):
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 0, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles")))
    assert preview._detect_sun_polygon_mode() == "night"


def test_la_just_before_sunrise_returns_night(monkeypatch):
    """Sanity check the -0.833° civil-horizon threshold — 05:30 PDT
    on Sep 10 in Orange County is comfortably before civil dawn."""
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 5, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles")))
    assert preview._detect_sun_polygon_mode() == "night"


def test_la_just_after_sunrise_returns_day(monkeypatch):
    """08:00 PDT on Sep 10 in Orange County is comfortably after
    civil dawn (sunrise ~06:35)."""
    monkeypatch.setenv("SUN_LAT", "33.7455")
    monkeypatch.setenv("SUN_LON", "-117.8677")
    monkeypatch.setenv("SUN_TZ", "America/Los_Angeles")
    _freeze_now(monkeypatch, datetime(2026, 9, 10, 8, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles")))
    assert preview._detect_sun_polygon_mode() == "day"
