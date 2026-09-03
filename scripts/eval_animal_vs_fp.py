"""Post-VLM classifier agreement analysis — offline read-only.

Reads:
  - `logs/classifier_shadow.jsonl` (one row per classifier prediction,
    written by src/classifier.py's ClassifierShadowLog since PR #143).
  - `alerts` table from Postgres — every VLM-passed detection carries
    `label_verdict` + `label_species` when the operator hand-labeled it
    via the /alerts UI.

Cross-references the two by (camera_id, track_id) and reports per-camera
precision/recall/F1 at candidate thresholds, plus ROC-AUC and per-species
breakdown. Purpose: pick per-camera operating thresholds for the
Phase 3 `active`-mode rollout (see [[project_wildlife_detector_137_phase3]]).

Read-only — never writes back to DB, never modifies models. Consumes
whatever the shadow soak has accumulated to date.

Ground truth mapping:
  label_verdict = 'correct'   → positive (should have alerted)
  label_verdict = 'incorrect' → negative (should have suppressed)
  label_verdict = 'unclear' or NULL → excluded from the join

At threshold T, `would_suppress = prob < T`:
  TP: correct alert   (prob >= T AND verdict = correct)
  FN: MISS            (prob <  T AND verdict = correct)  ← never want this
  FP: LEAK            (prob >= T AND verdict = incorrect)
  TN: cleaned         (prob <  T AND verdict = incorrect)

Usage:
    python scripts/eval_animal_vs_fp.py
        # --dsn defaults to $DATABASE_URL
        # --shadow defaults to logs/classifier_shadow.jsonl
        # --thresholds defaults to a spread around the current 0.20

Dev deps: same as scripts/train_animal_vs_fp.py — sklearn, psycopg[binary].
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default operating-point thresholds to sweep. Includes the currently-
# deployed 0.20 so operators can see "would-be" behavior at candidate
# tunings side-by-side with today's baseline.
DEFAULT_THRESHOLDS = [0.15, 0.20, 0.25, 0.30, 0.40, 0.50]


def fetch_labeled_alerts(dsn: str) -> dict[tuple[str, int], dict[str, Any]]:
    """Return { (camera_id, track_id): {verdict, species, ts, vlm_species} }
    for every labeled alert with a non-null track_id.

    `human_heartbeat` alerts skipped — those are periodic still-alive pings
    with NULL track_id, no classifier prediction to compare against.
    Duplicate (camera_id, track_id) rows keep the LATEST ts (labeled row
    wins over any earlier duplicate).
    """
    try:
        import psycopg  # noqa: F401
    except ImportError:
        logger.error("psycopg not installed — pip install 'psycopg[binary]'")
        sys.exit(2)
    from psycopg import connect

    q = """
        SELECT camera_id, track_id, ts, label_verdict, label_species,
               species AS vlm_species
        FROM alerts
        WHERE label_verdict IN ('correct', 'incorrect')
          AND track_id IS NOT NULL
        ORDER BY ts
    """
    labels: dict[tuple[str, int], dict[str, Any]] = {}
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(q)
        for row in cur:
            camera_id, track_id, ts, verdict, species, vlm_species = row
            labels[(camera_id, int(track_id))] = {
                "verdict": verdict,
                "species": species or "",
                "ts": float(ts),
                "vlm_species": vlm_species or "",
            }
    return labels


def load_shadow_log(path: Path) -> list[dict[str, Any]]:
    """Read the JSONL shadow log into memory. One row per classifier
    prediction. Malformed lines skipped with a warning."""
    if not path.exists():
        logger.error("Shadow log not found: %s", path)
        sys.exit(2)
    rows: list[dict[str, Any]] = []
    bad = 0
    with path.open(encoding="utf-8") as fh:
        for line_num, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError:
                bad += 1
                if bad <= 3:
                    logger.warning("Malformed JSON on line %d, skipped", line_num)
    if bad:
        logger.warning("Skipped %d malformed lines total", bad)
    return rows


def join_shadow_with_labels(
    shadow: list[dict[str, Any]],
    labels: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Inner join: keep only shadow rows whose (camera_id, track_id) has
    a labeled alert. Enriches each with verdict + species so downstream
    metrics can group by both. Same (camera_id, track_id) may appear
    multiple times in shadow (multi-VLM per track); each occurrence
    inherits the same label — that's fine, they were all judged by the
    same detection outcome."""
    joined: list[dict[str, Any]] = []
    for r in shadow:
        key = (r.get("camera_id", ""), r.get("track_id", -1))
        lbl = labels.get(key)
        if lbl is None:
            continue
        joined.append({
            **r,
            "verdict": lbl["verdict"],
            "label_species": lbl["species"],
            "alert_ts": lbl["ts"],
        })
    return joined


def confusion_at_threshold(
    rows: list[dict[str, Any]], threshold: float,
) -> tuple[int, int, int, int]:
    """(TP, FN, FP, TN) at operating point threshold.
    Positive = label_verdict='correct'; predicted-positive = prob >= threshold."""
    tp = fn = fp = tn = 0
    for r in rows:
        pos = r["verdict"] == "correct"
        pred_pos = r["prob"] >= threshold
        if pos and pred_pos:
            tp += 1
        elif pos and not pred_pos:
            fn += 1
        elif not pos and pred_pos:
            fp += 1
        else:
            tn += 1
    return tp, fn, fp, tn


