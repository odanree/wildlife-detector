"""Clusterer entrypoint — one-shot or scheduled batch.

    python -m src.clusterer.main --window-days 30              # one run, exit
    python -m src.clusterer.main --window-days 30 --every-hours 24
                                                               # nightly loop
    python -m src.clusterer.main --window-days 30 --dry-run    # cluster + link, write nothing

Pattern: **request-driven batch on a timer**, not LISTEN/NOTIFY. The
embedder reacts per alert because one CLIP forward pass is cheap and
independent; clustering is a global operation over the window — running
it per alert would recompute the same HDBSCAN 500x a night for no gain.
A once-a-day trailing window is the right granularity for "how many
distinct rats in the last N hours" (the 2c UI reads rat_id straight
from SQL — no ML at query time).

Scheduling lives in-process (`--every-hours`) rather than in a sidecar
that `docker exec`s into this container: that sidecar would need the
Docker socket mounted, which is a root-equivalent trust boundary we do
not want to open for a cron. docker-compose.yml runs this module in
loop mode as `clusterer-timer`; the `clusterer` service (profile
`tools`) is the same image for one-shots and the backfill.

Env → CLI defaults (same `or default` discipline as the embedder — the
compose env_file passes set-but-empty strings):

    CLUSTER_WINDOW_DAYS, CLUSTER_MIN_CLUSTER_SIZE, CLUSTER_MIN_SAMPLES,
    CLUSTER_SELECTION, CLUSTER_EPSILON_COSINE, CLUSTER_LINK_THRESHOLD,
    CLUSTER_RETIRE_DAYS, CLUSTER_EVERY_HOURS, CLIP_MODEL_ID
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from src.clusterer.clusterer import ClusterParams, RatClusterer, cosine_to_euclid
from src.clusterer.schema import ensure_schema

logger = logging.getLogger(__name__)


def _env(name: str, default):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--window-days", type=int, default=int(_env("CLUSTER_WINDOW_DAYS", 30)))
    ap.add_argument("--min-cluster-size", type=int,
                    default=int(_env("CLUSTER_MIN_CLUSTER_SIZE", ClusterParams.min_cluster_size)))
    ap.add_argument("--min-samples", type=int, default=_env("CLUSTER_MIN_SAMPLES", ClusterParams.min_samples),
                    help="HDBSCAN min_samples (default: %(default)s; 0 → hdbscan default = min_cluster_size)")
    ap.add_argument("--selection", choices=["leaf", "eom"],
                    default=_env("CLUSTER_SELECTION", ClusterParams.cluster_selection_method),
                    help="HDBSCAN cluster_selection_method. 'eom' collapses this corpus into one blob; "
                         "see clusterer.py 'Parameter calibration'.")
    ap.add_argument(
        "--epsilon-cosine", type=float, default=float(_env("CLUSTER_EPSILON_COSINE", 0.0)),
        help="cluster_selection_epsilon expressed as a COSINE floor (0 = off). "
             "Clusters whose mutual-reachability gap is tighter than this are not split. "
             "Converted to euclidean-on-unit-vectors internally.",
    )
    ap.add_argument("--link-threshold", type=float,
                    default=float(_env("CLUSTER_LINK_THRESHOLD", ClusterParams.link_threshold)))
    ap.add_argument("--retire-days", type=int, default=int(_env("CLUSTER_RETIRE_DAYS", ClusterParams.retire_days)))
    ap.add_argument("--include-unlabeled", action="store_true",
                    help="Also cluster unlabelled rodent alerts (default: label_verdict='correct' only)")
    ap.add_argument("--model-version", default=_env("CLIP_MODEL_ID", ClusterParams.model_version))
    ap.add_argument("--every-hours", type=float, default=_env("CLUSTER_EVERY_HOURS", None),
                    help="Loop: run, sleep this many hours, repeat. Omit for one-shot.")
    ap.add_argument("--dry-run", action="store_true", help="Cluster + link, write nothing")
    ap.add_argument("--notes", default=None, help="Free text stored on rat_cluster_runs.notes")
    return ap


def params_from_args(args: argparse.Namespace) -> ClusterParams:
    ms = int(args.min_samples) if args.min_samples is not None else None
    return ClusterParams(
        min_cluster_size=args.min_cluster_size,
        min_samples=ms if ms and ms > 0 else None,
        cluster_selection_epsilon=cosine_to_euclid(args.epsilon_cosine) if args.epsilon_cosine > 0 else 0.0,
        cluster_selection_method=args.selection,
        link_threshold=args.link_threshold,
        retire_days=args.retire_days,
        labeled_only=not args.include_unlabeled,
        model_version=args.model_version,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL") or "INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    if not args.database_url:
        print("ERROR: --database-url or $DATABASE_URL required", file=sys.stderr)
        return 2

    # Fail-fast at the trust boundary: no pgvector / no alert_embeddings →
    # no service. A crash-looping container beats a timer that "runs"
    # nightly and writes nothing.
    ensure_schema(args.database_url)

    params = params_from_args(args)
    clusterer = RatClusterer(args.database_url, params)
    logger.info("clusterer params: %s window_days=%d", params.to_json(), args.window_days)

    every_s = float(args.every_hours) * 3600.0 if args.every_hours else None
    while True:
        try:
            res = clusterer.run_trailing(
                args.window_days, now=datetime.now(timezone.utc), dry_run=args.dry_run, notes=args.notes,
            )
            for s in res.spillover:
                if s["flag"]:
                    logger.warning("spillover candidate: %s", s)
        except Exception:
            if every_s is None:
                raise
            # Restart-loop resiliency for the scheduled shape: log and
            # try again next tick. rat_cluster_runs has no row for a
            # failed run, so the gap is visible in the audit table.
            logger.exception("clusterer run failed; retrying next tick")
        if every_s is None:
            return 0
        logger.info("sleeping %.1fh until next run", every_s / 3600.0)
        time.sleep(every_s)


if __name__ == "__main__":
    sys.exit(main())
