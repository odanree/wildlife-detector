"""StateDB.append_alert — embed_queue publisher hook + bbox JSONB.

No Postgres: a fake pool records every SQL statement the method issues
so we can assert the NOTIFY gating (rodent AND live AND has snapshot)
and the bbox serialization without a DB. The real DDL / vector path is
covered by the embedder's own startup against the compose stack.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from psycopg.types.json import Jsonb

from src.storage import state_db
from src.storage.state_db import EMBED_QUEUE_CHANNEL, StateDB


class _FakeCursor:
    def __init__(self, log: list, returned_id: int | None):
        self._log = log
        self._returned_id = returned_id

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (self._returned_id,) if self._returned_id is not None else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, log, returned_id):
        self._log, self._id = log, returned_id

    def cursor(self, **_):
        return _FakeCursor(self._log, self._id)


class _FakePool:
    def __init__(self, returned_id: int | None = 4242):
        self.log: list = []
        self.returned_id = returned_id

    @contextmanager
    def connection(self):
        yield _FakeConn(self.log, self.returned_id)


def _db(pool: _FakePool) -> StateDB:
    # Bypass __init__ (opens a real pool + runs DDL); inject the fake.
    db = StateDB.__new__(StateDB)
    db._pool = pool
    return db


def _notifies(pool: _FakePool) -> list:
    return [(s, p) for s, p in pool.log if "pg_notify" in s]


def test_live_rodent_with_snapshot_publishes_embed_queue():
    pool = _FakePool(returned_id=4242)
    rid = _db(pool).append_alert(
        species="rat", is_rodent=True, historical=False,
        snapshot="2026-09-15/rodent_x.jpg",
    )
    assert rid == 4242
    n = _notifies(pool)
    assert len(n) == 1
    assert n[0][1] == (EMBED_QUEUE_CHANNEL, "4242")
    assert EMBED_QUEUE_CHANNEL == "embed_queue"


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(species="rat", is_rodent=True, historical=True, snapshot="d/x.jpg"),   # disk backfill
        dict(species="other", is_rodent=False, historical=False, snapshot="d/x.jpg"),  # not rodent
        dict(species="rat", is_rodent=True, historical=False, snapshot=None),          # nothing to embed
    ],
)
def test_no_publish_when_gating_fails(kwargs):
    pool = _FakePool(returned_id=1)
    _db(pool).append_alert(**kwargs)
    assert _notifies(pool) == []


def test_no_publish_when_insert_deduped():
    # ON CONFLICT DO NOTHING → RETURNING yields no row → None id → no notify.
    pool = _FakePool(returned_id=None)
    rid = _db(pool).append_alert(species="rat", is_rodent=True, snapshot="d/x.jpg")
    assert rid is None
    assert _notifies(pool) == []


def test_bbox_serialized_as_jsonb_with_frame_size():
    pool = _FakePool(returned_id=7)
    _db(pool).append_alert(
        species="mouse", is_rodent=True, snapshot="d/x.jpg",
        bbox=(10, 20, 30, 40), frame_size=(2048, 928),
    )
    insert_sql, params = next((s, p) for s, p in pool.log if s.startswith("INSERT INTO alerts"))
    assert "bbox" in insert_sql
    bbox_param = params[-1]
    assert isinstance(bbox_param, Jsonb)
    assert bbox_param.obj == {"x1": 10, "y1": 20, "x2": 30, "y2": 40, "frame_w": 2048, "frame_h": 928}


def test_bbox_none_binds_null():
    pool = _FakePool(returned_id=7)
    _db(pool).append_alert(species="mouse", is_rodent=True, snapshot="d/x.jpg")
    _, params = next((s, p) for s, p in pool.log if s.startswith("INSERT INTO alerts"))
    assert params[-1] is None


def test_notify_failure_does_not_fail_insert(monkeypatch):
    class _BoomCursor(_FakeCursor):
        def execute(self, sql, params=None):
            if "pg_notify" in sql:
                raise RuntimeError("listener plumbing broke")
            super().execute(sql, params)

    class _BoomConn(_FakeConn):
        def cursor(self, **_):
            return _BoomCursor(self._log, self._id)

    class _BoomPool(_FakePool):
        @contextmanager
        def connection(self):
            yield _BoomConn(self.log, self.returned_id)

    pool = _BoomPool(returned_id=99)
    rid = _db(pool).append_alert(species="rat", is_rodent=True, snapshot="d/x.jpg")
    assert rid == 99  # row is the source of truth; embedding is derived state


def test_migrate_adds_bbox_column():
    pool = _FakePool()
    _db(pool)._migrate()
    assert any("ADD COLUMN IF NOT EXISTS bbox JSONB" in s for s, _ in pool.log)
    # Pgvector DDL must NOT live here — detectors run against vanilla PG.
    assert not any("vector" in s.lower() for s, _ in pool.log)
    assert state_db._SCHEMA_VERSION == 1
