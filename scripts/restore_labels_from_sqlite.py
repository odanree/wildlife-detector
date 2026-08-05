"""Recover labels from the pre-migration SQLite `data/state.db` after
a Postgres volume wipe.

Motivation: after a `docker system prune --volumes` (or similar
aggressive cleanup) destroyed the wildlife-detector_pg-data volume,
the fresh Postgres backfilled 12k+ alerts from `snapshots/*.jpg` with
NEW BIGSERIAL ids — the same snapshot filenames map to different
alert_ids now. The one-shot migration script preserved ids in the
original migration, but that anchor is broken by the volume-recreate,
so we key on the natural key `(ts, snapshot)` instead.

Pattern: **re-key migration by natural key** — same shape as any
schema-refactor migration that renames or drops the surrogate PK.
Snapshots on disk are the load-bearing anchor; alert_id is a
generated convenience that we let the fresh sequence pick freely.

## What this does

1. Read every labeled row from SQLite (`label_ts IS NOT NULL`).
2. For each, find the Postgres row with matching (ts, snapshot).
   The unique index `uniq_alerts_ts_species_snap` guarantees at most
   one match per (ts, species, snapshot) triple; snapshot alone is
   often unique too since it embeds a millisecond timestamp.
3. UPDATE that row's label_verdict / label_species / label_notes /
   label_ts. Overwrites any existing label (SQLite is authoritative;
   the current Postgres labels came from an unknown source of dubious
   provenance).

## Idempotency

Re-running the script is safe — UPDATE is idempotent. Rows already at
the SQLite label state get overwritten with the same values (no-op).

## Failure mode

Rows whose snapshot was cleaned up (or whose timestamp is off by even
a millisecond due to timezone drift) won't match and get counted as
`unmatched`. Those are the only truly-lost labels — everything else
recovers.

Usage from the wildlife-detector repo root, with the stack up:

    docker compose exec web python scripts/restore_labels_from_sqlite.py --dry-run
    # then, if the diff looks right:
    docker compose exec web python scripts/restore_labels_from_sqlite.py

Options:
    --sqlite <path>       Old SQLite state.db (default data/state.db)
    --database-url <uri>  Postgres conninfo (default $DATABASE_URL)
    --dry-run             Report matched/unmatched, don't UPDATE
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import psycopg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", default="data/state.db")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--dry-run", action="store_true")
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
    total_labeled = src.execute(
        "SELECT COUNT(*) FROM alerts WHERE label_ts IS NOT NULL"
    ).fetchone()[0]
    print(f"SQLite: {total_labeled} labeled rows to consider")

    dst = psycopg.connect(args.database_url)

    matched = 0
    unmatched = 0
    updated = 0
    ambiguous = 0

    with dst.cursor() as cur:
        # Also pull camera_id — the disk-backfill on fresh Postgres
        # tags every row 'yard' by default (snapshot filenames don't
        # embed camera info), so we need SQLite's authoritative value
        # to fix rows that actually belong to rooftop. Without this,
        # rooftop labels get filed under yard and the rooftop UI shows
        # empty even though the labels exist in the DB.
        rows = src.execute(
            "SELECT ts, snapshot, species, camera_id, label_verdict, "
            "label_species, label_notes, label_ts FROM alerts "
            "WHERE label_ts IS NOT NULL"
        ).fetchall()

    for r in rows:
        # Match on snapshot alone. The disk-backfill re-ingest assigns
        # a NEW ts to each snapshot (derived from mtime or the filename
        # embedded time), so the old SQLite ts no longer aligns —
        # empirically only 20% of rows match on (ts, snapshot) while
        # 91% match on snapshot alone. Snapshot filenames embed
        # millisecond timestamps + track_id so uniqueness is safe;
        # UNIQUE INDEX uniq_alerts_ts_species_snap enforces at the
        # DB level.
        with dst.cursor() as cur:
            cur.execute(
                "SELECT id FROM alerts WHERE snapshot = %s ORDER BY id LIMIT 2",
                (r["snapshot"],),
            )
            hits = cur.fetchall()

        if not hits:
            unmatched += 1
            continue
        if len(hits) > 1:
            ambiguous += 1

        matched += 1
        alert_id = hits[0][0]

        if args.dry_run:
            continue

        with dst.cursor() as cur:
            cur.execute(
                "UPDATE alerts SET camera_id=%s, label_verdict=%s, "
                "label_species=%s, label_notes=%s, label_ts=%s WHERE id=%s",
                (
                    r["camera_id"],
                    r["label_verdict"],
                    r["label_species"],
                    r["label_notes"],
                    r["label_ts"],
                    alert_id,
                ),
            )
            if cur.rowcount > 0:
                updated += 1

    dst.commit()
    dst.close()

    print()
    print(f"  matched:    {matched}")
    print(f"  unmatched:  {unmatched}  (snapshots missing or ts drift)")
    print(f"  ambiguous:  {ambiguous}  (>1 row shares ts+snapshot — took lowest id)")
    if args.dry_run:
        print(f"  [dry-run] would UPDATE: {matched}")
    else:
        print(f"  UPDATE'd:  {updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