def metrics(tp: int, fn: int, fp: int, tn: int) -> dict[str, float]:
    """precision, recall, f1, accuracy, MCC-equivalent. Zero-safe."""
    n = tp + fn + fp + tn
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    acc = (tp + tn) / n if n else 0.0
    # Miss rate = false negatives / all positives — the rate the
    # classifier would drop legit animals in active mode. This is
    # the metric that gates rollout more than precision does.
    miss_rate = fn / (tp + fn) if (tp + fn) else 0.0
    return {
        "precision": prec,
        "recall": rec,
        "f1": f1,
        "accuracy": acc,
        "miss_rate": miss_rate,
    }


def roc_auc_safe(rows: list[dict[str, Any]]) -> float | None:
    """ROC-AUC over the joined rows. Returns None when only one class
    is present (undefined). Uses sklearn; degrades to a warning if
    sklearn is missing rather than failing the whole eval."""
    try:
        from sklearn.metrics import roc_auc_score
    except ImportError:
        logger.warning("sklearn not installed — skipping ROC-AUC")
        return None
    y = [1 if r["verdict"] == "correct" else 0 for r in rows]
    if len(set(y)) < 2:
        return None
    p = [r["prob"] for r in rows]
    return float(roc_auc_score(y, p))


def report_per_camera(
    joined: list[dict[str, Any]], thresholds: list[float],
) -> None:
    by_cam: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in joined:
        by_cam[r["camera_id"]].append(r)

    print("\n" + "=" * 78)
    print("PER-CAMERA AGREEMENT (shadow classifier vs operator labels)")
    print("=" * 78)

    for cam in sorted(by_cam):
        rows = by_cam[cam]
        pos = sum(1 for r in rows if r["verdict"] == "correct")
        neg = len(rows) - pos
        auc = roc_auc_safe(rows)
        auc_str = f"{auc:.3f}" if auc is not None else "n/a"
        print(f"\n─── {cam} ─────────────────────────────────────────────────")
        print(f"  labeled overlap: n={len(rows)}  positives={pos}  negatives={neg}  "
              f"ROC-AUC={auc_str}")
        if pos == 0 or neg == 0:
            print(f"  ! only one class present — skipping threshold sweep")
            continue
        print(f"  {'thresh':>8}  {'TP':>6}  {'FN':>6}  {'FP':>6}  {'TN':>6}  "
              f"{'prec':>6}  {'recall':>7}  {'F1':>6}  {'miss%':>6}")
        for t in thresholds:
            tp, fn, fp, tn = confusion_at_threshold(rows, t)
            m = metrics(tp, fn, fp, tn)
            print(f"  {t:>8.2f}  {tp:>6}  {fn:>6}  {fp:>6}  {tn:>6}  "
                  f"{m['precision']:>6.3f}  {m['recall']:>7.3f}  "
                  f"{m['f1']:>6.3f}  {m['miss_rate']*100:>5.1f}%")


def report_per_species(joined: list[dict[str, Any]]) -> None:
    """Per-species precision at the current 0.20 threshold. Answers 'is
    the classifier biased against squirrels? overkilling on rodents?'"""
    print("\n" + "=" * 78)
    print("PER-SPECIES BEHAVIOR (at threshold=0.20)")
    print("=" * 78)
    T = 0.20
    by_species: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in joined:
        # label_species is the ground-truth species — take precedence
        # over VLM's guess (VLM already tried and the operator judged it).
        sp = r["label_species"] or f"vlm:{r.get('vlm_species','')}"
        by_species[sp].append(r)

    print(f"  {'species':<20} {'n':>6}  {'pos':>5}  {'neg':>5}  "
          f"{'suppr':>6}  {'miss':>5}  {'miss_rate':>10}")
    for sp in sorted(by_species, key=lambda s: -len(by_species[s])):
        rows = by_species[sp]
        pos = sum(1 for r in rows if r["verdict"] == "correct")
        neg = len(rows) - pos
        suppressed = sum(1 for r in rows if r["prob"] < T)
        # Missed = pos AND would-suppress
        miss = sum(1 for r in rows if r["verdict"] == "correct" and r["prob"] < T)
        miss_rate = miss / pos * 100 if pos else 0.0
        print(f"  {sp[:20]:<20} {len(rows):>6}  {pos:>5}  {neg:>5}  "
              f"{suppressed:>6}  {miss:>5}  {miss_rate:>9.1f}%")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=os.getenv("DATABASE_URL", ""))
    ap.add_argument("--shadow", type=Path,
                    default=Path("logs/classifier_shadow.jsonl"))
    ap.add_argument(
        "--thresholds", type=str,
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
        help="Comma-separated operating points to sweep.",
    )
    args = ap.parse_args()

    if not args.dsn:
        logger.error("--dsn required (or set DATABASE_URL)")
        return 2

    thresholds = [float(t.strip()) for t in args.thresholds.split(",")]

    logger.info("Reading shadow log %s ...", args.shadow)
    shadow = load_shadow_log(args.shadow)
    logger.info("Shadow rows: %d", len(shadow))

    logger.info("Fetching labeled alerts from %s ...", args.dsn.split("@")[-1])
    labels = fetch_labeled_alerts(args.dsn)
    logger.info("Labeled alerts (with track_id): %d", len(labels))

    joined = join_shadow_with_labels(shadow, labels)
    logger.info("Joined rows: %d", len(joined))
    if not joined:
        logger.error("No overlap between shadow log and labeled alerts — bailing")
        return 1

    # Global summary before per-camera breakdown.
    verdict_counts = Counter(r["verdict"] for r in joined)
    print(f"\nGlobal join summary: {dict(verdict_counts)}")

    report_per_camera(joined, thresholds)
    report_per_species(joined)
    return 0


if __name__ == "__main__":
    sys.exit(main())
