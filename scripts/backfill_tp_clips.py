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


def _failure_path(clips_dir: Path, alert_id: int, alert_ts: float) -> Path:
    """Mirror of ClipArchiver.failure_path — kept in sync manually."""
    day = datetime.fromtimestamp(alert_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return clips_dir / day / f"{alert_id}.failed"


def _is_permanent_failure(clips_dir: Path, alert_id: int, alert_ts: float) -> bool:
    """A `.failed` tombstone means a prior pull attempt determined the
    alert's source footage was unrecoverable (AgentDVR outage gap, NVR
    retention rotation, etc.). Re-enqueuing burns cycles for no gain.
    """
    return _failure_path(clips_dir, alert_id, alert_ts).exists()


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
    ap.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-enqueue alerts marked permanent failure (`.failed` tombstones). "
             "Default: skip them. Use when investigating whether a prior "
             "permanent-failure verdict was wrong (e.g. the source recovered).",
    )
    args = ap.parse_args()

    if not args.database_url:
        print("ERROR: --database-url or $DATABASE_URL required", file=sys.stderr)
        return 2

    clips_dir = Path(args.clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)

    # --retry-failed requires write access to unlink `.failed` tombstones
    # under clips_dir. Fail fast at the trust boundary — otherwise each
    # tombstone unlink fires a per-row WARN and the notify still enqueues,
    # so the operator watches a "success" log while the archiver silently
    # re-skips every alert because the tombstone was never cleared.
    # Probe with a real tempfile rather than os.access: bind-mounted R/O
    # filesystems and ACL edge cases can lie in either direction.
    if args.retry_failed and not args.dry_run:
        probe = clips_dir / f".backfill_write_probe_{os.getpid()}"
        try:
            probe.touch()
            probe.unlink()
        except OSError as e:
            print(
                f"ERROR: --retry-failed needs write access to {clips_dir} "
                f"to clear `.failed` tombstones, but probe write failed: {e}. "
                f"Aborting to avoid a silent no-op run.",
                file=sys.stderr,
            )
            return 3

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
        permanent_failures = 0
        cleared_failures = 0
        enqueued = 0
        for alert_id, alert_ts, camera_id in rows:
            if _clip_exists(clips_dir, alert_id, float(alert_ts)):
                already += 1
                continue
            if _is_permanent_failure(clips_dir, alert_id, float(alert_ts)):
                if not args.retry_failed:
                    permanent_failures += 1
                    continue
                # --retry-failed: the archiver's submit() gates on the
                # tombstone before it reaches the worker pool, so a
                # bare NOTIFY here would be a no-op. Clear the
                # tombstone first so the archiver actually re-attempts.
                # Idempotent — a missing file is fine at this stage.
                if not args.dry_run:
                    try:
                        _failure_path(clips_dir, alert_id, float(alert_ts)).unlink()
                        cleared_failures += 1
                    except OSError as e:
                        print(
                            f"  WARN: failed to clear tombstone for "
                            f"alert={alert_id}: {e} (skipping)",
                            file=sys.stderr,
                        )
                        permanent_failures += 1
                        continue
            if args.dry_run:
                enqueued += 1
                print(f"  [would enqueue] alert={alert_id} camera={camera_id} ts={alert_ts}")
                continue
            with conn.cursor() as cur:
                # pg_notify() function form accepts bound parameters;
                # the SQL NOTIFY statement doesn't. Same fix as
                # StateDB.notify() — see that method's docstring.
                cur.execute("SELECT pg_notify(%s, %s)", ("archive_queue", str(alert_id)))
            enqueued += 1
            if enqueued % 25 == 0:
                print(f"  enqueued {enqueued}/{total - already} (already-archived: {already})")
            time.sleep(args.sleep_ms / 1000.0)

    print()
    print(f"Done. TPs-with-species: {total}")
    print(f"  already-archived (skipped): {already}")
    if permanent_failures:
        retry_hint = " (use --retry-failed to re-enqueue)" if not args.retry_failed else ""
        print(f"  permanent-failure tombstones (skipped): {permanent_failures}{retry_hint}")
    if cleared_failures:
        print(f"  tombstones cleared for retry: {cleared_failures}")
    print(f"  {'would enqueue' if args.dry_run else 'enqueued'}: {enqueued}")
    print()
    print("The archiver container will drain these in the background. Watch:")
    print("  docker compose logs -f archiver")
    return 0


if __name__ == "__main__":
    sys.exit(main())
