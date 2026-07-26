"""Grid-search motion-detector configs against the whole ground_truth
clip corpus. For each (config, clip) pair: run replay() and tally
whether an alert would have fired. Aggregate: TP-retention rate,
FP-rejection rate, precision under that config.

Usage:
    python sandbox/sweep.py                     # default grid
    python sandbox/sweep.py --config baseline   # single config = production defaults
    python sandbox/sweep.py --limit-clips 4     # smoke test on first N clips

Reads ground_truth.json produced by fetch_clips.py.
Writes results/sweep_<timestamp>.csv + prints summary table.

Pattern: **shadow-model precision-recall sweep** — offline eval that
doesn't touch production, output is a Pareto frontier over knob
values. Same shape as an ML hyperparameter grid, just with a hand-
built "model" (the motion pipeline) instead of a trained one.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from itertools import product
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from sandbox.replay import replay  # noqa: E402

MANIFEST = SCRIPT_DIR / "ground_truth.json"
CLIPS_DIR = SCRIPT_DIR / "clips"
RESULTS_DIR = SCRIPT_DIR / "results"

# Production defaults — pulled from motion_detector.py + detection.yaml
# so a "baseline" run reproduces current behavior. Update if production
# defaults change.
BASELINE = {
    "min_track_age": 2,
    "max_velocity": 40,
    "min_area": 80,
    "max_area": 4000,
    "var_threshold": 18.0,
    "history": 400,
    "edge_margin": 20,
    "use_zone": True,
}

# The grid. Each key is a config dim; the value is the list of values
# to test. Cartesian product = one run per combination. Keep this small
# — 60 combos × 46 clips × ~2.6s = 2h. Trimmed to focus on the primary
# knobs. Expand later once we've picked a corridor of interest.
GRID = {
    "min_track_age": [2, 3, 4, 5, 6],           # persistence gate — primary knob
    "min_area":      [80, 200],                 # small-blob rejection
}


def _iter_configs(grid: dict) -> list[dict]:
    """Cartesian product of the grid → list of full config dicts, each
    complete (missing keys filled from BASELINE)."""
    keys = list(grid.keys())
    combos = list(product(*(grid[k] for k in keys)))
    return [{**BASELINE, **dict(zip(keys, c))} for c in combos]


def _short_config_id(cfg: dict) -> str:
    """Compact human-readable id for a config — used as row key in CSV
    and terminal table so operator can eyeball which config wins."""
    varying = [k for k in GRID if cfg.get(k) != BASELINE.get(k)]
    if not varying:
        return "baseline"
    return ",".join(f"{k}={cfg[k]}" for k in varying)


def run_sweep(clip_records: list[dict], configs: list[dict]) -> list[dict]:
    """For each (config, clip) run replay() and return flat rows."""
    rows: list[dict] = []
    total = len(configs) * len(clip_records)
    n = 0
    for cfg in configs:
        cid = _short_config_id(cfg)
        for rec in clip_records:
            n += 1
            clip_path = CLIPS_DIR / rec["clip"]
            if not clip_path.exists():
                print(f"  [{n:>4}/{total}] SKIP (missing) {rec['clip']}", file=sys.stderr)
                continue
            result = replay(
                clip_path,
                min_track_age=cfg["min_track_age"],
                max_velocity=cfg["max_velocity"],
                min_area=cfg["min_area"],
                max_area=cfg["max_area"],
                var_threshold=cfg["var_threshold"],
                history=cfg["history"],
                edge_margin=cfg["edge_margin"],
                use_zone=cfg["use_zone"],
            )
            rows.append({
                "config_id": cid,
                "alert_id": rec["id"],
                "verdict_truth": rec["verdict"],   # TP or FP
                "clip": rec["clip"],
                "would_fire": result["would_fire_alert"],
                "dets_post_zone": result["detections_post_zone"],
                "first_hit_frame": result["first_hit_frame"],
                **{f"cfg_{k}": v for k, v in cfg.items()},
            })
            if n % 10 == 0 or n == total:
                print(f"  [{n:>4}/{total}] done", file=sys.stderr)
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    """Per-config: TP retention, FP rejection, precision."""
    by_config: dict[str, dict] = {}
    for r in rows:
        cid = r["config_id"]
        agg = by_config.setdefault(cid, {"tp_fires": 0, "tp_total": 0, "fp_fires": 0, "fp_total": 0})
        if r["verdict_truth"] == "TP":
            agg["tp_total"] += 1
            if r["would_fire"]:
                agg["tp_fires"] += 1
        else:  # FP
            agg["fp_total"] += 1
            if r["would_fire"]:
                agg["fp_fires"] += 1

    out = []
    for cid, agg in by_config.items():
        tp_ret = agg["tp_fires"] / agg["tp_total"] if agg["tp_total"] else 0
        fp_rej = 1 - (agg["fp_fires"] / agg["fp_total"]) if agg["fp_total"] else 0
        # New precision if we applied this filter (naive — assumes tp/fp
        # in-sample proportions match production, which they don't since
        # we sampled 30 FPs but have all 16 TPs).
        fires = agg["tp_fires"] + agg["fp_fires"]
        prec = agg["tp_fires"] / fires if fires else 0
        out.append({
            "config_id": cid,
            "tp_fires": agg["tp_fires"], "tp_total": agg["tp_total"],
            "fp_fires": agg["fp_fires"], "fp_total": agg["fp_total"],
            "tp_retention": tp_ret,
            "fp_rejection": fp_rej,
            "sandbox_precision": prec,
        })
    # Rank by (tp_retention desc, fp_rejection desc) — Pareto-frontier
    # candidates float to the top.
    out.sort(key=lambda r: (r["tp_retention"], r["fp_rejection"]), reverse=True)
    return out


def print_table(summary: list[dict]) -> None:
    print(f"{'config':<45}{'TP fire':<10}{'FP fire':<10}{'TP ret':<10}{'FP rej':<10}{'prec (sandbox)'}")
    print("-" * 100)
    for r in summary:
        tp = f"{r['tp_fires']:>3}/{r['tp_total']}"
        fp = f"{r['fp_fires']:>3}/{r['fp_total']}"
        print(f"{r['config_id']:<45}{tp:<10}{fp:<10}"
              f"{r['tp_retention']*100:>5.1f}%   {r['fp_rejection']*100:>5.1f}%   {r['sandbox_precision']*100:>5.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", choices=["baseline", "grid"], default="grid",
                    help="baseline = production defaults only; grid = full sweep")
    ap.add_argument("--limit-clips", type=int, default=0,
                    help="only replay first N clips (smoke test)")
    args = ap.parse_args()

    if not MANIFEST.exists():
        raise SystemExit(f"No manifest at {MANIFEST} — run fetch_clips.py first.")
    manifest = json.loads(MANIFEST.read_text())
    # Keep only records that actually have a downloaded clip.
    clip_records = [r for r in manifest if r.get("clip") and (CLIPS_DIR / r["clip"]).exists()]
    if args.limit_clips > 0:
        clip_records = clip_records[:args.limit_clips]
    print(f"Corpus: {len(clip_records)} clips "
          f"({sum(1 for r in clip_records if r['verdict']=='TP')} TP, "
          f"{sum(1 for r in clip_records if r['verdict']=='FP')} FP)")

    configs = [BASELINE] if args.config == "baseline" else _iter_configs(GRID)
    print(f"Configs: {len(configs)}")

    rows = run_sweep(clip_records, configs)
    summary = summarize(rows)

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"sweep_{stamp}.csv"
    with csv_path.open("w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"\nDetail rows: {csv_path}")
    print()
    print_table(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
