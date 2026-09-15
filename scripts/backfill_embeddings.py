"""One-shot backfill: publish `embed_queue` notifies for every rodent
alert that doesn't yet have a CLIP embedding.

Motivation: the on-insert publisher in StateDB.append_alert only fires
from Phase 2a onward. This script re-drives history (~29k rodent alerts
at time of writing — 17k live + 12k disk-backfilled) so the Phase 2b
clusterer has the whole population, not just the last few days.

## Delivery

Publishes on the same `embed_queue` channel the detectors use; the
embedder container's LISTEN loop consumes them and micro-batches CLIP.
No model inference here — we just enqueue and let the bounded worker
serialize. Same split as scripts/backfill_tp_clips.py ↔ archiver.

## Idempotency

Two layers. (1) The candidate query LEFT JOINs alert_embeddings and only
selects rows with no embedding for the target model_version, so a
re-run publishes only the gaps. (2) The embedder itself re-checks
before any image I/O, so an overlap between this script and a live
notify is a cheap skip, never a double write.

## Rate limiting + backpressure

`--sleep-ms` (default 20 → 50 notifies/s) keeps the embedder's queue
from ballooning on the initial burst. On top of that, every
`--check-every` notifies we measure LAG = published-but-not-yet-embedded
and pause while it exceeds `--max-lag`. That's a **closed-loop
backpressure** knob rather than a guessed sleep: if CLIP runs faster
than expected the script speeds up to match; if the embedder is down
the script parks instead of filling a queue that will be dropped.

## Ordering

Newest → oldest. Recent alerts are the ones the operator is looking at
in the UI today, and the ones Phase 2b will want first.

Usage (from repo root, with the stack up):

    docker compose exec web python scripts/backfill_embeddings.py --dry-run
    docker compose exec web python scripts/backfill_embeddings.py --limit 5
    docker compose exec web python scripts/backfill_embeddings.py
    # or: DATABASE_URL=... python scripts/backfill_embeddings.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg

CHANNEL = "embed_queue"
DEFAULT_MODEL_VERSION = "openai/clip-vit-base-patch32"


def _candidates(conn: psycopg.Connection, model_version: str, limit: int | None,
                include_historical: bool) -> list[tuple[int, str, float]]:
    where = ["a.is_rodent", "a.snapshot IS NOT NULL", "e.alert_id IS NULL"]
    if not include_historical:
        where.append("NOT a.historical")
    sql = f"""
        SELECT a.id, a.camera_id, a.ts
        FROM alerts a
        LEFT JOIN alert_embeddings e
               ON e.alert_id = a.id AND e.model_version = %s
        WHERE {' AND '.join(where)}
        ORDER BY a.ts DESC
    """  # noqa: S608 -- where-clauses are compile-time constants
    params: list = [model_version]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [(int(r[0]), r[1], float(r[2])) for r in cur.fetchall()]


def _embedded_count(conn: psycopg.Connection, ids: list[int], model_version: str) -> int:
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM alert_embeddings WHERE alert_id = ANY(%s) AND model_version = %s",
            (ids, model_version),
        )
        return int(cur.fetchone()[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres conninfo (default: $DATABASE_URL)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print what would be enqueued; publish nothing")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N candidates (newest first)")
    ap.add_argument(
        "--model-version",
        default=os.environ.get("CLIP_MODEL_ID") or DEFAULT_MODEL_VERSION,
        help="alert_embeddings.model_version to check for gaps (default: %(default)s)",
    )
    ap.add_argument(
        "--include-historical",
        action="store_true",
        help="Also enqueue historical=TRUE rows (disk-backfilled, no description). "
             "Default: live rows only — these have the richest metadata for 2b.",
    )
    ap.add_argument("--sleep-ms", type=int, default=20, help="ms between notifies (default: 20)")
    ap.add_argument(
        "--max-lag", type=int, default=2000,
        help="Pause publishing while (published - embedded) exceeds this (default: 2000)",
    )
    ap.add_argument(
        "--check-every", type=int, default=250,
        help="Re-measure lag every N notifies (default: 250)",
    )
    args = ap.parse_args()

    if not args.database_url:
        print("ERROR: --database-url or $DATABASE_URL required", file=sys.stderr)
        return 2

    with psycopg.connect(args.database_url, autocommit=True) as conn:
        # Fail-fast at the trust boundary: no alert_embeddings table means
        # the embedder has never started (or pgvector is missing). Every
        # notify we'd publish would be dropped on the floor.
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('alert_embeddings')")
            if cur.fetchone()[0] is None:
                print(
                    "ERROR: alert_embeddings table does not exist. Start the embedder "
                    "(docker compose up -d embedder) so it can apply its schema, then retry.",
                    file=sys.stderr,
                )
                return 3

        rows = _candidates(conn, args.model_version, args.limit, args.include_historical)
        total = len(rows)
        print(f"Candidates without embedding for {args.model_version!r}: {total}"
              f"{' (limit applied)' if args.limit else ''}")
        if args.dry_run:
            for alert_id, camera_id, ts in rows[:20]:
                print(f"  [would enqueue] alert={alert_id} camera={camera_id} ts={ts:.0f}")
            if total > 20:
                print(f"  ... and {total - 20} more")
            return 0

        t0 = time.monotonic()
        published: list[int] = []
        paused_s = 0.0
        for alert_id, camera_id, ts in rows:
            with conn.cursor() as cur:
                # pg_notify() function form accepts bound parameters; the
                # NOTIFY statement doesn't. Same as StateDB.notify().
                cur.execute("SELECT pg_notify(%s, %s)", (CHANNEL, str(alert_id)))
            published.append(alert_id)
            n = len(published)
            if n % args.check_every == 0 or n == total:
                done = _embedded_count(conn, published, args.model_version)
                lag = n - done
                elapsed = time.monotonic() - t0
                rate = done / elapsed if elapsed > 0 else 0.0
                eta = (total - done) / rate if rate > 0 else float("inf")
                print(
                    f"  published {n}/{total}  embedded {done}  lag {lag}  "
                    f"{rate:.1f} alerts/s  eta {eta/60:.1f} min",
                    flush=True,
                )
                # Closed-loop backpressure: park until the worker catches up.
                while lag > args.max_lag:
                    time.sleep(2.0)
                    paused_s += 2.0
                    done = _embedded_count(conn, published, args.model_version)
                    lag = n - done
            time.sleep(args.sleep_ms / 1000.0)

        # Final drain wait (bounded) so the summary reflects reality.
        deadline = time.monotonic() + 600
        done = _embedded_count(conn, published, args.model_version)
        while done < len(published) and time.monotonic() < deadline:
            time.sleep(5.0)
            done = _embedded_count(conn, published, args.model_version)
        elapsed = time.monotonic() - t0

    print()
    print(f"Done. published={len(published)} embedded={done} "
          f"unembedded={len(published) - done} wall={elapsed/60:.1f} min "
          f"(paused {paused_s:.0f}s for backpressure)")
    if done < len(published):
        print("  Some alerts didn't land (snapshot missing on disk, or dropped notify). "
              "Re-run this script — it only republishes the gaps.")
    print("Watch the embedder: docker compose logs -f embedder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
