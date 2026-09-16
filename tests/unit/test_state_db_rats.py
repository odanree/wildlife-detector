"""StateDB rat catalog methods (Phase 2c) — get_rat / rename_rat / merge_rats.

No Postgres: a scripted fake pool feeds each `execute` the row set it
should return and records every statement, so we can assert the merge
transaction's GUARDS (retired source / target, self-merge, missing rows)
and its STATEMENT ORDER (alerts move before the source is retired, audit
row last) without a DB. The real SQL is exercised by the compose stack.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from src.storage.state_db import StateDB

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _rat(rid: int, retired: bool = False, notes: str | None = None) -> dict:
    return {
        "id": rid, "first_seen": NOW, "last_seen": NOW, "alert_count": 10,
        "primary_camera": "yard", "retired_at": NOW if retired else None, "notes": notes,
    }


class _ScriptedCursor:
    """Each execute() pops the next scripted result: a list of rows, or an
    int meaning "rowcount only" (UPDATE with no RETURNING)."""

    def __init__(self, log: list, script: list):
        self._log, self._script = log, script
        self._rows: list = []
        self.rowcount = -1

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))
        nxt = self._script.pop(0) if self._script else []
        if isinstance(nxt, int):
            self._rows, self.rowcount = [], nxt
        else:
            self._rows, self.rowcount = list(nxt), len(nxt)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    def __init__(self, script: list | None = None):
        self.log: list = []
        self.script = list(script or [])
        self.exits = 0

    @contextmanager
    def connection(self):
        conn = self

        class _Conn:
            def cursor(self, **_):
                return _ScriptedCursor(conn.log, conn.script)

        yield _Conn()
        self.exits += 1


def _db(pool: _FakePool) -> StateDB:
    db = StateDB.__new__(StateDB)  # bypass __init__ (real pool + DDL)
    db._pool = pool
    return db


def _sqls(pool: _FakePool) -> list[str]:
    return [s for s, _ in pool.log]


# ── merge_rats ──────────────────────────────────────────────────────────


def test_merge_happy_path_statement_order_and_result():
    target_after = {**_rat(2), "alert_count": 25}
    pool = _FakePool(script=[
        [_rat(1, notes="Scar"), _rat(2)],  # SELECT ... FOR UPDATE
        15,                                # UPDATE alerts (rowcount)
        1,                                 # UPDATE rats (retire source)
        [target_after],                    # UPDATE rats target ... RETURNING
        1,                                 # INSERT rat_cluster_runs
    ])
    res = _db(pool).merge_rats(1, 2)

    assert res == {"source_id": 1, "target_id": 2, "moved": 15, "target": target_after}
    sqls = _sqls(pool)
    assert len(sqls) == 5
    assert sqls[0].startswith("SELECT") and "FOR UPDATE" in sqls[0]
    assert sqls[1].startswith("UPDATE alerts SET rat_id")
    assert pool.log[1][1] == (2, 1)  # target ← source
    assert "retired_at = now()" in sqls[2] and pool.log[2][1] == (1,)
    assert "MODE() WITHIN GROUP" in sqls[3] and pool.log[3][1] == (2, 2)
    # Regression pin: the FROM-subquery aliases `primary_camera`, so an
    # unqualified RETURNING list is ambiguous in Postgres (500 on the
    # first live merge). Every returned column must be r.-qualified.
    returning = sqls[3].split("RETURNING", 1)[1]
    assert all(c.strip().startswith("r.") for c in returning.split(","))
    assert sqls[4].startswith("INSERT INTO rat_cluster_runs") and "'operator_merge'" in sqls[4]
    assert "15 alerts moved" in pool.log[4][1][1]
    assert "'Scar'" in pool.log[4][1][1]
    # Single connection context → single transaction boundary.
    assert pool.exits == 1


def test_merge_locks_both_rows_in_ascending_id_order():
    pool = _FakePool(script=[[_rat(2), _rat(9)], 3, 1, [_rat(2)], 1])
    _db(pool).merge_rats(9, 2)
    assert "ORDER BY id FOR UPDATE" in _sqls(pool)[0]
    assert pool.log[0][1] == ([9, 2],)  # ANY(array) — ORDER BY does the sorting


def test_merge_self_is_rejected_before_any_sql():
    pool = _FakePool()
    with pytest.raises(ValueError, match="itself"):
        _db(pool).merge_rats(4, 4)
    assert pool.log == []


def test_merge_retired_source_is_400_not_silent_noop():
    """Idempotency guard: re-posting a merge finds the source retired and
    must NOT re-run the alert move / re-stamp retired_at."""
    pool = _FakePool(script=[[_rat(1, retired=True), _rat(2)]])
    with pytest.raises(ValueError, match="already retired"):
        _db(pool).merge_rats(1, 2)
    assert len(pool.log) == 1  # only the SELECT ... FOR UPDATE ran


def test_merge_into_retired_target_is_rejected():
    pool = _FakePool(script=[[_rat(1), _rat(2, retired=True)]])
    with pytest.raises(ValueError, match="target rat 2 is retired"):
        _db(pool).merge_rats(1, 2)
    assert len(pool.log) == 1


@pytest.mark.parametrize("present, missing", [([2], 1), ([1], 2)])
def test_merge_missing_rat_is_lookup_error(present, missing):
    pool = _FakePool(script=[[_rat(r) for r in present]])
    with pytest.raises(LookupError, match=f"rat {missing} not found"):
        _db(pool).merge_rats(1, 2)
    assert len(pool.log) == 1


# ── rename_rat ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, stored", [
    ("Scar", "Scar"),
    ("  Notch  ", "Notch"),
    ("", None),
    ("   ", None),
    (None, None),
])
def test_rename_normalizes_blank_to_null(raw, stored):
    pool = _FakePool(script=[[_rat(7, notes=stored)]])
    row = _db(pool).rename_rat(7, raw)
    sql, params = pool.log[0]
    assert sql.startswith("UPDATE rats SET notes = %s")
    assert "RETURNING" in sql and "centroid" not in sql
    assert params == (stored, 7)
    assert row["notes"] == stored


def test_rename_missing_rat_returns_none():
    pool = _FakePool(script=[[]])
    assert _db(pool).rename_rat(404, "ghost") is None


# ── get_rat ─────────────────────────────────────────────────────────────


def test_get_rat_camera_frequency_is_percent_over_all_alerts():
    pool = _FakePool(script=[
        [_rat(5, notes="Stubby")],
        [{"camera_id": "yard", "n": 30}, {"camera_id": "rooftop", "n": 10}],
        [{"id": 1, "ts": 1.0, "camera_id": "yard", "species": "rat", "confidence": 0.9,
          "is_rodent": True, "historical": False, "description": "d", "snapshot": "a/b.jpg",
          "track_id": 3, "label_verdict": "correct", "label_species": "real_rodent"}],
    ])
    out = _db(pool).get_rat(5, alerts_limit=1)
    assert out["notes"] == "Stubby"
    assert out["camera_frequency"] == {"yard": 75.0, "rooftop": 25.0}
    assert out["alerts_total"] == 40          # from the GROUP BY, not the capped page
    assert len(out["alerts"]) == 1
    assert out["alerts"][0]["is_rodent"] is True
    # Alert page is newest-first and honours the cap.
    sql, params = pool.log[2]
    assert "ORDER BY ts DESC" in sql and params == (5, 1)


def test_get_rat_missing_returns_none_without_further_queries():
    pool = _FakePool(script=[[]])
    assert _db(pool).get_rat(999) is None
    assert len(pool.log) == 1
