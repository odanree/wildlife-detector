"""Train a LightGBM binary classifier: pre-VLM moth vs real-animal.

Reads:
  - `pre_vlm_drop_labels` table from Postgres (hand-labels from the
    /drops labeling page)
  - `logs/pre_vlm_drops.jsonl` (brightness features per drop, produced
    by src/classifier.py's PreVlmDropSink)

Trains a LightGBM model that predicts P(real_animal | brightness features).
Purpose: eventually replace/augment the current NIGHT_INSECT_BRIGHTNESS_MIN
threshold with a learned model that preserves real animals the current
filter kills.

Feature contract mirrors what the pipeline will pass at predict time —
same fields the drop-log records. Any change here must land in a
consumer PR that updates both the pipeline call site and this script.

Usage:
    python scripts/train_pre_vlm_filter.py
        # --dsn defaults to $DATABASE_URL
        # --output defaults to models/pre_vlm_filter.txt

Dev deps: same as scripts/train_animal_vs_fp.py (lightgbm, sklearn,
pandas, psycopg[binary]). Install with:
    pip install -r requirements-train.txt
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Feature spec — read from the JSONL row. `baseline_mode` intentionally
# excluded: too sparse and correlated with brightness features that
# already encode day/night. `trigger` kept as categorical since the
# three trigger types (mean/max/elong) each capture a distinct FP
# class the filter is currently killing.
NUMERIC_FEATURES = [
    "mean",       # tight-bbox mean brightness
    "max",        # tight-bbox max brightness
    "ar",         # tight-bbox aspect ratio (max/min side)
    "bbox_w",
    "bbox_h",
    "area",
    "wide_mean",  # wider-context mean brightness (from _wide_bbox_coords)
    "wide_max",
]
CATEGORICAL_FEATURES = ["camera_id", "trigger"]
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES


def fetch_labels(dsn: str) -> dict[str, str]:
    """Return { drop_id: label } for moth + real_animal labels only.
    `unclear` labels are excluded — they're operator-uncertainty, not
    a training signal."""
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT drop_id, label FROM pre_vlm_drop_labels "
            "WHERE label IN ('moth', 'real_animal')"
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def build_dataset(jsonl_path: Path, labels: dict[str, str]) -> list[dict]:
    """Read the JSONL, keep rows whose `snapshot` matches a labeled
    drop_id. y=1 for real_animal, y=0 for moth."""
    kept = 0
    missing_features: Counter[str] = Counter()
    rows: list[dict] = []
    with jsonl_path.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            snap = r.get("snapshot")
            if not snap or snap not in labels:
                continue
            row: dict[str, Any] = {
                "drop_id": snap,
                "camera_id": r.get("camera_id") or "unknown",
                "trigger": r.get("trigger") or "unknown",
                "y": 1 if labels[snap] == "real_animal" else 0,
            }
            ok = True
            for k in NUMERIC_FEATURES:
                v = r.get(k)
                if v is None:
                    missing_features[k] += 1
                    ok = False
                    break
                row[k] = float(v)
            if ok:
                rows.append(row)
                kept += 1
    if missing_features:
        logger.warning(
            "Dropped %d labeled rows missing features: %s",
            sum(missing_features.values()), dict(missing_features),
        )
    logger.info("Kept %d labeled rows with complete features", kept)
    return rows


def stratified_split(rows: list[dict], test_frac: float, val_frac: float,
                     seed: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Simple stratified-by-label random split. Small dataset (n<300)
    doesn't warrant group splits — no track-adjacency to worry about
    since each drop is a single-frame decision, not a rolling track."""
    import random
    rng = random.Random(seed)
    pos = [r for r in rows if r["y"] == 1]
    neg = [r for r in rows if r["y"] == 0]
    rng.shuffle(pos)
    rng.shuffle(neg)

    def split(xs: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        n = len(xs)
        n_test = max(1, int(n * test_frac))
        n_val = max(1, int(n * val_frac))
        return xs[:n_test], xs[n_test:n_test + n_val], xs[n_test + n_val:]

    p_test, p_val, p_train = split(pos)
    n_test, n_val, n_train = split(neg)
    train = p_train + n_train
    val = p_val + n_val
    test = p_test + n_test
    rng.shuffle(train)
    rng.shuffle(val)
    rng.shuffle(test)
    return train, val, test


def train_model(rows: list[dict], seed: int = 42,
                save_path: Path | None = None) -> dict:
    import lightgbm as lgb
    import pandas as pd
    from sklearn.metrics import (
        precision_recall_fscore_support, roc_auc_score,
        average_precision_score, confusion_matrix,
    )

    train_rows, val_rows, test_rows = stratified_split(
        rows, test_frac=0.15, val_frac=0.15, seed=seed,
    )
    logger.info(
        "Split — train=%d val=%d test=%d",
        len(train_rows), len(val_rows), len(test_rows),
    )

    def to_xy(rs: list[dict]):
        d = pd.DataFrame(rs)
        y = d["y"].to_numpy()
        for col in CATEGORICAL_FEATURES:
            d[col] = d[col].astype("category")
        return d[ALL_FEATURES], y

    X_train, y_train = to_xy(train_rows)
    X_val, y_val = to_xy(val_rows)
    X_test, y_test = to_xy(test_rows)

    pos_weight = float((y_train == 0).sum()) / max(1, (y_train == 1).sum())
    logger.info(
        "Class balance — train pos=%d neg=%d weight=%.2f",
        (y_train == 1).sum(), (y_train == 0).sum(), pos_weight,
    )

    train_set = lgb.Dataset(
        X_train, label=y_train, categorical_feature=CATEGORICAL_FEATURES,
        weight=[pos_weight if y else 1.0 for y in y_train],
    )
    val_set = lgb.Dataset(
        X_val, label=y_val, categorical_feature=CATEGORICAL_FEATURES,
        reference=train_set,
    )

    params = {
        "objective": "binary",
        "metric": "average_precision",
        "learning_rate": 0.05,
        # Modest capacity — small dataset (~200 rows), so a big tree
        # will overfit trivially. Constrain leaves + min-in-leaf.
        "num_leaves": 15,
        "min_data_in_leaf": 5,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "seed": seed,
        "verbosity": -1,
    }
    model = lgb.train(
        params, train_set,
        num_boost_round=300,
        valid_sets=[val_set],
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)],
    )

    y_pred_proba = model.predict(X_test)
    for op in (0.2, 0.3, 0.5):
        y_pred = (y_pred_proba >= op).astype(int)
        p, r, f, _ = precision_recall_fscore_support(
            y_test, y_pred, average="binary", zero_division=0,
        )
        if len(set(y_test)) > 1:
            tn, fp, fn, tp = confusion_matrix(y_test, y_pred).ravel()
        else:
            tn = fp = fn = tp = 0
        logger.info(
            "op@%.1f — precision=%.3f recall=%.3f f1=%.3f "
            "TP=%d FP=%d FN=%d TN=%d",
            op, p, r, f, tp, fp, fn, tn,
        )

    if len(set(y_test)) > 1:
        auc = roc_auc_score(y_test, y_pred_proba)
        ap = average_precision_score(y_test, y_pred_proba)
        logger.info("Test ROC-AUC=%.4f  Avg-Precision=%.4f", auc, ap)
    else:
        auc = ap = float("nan")
        logger.warning("Test set is single-class — AUC/AP undefined")

    # Per-camera recall @ 0.2 threshold — the operating point where
    # a "real animal" prob >= 0.2 keeps the row alive (i.e. we'd
    # NOT drop it via insect pre-filter).
    logger.info("Per-camera recall @ threshold=0.2 (positive class = real animal):")
    test_df = pd.DataFrame(test_rows)
    test_df["proba"] = y_pred_proba
    for cam, grp in test_df.groupby("camera_id", observed=True):
        pos = grp[grp["y"] == 1]
        if len(pos) == 0:
            logger.info("  %s — 0 positives in test set", cam)
            continue
        rec = (pos["proba"] >= 0.2).mean()
        logger.info("  %s — n_pos=%d recall=%.3f", cam, len(pos), rec)

    # Feature importance — cheap sanity check that we're leaning on
    # the right signals.
    logger.info("Feature importance (gain):")
    fi = sorted(
        zip(model.feature_name(), model.feature_importance(importance_type="gain")),
        key=lambda x: -x[1],
    )
    for name, gain in fi:
        logger.info("  %-14s %.1f", name, gain)

    metrics = {
        "n_train": len(train_rows),
        "n_val": len(val_rows),
        "n_test": len(test_rows),
        "roc_auc": float(auc) if auc == auc else None,  # NaN check
        "avg_precision": float(ap) if ap == ap else None,
        "class_balance_train": {
            "positive": int((y_train == 1).sum()),
            "negative": int((y_train == 0).sum()),
        },
        "feature_importance_gain": {name: float(g) for name, g in fi},
    }

    if save_path:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        model.save_model(str(save_path))
        (save_path.with_suffix(".meta.json")).write_text(json.dumps({
            "features": ALL_FEATURES,
            "categorical_features": CATEGORICAL_FEATURES,
            "target": "real_animal (1) vs moth (0)",
            "metrics": metrics,
        }, indent=2))
        logger.info("Saved model → %s", save_path)

    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--dsn", default=os.getenv("DATABASE_URL", ""),
                        help="Postgres DSN (default: $DATABASE_URL)")
    parser.add_argument(
        "--jsonl",
        default=os.getenv("PRE_VLM_DROP_LOG_PATH", "logs/pre_vlm_drops.jsonl"),
        help="Path to pre_vlm_drops.jsonl (default: $PRE_VLM_DROP_LOG_PATH "
             "or logs/pre_vlm_drops.jsonl)",
    )
    parser.add_argument("--output", default="models/pre_vlm_filter.txt")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-labels", type=int, default=50,
                        help="Refuse to train if fewer hand-labels than this")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.dsn:
        logger.error("No DSN — set --dsn or DATABASE_URL")
        return 2
    jsonl = Path(args.jsonl)
    if not jsonl.exists():
        logger.error("JSONL not found at %s", jsonl)
        return 2

    logger.info("Fetching labels from %s ...", args.dsn)
    labels = fetch_labels(args.dsn)
    balance = Counter(labels.values())
    logger.info("Fetched labels — moth=%d real_animal=%d",
                balance.get("moth", 0), balance.get("real_animal", 0))
    if sum(balance.values()) < args.min_labels:
        logger.error(
            "Only %d hand-labels, need at least %d. Keep labeling.",
            sum(balance.values()), args.min_labels,
        )
        return 3

    logger.info("Reading JSONL %s ...", jsonl)
    rows = build_dataset(jsonl, labels)
    if len(rows) < args.min_labels:
        logger.error(
            "Only %d rows matched (JSONL join), need at least %d",
            len(rows), args.min_labels,
        )
        return 4

    pos = sum(1 for r in rows if r["y"] == 1)
    neg = len(rows) - pos
    logger.info("Dataset — positive(real_animal)=%d negative(moth)=%d", pos, neg)
    if pos < 20 or neg < 20:
        logger.error(
            "Need ≥ 20 of each class to train usefully (have pos=%d neg=%d)",
            pos, neg,
        )
        return 5

    train_model(rows, seed=args.seed, save_path=Path(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
