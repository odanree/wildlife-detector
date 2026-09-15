"""Backfill `alerts.rat_id` + `rats` over the whole embedded history by
replaying the clusterer over rolling, overlapping windows.

Motivation: the nightly clusterer only ever sees the trailing 30 days.
Phase 2a embedded ~17k historical live alerts (9.3k of them TP-labelled);
this script walks that history oldest → newest so the `rats` catalog is
seeded with every identity we have evidence for before the nightly job
takes over.

## Windows

    |---- W0 ----|
             |---- W1 ----|
                      |---- W2 ----|
    step = window_days - overlap_days

Oldest → newest (the opposite of backfill_embeddings.py) because the
linker is causal: window N+1 matches its clusters against the rats
window N spawned. Overlap is what lets a rat straddling a boundary keep
one id — its overlap-region alerts already carry window N's rat_id, so
window N+1's cluster containing them has a centroid that lands on the
same rat at cosine ≈ 1. Without overlap a rat seen day 29 → 31 would
get two ids.

## Idempotency

`rat_cluster_runs` is the ledger. A window is skipped when a run row
exists with the same (window_start, window_end, algo). `--force`
re-runs anyway (assignments are stable, so this is safe — it just
appends another audit row). `--reset` wipes rat_id / rats / runs first
for a from-scratch rebuild after a param change.

## Spillover report

Every spawn decision is graded against the HNSW neighbourhood (see
RatClusterer._spillover_for_spawn). The summary at the end ranks
windows by flagged spawns — that is the load-bearing test of the
centroid-linking design: a flagged spawn is a rat the linker called
"new" while the per-alert neighbourhood says "already catalogued".

Usage (inside the clusterer image so hdbscan is present):

    docker compose run --rm clusterer python scripts/backfill_rat_ids.py --dry-run
    docker compose run --rm clusterer python scripts/backfill_rat_ids.py
    docker compose run --rm clusterer python scripts/backfill_rat_ids.py --window-days 14 --overlap-days 3 --reset
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

# Running as `python scripts/backfill_rat_ids.py` puts scripts/ (not the
# repo root) on sys.path. Same bootstrap as scripts/event_triggered_replay.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.clusterer.clusterer import ALGO, ClusterParams, RatClusterer, RunResult, cosine_to_euclid  # noqa: E402
from src.clusterer.schema import ensure_schema  # noqa: E402

logger = logging.getLogger("backfill_rat_ids")


def _candidate_span(conn: psycopg.Connection, params: ClusterParams) -> tuple[datetime, datetime] | None:
    where = ["a.is_rodent", "NOT a.historical", "a.snapshot IS NOT NULL", "e.model_version = %s"]
    if params.labeled_only:
        where.append("a.label_verdict = 'correct'")
    else:
        where.append("(a.label_verdict IS NULL OR a.label_verdict = 'correct')")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MIN(a.ts), MAX(a.ts) FROM alerts a JOIN alert_embeddings e ON e.alert_id = a.id "
            f"WHERE {' AND '.join(where)}",  # noqa: S608
            (params.model_version,),
        )
        mn, mx = cur.fetchone()
    if mn is None:
        return None
    return (datetime.fromtimestamp(float(mn), tz=timezone.utc),
            datetime.fromtimestamp(float(mx), tz=timezone.utc))


def plan_windows(span_start: datetime, span_end: datetime, window_days: int, overlap_days: int) -> list[tuple[datetime, datetime]]:
    """Rolling windows aligned to midnight UTC of the span start; the
    last window is extended to cover span_end (+1 s so `ts < end` is
    inclusive of the newest alert)."""
    if overlap_days >= window_days:
        raise ValueError("overlap_days must be < window_days")
    step = timedelta(days=window_days - overlap_days)
    width = timedelta(days=window_days)
    start = span_start.replace(hour=0, minute=0, second=0, microsecond=0)
    hard_end = span_end + timedelta(seconds=1)
    out: list[tuple[datetime, datetime]] = []
    while True:
        end = start + width
        if end >= hard_end:
            out.append((start, hard_end))
            break
        out.append((start, end))
        start = start + step
    return out


def _covered(conn: psycopg.Connection, start: datetime, end: datetime) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM rat_cluster_runs WHERE algo = %s AND window_start = %s AND window_end = %s LIMIT 1",
            (ALGO, start, end),
        )
        return cur.fetchone() is not None


def _reset(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE alerts SET rat_id = NULL WHERE rat_id IS NOT NULL")
        n_alerts = cur.rowcount
        cur.execute("DELETE FROM rat_cluster_runs")
        cur.execute("DELETE FROM rats")
        n_rats = cur.rowcount
    conn.commit()
    print(f"reset: cleared rat_id on {n_alerts} alerts, deleted {n_rats} rats + all runs")


def _report(conn: psycopg.Connection, results: list[RunResult]) -> None:
    print()
    print("== per-window ==")
    for r in results:
        flags = [s for s in r.spillover if s["flag"]]
        print(f"  {r.summary()}")
        for s in flags[:5]:
            print(f"      spillover: cluster={s['cluster']} size={s['size']} → nn rat {s['nn_rat']} "
                  f"({s['nn_votes']}/{s.get('nn_k', 10)} votes, top sim {s['nn_top_sim']})")
    ranked = sorted(results, key=lambda r: -sum(1 for s in r.spillover if s["flag"]))
    print()
    print("== windows ranked by spillover flags (worst first) ==")
    for r in ranked[:5]:
        print(f"  {r.window_start.date()}→{r.window_end.date()}: "
              f"{sum(1 for s in r.spillover if s['flag'])} flagged of {r.rats_new} spawns")
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM rats WHERE retired_at IS NULL")
        active = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM rats")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT rat_id), COUNT(*) FROM alerts WHERE rat_id IS NOT NULL")
        distinct, assigned = cur.fetchone()
        cur.execute(
            "SELECT camera_id, COUNT(DISTINCT rat_id), COUNT(*) FROM alerts "
            "WHERE rat_id IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
        )
        per_cam = cur.fetchall()
    print()
    print(f"== catalog: rats total={total} active={active}; alerts assigned={assigned} distinct rat_id={distinct} ==")
    for cam, n_rats, n_alerts in per_cam:
        print(f"  {cam:<20} rats={n_rats:<5} alerts={n_alerts}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--overlap-days", type=int, default=5)
    # Clustering knobs default to the calibrated ClusterParams values so
    # the backfill and the nightly timer agree unless told otherwise.
    ap.add_argument("--min-cluster-size", type=int, default=ClusterParams.min_cluster_size)
    ap.add_argument("--min-samples", type=int, default=ClusterParams.min_samples,
                    help="0 → hdbscan default (= min_cluster_size)")
    ap.add_argument("--selection", choices=["leaf", "eom"], default=ClusterParams.cluster_selection_method)
    ap.add_argument("--epsilon-cosine", type=float, default=0.0)
    ap.add_argument("--link-threshold", type=float, default=ClusterParams.link_threshold)
    ap.add_argument("--retire-days", type=int, default=ClusterParams.retire_days)
    ap.add_argument("--include-unlabeled", action="store_true")
    ap.add_argument("--model-version", default=os.environ.get("CLIP_MODEL_ID") or ClusterParams.model_version)
    ap.add_argument("--dry-run", action="store_true", help="Plan + cluster + link every window; write nothing")
    ap.add_argument("--force", action="store_true", help="Re-run windows already covered by rat_cluster_runs")
    ap.add_argument("--reset", action="store_true", help="Clear rat_id / rats / runs before starting")
    ap.add_argument("--limit-windows", type=int, default=None, help="Stop after N windows (smoke)")
    args = ap.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL") or "INFO",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.database_url:
        print("ERROR: --database-url or $DATABASE_URL required", file=sys.stderr)
        return 2

    ensure_schema(args.database_url)
    params = ClusterParams(
        min_cluster_size=args.min_cluster_size,
        min_samples=args.min_samples if args.min_samples else None,
        cluster_selection_epsilon=cosine_to_euclid(args.epsilon_cosine) if args.epsilon_cosine > 0 else 0.0,
        cluster_selection_method=args.selection,
        link_threshold=args.link_threshold,
        retire_days=args.retire_days,
        labeled_only=not args.include_unlabeled,
        model_version=args.model_version,
    )
    clusterer = RatClusterer(args.database_url, params)

    with psycopg.connect(args.database_url) as conn:
        if args.reset and not args.dry_run:
            _reset(conn)
        span = _candidate_span(conn, params)
        if span is None:
            print("No candidate alerts (no TP-labelled embedded rodent alerts). Nothing to do.")
            return 0
        windows = plan_windows(span[0], span[1], args.window_days, args.overlap_days)
        print(f"span {span[0].date()} → {span[1].date()}; {len(windows)} windows of {args.window_days}d "
              f"(overlap {args.overlap_days}d); params={params.to_json()}")

        results: list[RunResult] = []
        for i, (ws, we) in enumerate(windows):
            if args.limit_windows is not None and i >= args.limit_windows:
                break
            if not args.force and not args.dry_run and _covered(conn, ws, we):
                print(f"  [skip] {ws.date()}→{we.date()} already covered by rat_cluster_runs")
                continue
            res = clusterer.run_window(ws, we, dry_run=args.dry_run,
                                       notes=f"backfill_rat_ids window {i + 1}/{len(windows)}")
            results.append(res)

        if args.dry_run:
            print()
            print("DRY RUN — nothing written. Per-window results:")
            for r in results:
                print(f"  {r.summary()}")
            return 0
        _report(conn, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
