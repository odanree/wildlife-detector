"""One-shot import from the legacy SQLite state.db into Postgres.

Preserves alert IDs so downstream references (snapshot filenames,
external caches, per-alert URLs) don't break. After import, resets
the `alerts_id_seq` sequence to MAX(id)+1 so new inserts don't
collide.

Idempotent: `INSERT ... ON CONFLICT (id) DO NOTHING`, so re-running
after a partial import fills only the gaps.

Usage (from the wildlife-detector repo root):
    # Inside the web container (Postgres reachable at postgres:5432):
    docker compose exec web python scripts/migrate_sqlite_to_postgres.py \\
        --sqlite /app/data/state.db

    # Or from host with a running compose stack (needs psycopg locally):
    DATABASE_URL=postgresql://wildlife:PASS@localhost:5432/wildlife \\
        python scripts/migrate_sqlite_to_postgres.py --sqlite ./data/state.db

Pattern: **one-shot bulk import with idempotent-by-id insert**. Same
shape as any dump/restore migration; the ID-preservation piece is what
lets snapshot filenames + external caches keep working.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


BATCH_SIZE = 1000

# Columns to migrate — same order as the alerts table's non-generated cols.
# We explicitly include `id` so IDs are preserved (BIGSERIAL doesn't fight
# us because we're inserting an explicit value, not relying on the default).
COLS = [
    "id", "ts", "camera_id", "species", "confidence", "description",
    "snapshot", "track_id", "yolo_conf", "is_rodent", "historical",
    "created_at", "label_verdict", "label_species", "label_notes", "label_ts",
]

INSERT_SQL = f"""
    INSERT INTO alerts ({', '.join(COLS)})
    VALUES ({', '.join(f'%({c})s' for c in COLS)})
    ON CONFLICT DO NOTHING
"""
# Target-less ON CONFLICT so ANY unique violation is silently skipped —
# covers both the PRIMARY KEY (id) and the composite unique index
# (ts, species, coalesce(snapshot,'')). A legacy SQLite DB may contain
# rows that violate the composite index (added later than the table),
# and ON CONFLICT (id) alone would let those through and blow up.


def _to_bool(v):
    """SQLite stores booleans as INTEGER 0/1; Postgres wants Python bool."""
    if v is None:
        return None
    return bool(v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sqlite",
        default="data/state.db",
        help="Path to legacy SQLite state.db (default: data/state.db)",
    )
    ap.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres conninfo (default: $DATABASE_URL)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report row counts, don't insert.",
    )
    args = ap.parse_args()

    if not args.database_url:
        print("ERROR: --database-url or $DATABASE_URL required", file=sys.stderr)
        return 2

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f"ERROR: SQLite file not found: {sqlite_path}", file=sys.stderr)
        return 2

    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    total_src = src.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    print(f"Source SQLite: {total_src} rows in alerts")

    dst = psycopg.connect(args.database_url, row_factory=dict_row)
    with dst.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM alerts")
        already = cur.fetchone()["count"]
    print(f"Destination Postgres: {already} rows in alerts (before import)")

    if args.dry_run:
        print("--dry-run: not inserting.")
        return 0

    inserted = 0
    offset = 0
    while True:
        rows = src.execute(
            "SELECT * FROM alerts ORDER BY id LIMIT ? OFFSET ?",
            (BATCH_SIZE, offset),
        ).fetchall()
        if not rows:
            break
        batch = [
            {
                "id": r["id"],
                "ts": r["ts"],
                "camera_id": r["camera_id"] if "camera_id" in r.keys() else "yard",
                "species": r["species"],
                "confidence": r["confidence"],
                "description": r["description"],
                "snapshot": r["snapshot"],
                "track_id": r["track_id"],
                "yolo_conf": r["yolo_conf"],
                "is_rodent": _to_bool(r["is_rodent"]),
                "historical": _to_bool(r["historical"]),
                "created_at": r["created_at"],
                "label_verdict": r["label_verdict"] if "label_verdict" in r.keys() else None,
                "label_species": r["label_species"] if "label_species" in r.keys() else None,
                "label_notes":   r["label_notes"]   if "label_notes"   in r.keys() else None,
                "label_ts":      r["label_ts"]      if "label_ts"      in r.keys() else None,
            }
            for r in rows
        ]
        with dst.cursor() as cur:
            cur.executemany(INSERT_SQL, batch)
            # rowcount reports total processed by executemany, not
            # actually-inserted (ON CONFLICT DO NOTHING confuses it).
            # Trust rowcount as a proxy — good enough for progress.
            inserted += cur.rowcount if cur.rowcount > 0 else len(batch)
        dst.commit()
        offset += len(rows)
        print(f"  imported {offset}/{total_src}")

    # Reset the id sequence to MAX(id)+1 so new inserts don't collide
    # with the preserved IDs. Postgres BIGSERIAL creates `alerts_id_seq`
    # by default.
    with dst.cursor() as cur:
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('alerts', 'id'), "
            "GREATEST(COALESCE(MAX(id), 0), 1)) FROM alerts"
        )
        cur.execute("SELECT COUNT(*) AS n FROM alerts")
        final = cur.fetchone()["n"]
    dst.commit()
    print()
    print(f"Done. Postgres now has {final} rows (was {already}).")
    print(f"Sequence advanced to MAX(id)+1 so new inserts won't collide.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
