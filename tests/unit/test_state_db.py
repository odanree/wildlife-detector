"""Unit tests for the SQLite-backed state store (Phase 1 of ADR 002).

Uses temp files instead of :memory: so we can prove the persistence behavior
(reopen the DB, alerts still there). The temp files land under
tempfile.mkdtemp() and are cleaned up per test.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.storage.state_db import StateDB


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as td:
        yield str(Path(td) / "state.db")


class TestAppendAndList:
    def test_append_then_list(self, db_path):
        db = StateDB(db_path)
        db.append_alert(species="rat", confidence=0.9, description="tail visible",
                        snapshot="2026-07-17/rodent_x.jpg", track_id=42,
                        yolo_conf=0.5, is_rodent=True)
        rows = db.list_alerts()
        assert len(rows) == 1
        assert rows[0]["species"] == "rat"
        assert rows[0]["confidence"] == pytest.approx(0.9)
        assert rows[0]["snapshot"] == "2026-07-17/rodent_x.jpg"
        assert rows[0]["track_id"] == 42
        assert rows[0]["yolo_conf"] == pytest.approx(0.5)
        assert rows[0]["is_rodent"] is True
        assert rows[0]["historical"] is False
        db.close()

    def test_list_newest_first(self, db_path):
        db = StateDB(db_path)
        # Explicit ts so ordering isn't racy with time.time()
        db.append_alert(species="rat", ts=100.0, snapshot="a.jpg")
        db.append_alert(species="mouse", ts=200.0, snapshot="b.jpg")
        db.append_alert(species="cat", ts=150.0, snapshot="c.jpg")
        rows = db.list_alerts()
        assert [r["species"] for r in rows] == ["mouse", "cat", "rat"]
        db.close()

    def test_species_filter(self, db_path):
        db = StateDB(db_path)
        db.append_alert(species="rat", ts=1.0)
        db.append_alert(species="mouse", ts=2.0)
        db.append_alert(species="rat", ts=3.0)
        assert len(db.list_alerts(species="rat")) == 2
        assert len(db.list_alerts(species="mouse")) == 1
        assert len(db.list_alerts(species="dog")) == 0
        db.close()

    def test_limit(self, db_path):
        db = StateDB(db_path)
        for i in range(10):
            db.append_alert(species="rat", ts=float(i))
        assert len(db.list_alerts(limit=3)) == 3
        assert len(db.list_alerts(limit=100)) == 10
        db.close()

    def test_since_ts(self, db_path):
        db = StateDB(db_path)
        db.append_alert(species="rat", ts=100.0)
        db.append_alert(species="mouse", ts=200.0)
        db.append_alert(species="cat", ts=300.0)
        rows = db.list_alerts(since_ts=150.0)
        assert [r["species"] for r in rows] == ["cat", "mouse"]
        db.close()


class TestIdempotentBackfill:
    def test_unique_constraint_dedupes(self, db_path):
        """Same (ts, species, snapshot) triple → second insert silently ignored."""
        db = StateDB(db_path)
        result1 = db.append_alert(species="rat", ts=100.0, snapshot="a.jpg")
        result2 = db.append_alert(species="rat", ts=100.0, snapshot="a.jpg")
        assert result1 is not None
        assert result2 is None   # unique constraint fired
        assert db.total_alerts() == 1
        db.close()

    def test_bulk_backfill_dedup(self, db_path):
        """Running the same backfill twice inserts the same rows only once."""
        db = StateDB(db_path)
        rows = [
            {"ts": 100.0, "species": "rodent", "snapshot": "a.jpg",
             "confidence": None, "description": "", "track_id": None,
             "yolo_conf": None, "is_rodent": 1, "historical": 1},
            {"ts": 200.0, "species": "rodent", "snapshot": "b.jpg",
             "confidence": None, "description": "", "track_id": None,
             "yolo_conf": None, "is_rodent": 1, "historical": 1},
        ]
        assert db.append_alerts_bulk(rows) == 2
        assert db.append_alerts_bulk(rows) == 0   # all duplicates now
        assert db.total_alerts() == 2
        db.close()

    def test_bulk_backfill_partial_overlap(self, db_path):
        """New JPEG added between backfills → only new row inserted second time."""
        db = StateDB(db_path)
        db.append_alerts_bulk([
            {"ts": 100.0, "species": "rodent", "snapshot": "a.jpg",
             "confidence": None, "description": "", "track_id": None,
             "yolo_conf": None, "is_rodent": 1, "historical": 1},
        ])
        assert db.total_alerts() == 1
        # Second scan sees the same 'a' plus a new 'b'
        n = db.append_alerts_bulk([
            {"ts": 100.0, "species": "rodent", "snapshot": "a.jpg",
             "confidence": None, "description": "", "track_id": None,
             "yolo_conf": None, "is_rodent": 1, "historical": 1},
            {"ts": 200.0, "species": "rodent", "snapshot": "b.jpg",
             "confidence": None, "description": "", "track_id": None,
             "yolo_conf": None, "is_rodent": 1, "historical": 1},
        ])
        assert n == 1
        assert db.total_alerts() == 2
        db.close()

    def test_null_snapshot_dedup(self, db_path):
        """Two alerts with NULL snapshot but different species should coexist —
        the unique index uses COALESCE(snapshot, '') so nulls compare equal."""
        db = StateDB(db_path)
        db.append_alert(species="rat", ts=100.0, snapshot=None)
        db.append_alert(species="mouse", ts=100.0, snapshot=None)
        assert db.total_alerts() == 2
        # But same species + ts + null snapshot IS a duplicate
        result = db.append_alert(species="rat", ts=100.0, snapshot=None)
        assert result is None
        assert db.total_alerts() == 2
        db.close()


class TestPersistence:
    def test_survives_reopen(self, db_path):
        db = StateDB(db_path)
        db.append_alert(species="rat", ts=100.0, snapshot="a.jpg")
        db.append_alert(species="mouse", ts=200.0, snapshot="b.jpg")
        db.close()

        # Simulate a detector restart
        db2 = StateDB(db_path)
        rows = db2.list_alerts()
        assert len(rows) == 2
        assert rows[0]["species"] == "mouse"   # newest first
        assert rows[1]["species"] == "rat"
        db2.close()


class TestQueries:
    def test_total_and_latest(self, db_path):
        db = StateDB(db_path)
        assert db.total_alerts() == 0
        assert db.latest_alert() is None
        db.append_alert(species="rat", ts=100.0)
        db.append_alert(species="mouse", ts=200.0)
        assert db.total_alerts() == 2
        latest = db.latest_alert()
        assert latest is not None
        assert latest["species"] == "mouse"
        db.close()

    def test_snapshots_present(self, db_path):
        db = StateDB(db_path)
        db.append_alert(species="rat", ts=1.0, snapshot="a.jpg")
        db.append_alert(species="rat", ts=2.0, snapshot="b.jpg")
        db.append_alert(species="rat", ts=3.0, snapshot=None)  # NULL snapshot
        present = db.snapshots_present()
        assert present == {"a.jpg", "b.jpg"}
        db.close()
