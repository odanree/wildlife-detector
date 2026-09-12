"""Unit tests for the priority-aware alert debounce (closes #203)."""
from __future__ import annotations

from src.alerts.debounce import (
    ALERT_PRIORITY_OTHER,
    ALERT_PRIORITY_RODENT,
    should_fire,
)


class TestDisabledOrManualBypass:
    """Rules 1 and 2 short-circuit ahead of any timing math."""

    def test_window_zero_always_fires(self):
        assert should_fire(
            now=100.0, last_ts=99.0, last_priority=ALERT_PRIORITY_RODENT,
            this_priority=ALERT_PRIORITY_OTHER, window_seconds=0.0,
            is_manual=False,
        )

    def test_window_negative_treated_as_disabled(self):
        assert should_fire(
            now=100.0, last_ts=99.0, last_priority=ALERT_PRIORITY_RODENT,
            this_priority=ALERT_PRIORITY_OTHER, window_seconds=-1.0,
            is_manual=False,
        )

    def test_manual_bypasses_debounce(self):
        assert should_fire(
            now=100.0, last_ts=99.9, last_priority=ALERT_PRIORITY_RODENT,
            this_priority=ALERT_PRIORITY_RODENT, window_seconds=20.0,
            is_manual=True,
        )


class TestSamePrioritySuppression:
    """Rule 3: window has elapsed → fire; still inside → don't."""

    def test_inside_window_same_priority_suppresses(self):
        # Rodent fired at t=90, next rodent at t=95 within 20s window.
        assert not should_fire(
            now=95.0, last_ts=90.0, last_priority=ALERT_PRIORITY_RODENT,
            this_priority=ALERT_PRIORITY_RODENT, window_seconds=20.0,
            is_manual=False,
        )

    def test_outside_window_fires(self):
        # Rodent fired at t=90, next rodent at t=115 (25s later, > 20s).
        assert should_fire(
            now=115.0, last_ts=90.0, last_priority=ALERT_PRIORITY_RODENT,
            this_priority=ALERT_PRIORITY_RODENT, window_seconds=20.0,
            is_manual=False,
        )

    def test_boundary_fires(self):
        # Exactly at window boundary — >= counts as elapsed.
        assert should_fire(
            now=110.0, last_ts=90.0, last_priority=ALERT_PRIORITY_OTHER,
            this_priority=ALERT_PRIORITY_OTHER, window_seconds=20.0,
            is_manual=False,
        )


class TestPriorityUpgrade:
    """Rule 4: rodent upgrades past a standing `other`. `other` never
    upgrades past a standing rodent. This is the #203 fix."""

    def test_rodent_upgrades_other(self):
        # `other` fired at t=90 (from VLM-reject-override). Rodent-positive
        # arrives at t=95 well inside the 20s window — must still fire.
        assert should_fire(
            now=95.0, last_ts=90.0, last_priority=ALERT_PRIORITY_OTHER,
            this_priority=ALERT_PRIORITY_RODENT, window_seconds=20.0,
            is_manual=False,
        )

    def test_other_does_not_upgrade_rodent(self):
        # Rodent fired at t=90. `other` arriving at t=95 must be
        # suppressed — no downgrade path.
        assert not should_fire(
            now=95.0, last_ts=90.0, last_priority=ALERT_PRIORITY_RODENT,
            this_priority=ALERT_PRIORITY_OTHER, window_seconds=20.0,
            is_manual=False,
        )

    def test_same_priority_never_upgrades(self):
        assert not should_fire(
            now=95.0, last_ts=90.0, last_priority=ALERT_PRIORITY_OTHER,
            this_priority=ALERT_PRIORITY_OTHER, window_seconds=20.0,
            is_manual=False,
        )


class TestColdStart:
    """First-ever alert: last_ts=0.0, last_priority=0."""

    def test_first_alert_ever_fires(self):
        # A fresh process has last_ts=0.0 and last_priority=0. Even a low-
        # priority `other` alert must fire — the >0 priority is always > 0.
        assert should_fire(
            now=100.0, last_ts=0.0, last_priority=0,
            this_priority=ALERT_PRIORITY_OTHER, window_seconds=20.0,
            is_manual=False,
        )
