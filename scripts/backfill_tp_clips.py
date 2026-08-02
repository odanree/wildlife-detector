"""One-shot backfill: republish archive_queue notifies for every
TP-with-species that doesn't yet have a local clip.

Motivation: the on-label archive path only fires from now onward.
This script re-drives history so previously-labeled TPs also get
archived — with an intentional cap of TPs that already have a species
tag, since bare-verdict TPs are still waiting for the operator's
species-backfill pass (which will re-trigger the archive naturally
via the label endpoint).

## Delivery

Publishes on the same `archive_queue` channel the web service uses;
the archiver container's LISTEN loop consumes them. No direct ffmpeg
invocation here — we just enqueue and let the bounded pool serialize.

## Idempotency

The archiver itself skips alerts whose clip file already exists, so
re-running this script is safe — it'll re-notify but the archiver
short-circuits before spawning ffmpeg.

## Rate limiting

Sleep briefly between notifies. Not because Postgres cares, but so the
archiver's bounded-pool queue never gets deep enough to matter — with
one notify every 200ms and the pool at 2 workers pulling ~10s clips
each, backlog steady-state stays around 5 items.

Usage (from repo root, with the stack up):

    docker compose exec web python scripts/backfill_tp_clips.py
    # or: DATABASE_URL=... python scripts/backfill_tp_clips.py

Add --dry-run to just print what would be enqueued.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg


def _clip_exists(clips_dir: Path, alert_id: int, alert_ts: float) -> bool:
    """Mirror of ClipArchiver.clip_path — kept in sync manually."""
    day = datetime.fromtimestamp(alert_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    p = clips_dir / day / f"{alert_id}.mp4"
    return p.exists() and p.stat().st_size > 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--clips-dir",
        default=os.environ.get("CLIPS_DIR", "/app/clips"),
        help="Local clip storage root (default: /app/clips)",
    )
    ap.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres conninfo (default: $DATABASE_URL)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--sleep-ms",
        type=int,
        default=200,
        help="ms between notifies (default: 200)",
    )
    args = ap.parse_args()

    if not args.database_url:
        print("ERROR: --database-url or $DATABASE_URL required", file=sys.stderr)
        return 2

    clips_dir = Path(args.clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(args.database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # TPs with species that we ostensibly want archived, newest
            # first. Older rows are less likely to still be in the NVR
            # retention window, so priority-order newest → oldest makes
            # each notify more likely to land on real footage.
            cur.execute(
                """
                SELECT id, ts, camera_id
                FROM alerts
                WHERE label_verdict = 'correct'
                  AND label_species IS NOT NULL
                ORDER BY ts DESC
                """
            )
            rows = cur.fetchall()

        total = len(rows)
        already = 0
        enqueued = 0
        for alert_id, alert_ts, camera_id in rows:
            if _clip_exists(clips_dir, alert_id, float(alert_ts)):
                already += 1
                continue
            if args.dry_run:
                enqueued += 1
                print(f"  [would enqueue] alert={alert_id} camera={camera_id} ts={alert_ts}")
                continue
            with conn.cursor() as cur:
                cur.execute("NOTIFY archive_queue, %s", (str(alert_id),))
            enqueued += 1
            if enqueued % 25 == 0:
                print(f"  enqueued {enqueued}/{total - already} (already-archived: {already})")
            time.sleep(args.sleep_ms / 1000.0)

    print()
    print(f"Done. TPs-with-species: {total}")
    print(f"  already-archived (skipped): {already}")
    print(f"  {'would enqueue' if args.dry_run else 'enqueued'}: {enqueued}")
    print()
    print("The archiver container will drain these in the background. Watch:")
    print("  docker compose logs -f archiver")
    return 0


if __name__ == "__main__":
    sys.exit(main())
