"""Train a LightGBM classifier: P(animal | bbox brightness features).

Reads labeled alerts from the postgres `alerts` table, parses the
brightness/geometry features already embedded in `description` text (PRs
#106/#107/#120 added them), splits by (camera_id, ts_bucket_5min) to
avoid track-level leakage, trains a LightGBM binary classifier, and
saves the model + feature list + threshold metrics to `models/`.

Usage:
    python scripts/train_animal_vs_fp.py              # trains on all labels
    python scripts/train_animal_vs_fp.py --min-labels 100  # skip cams with fewer labels
    python scripts/train_animal_vs_fp.py --camera backyard # single-camera model
    python scripts/train_animal_vs_fp.py --output models/animal_vs_fp_v1.pkl

Requires: lightgbm, scikit-learn, psycopg[binary]. Installable via
`pip install lightgbm scikit-learn` (both are pure-python-ish wheels).

Data assumptions (per project issue #137):
  - Labels reach at least ~1000 positives + ~3000 negatives
  - Descriptions contain the "mean=X max=Y AR=Z wide_mean=W wide_max=V" suffix
    from PR #107/#120 (skips old rows without those fields)

Metrics reported: per-camera recall/precision on positive class (real
animals), plus the FP rejection rate at operating threshold. Compared
against the current NIGHT_INSECT_BRIGHTNESS_MIN=130 filter's
implicit decisions for the same data.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Feature spec — parsed from `description` text. Matches the format
# our pipeline currently writes (PR #107 + #120). Ordered = column
# order for the training matrix + model input.
# ─────────────────────────────────────────────────────────────────────
NUMERIC_FEATURES = [
    "mean",         # tight-bbox mean brightness (0-255)
    "max",          # tight-bbox max brightness
    "ar",           # tight-bbox aspect ratio (max_dim/min_dim)
    "bbox_w",       # tight-bbox width in px
    "bbox_h",       # tight-bbox height in px
    "wide_mean",    # wide-crop mean brightness
    "wide_max",     # wide-crop max brightness
    "conf",         # VLM's own confidence (0-1)
]
CATEGORICAL_FEATURES = ["camera_id", "vlm_species"]
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

# Regex to pull the suffix "[bbox WxH=Npx mean=X max=Y AR=Z wide_mean=W wide_max=V]"
BBOX_RE = re.compile(
    r"bbox\s+(?P<bw>\d+)x(?P<bh>\d+)=\d+px\s+"
    r"mean=(?P<mean>\d+)\s+max=(?P<max>\d+)\s+AR=(?P<ar>[\d.]+)"
    r"(?:\s+wide_mean=(?P<wmean>\d+)\s+wide_max=(?P<wmax>\d+))?"
)


def fetch_labeled_alerts(dsn: str, camera: str | None = None) -> list[dict]:
    """Query alerts table for labeled rows with feature-embedded descriptions.
    Returns list of dicts with parsed features + label."""
    import psycopg
    where = ["description ~ 'mean=[0-9]+'", "label_verdict IS NOT NULL", "label_ts IS NOT NULL"]
    params: list[Any] = []
    if camera:
        where.append("camera_id = %s")
        params.append(camera)
    sql = f"""
        SELECT id, camera_id, ts, species, confidence, description,
               label_verdict, label_species
        FROM alerts
        WHERE {' AND '.join(where)}
        ORDER BY ts
    """
    rows: list[dict] = []
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            id_, cam, ts, species, conf, desc, verdict, label_species = row
            match = BBOX_RE.search(desc or "")
            if not match:
                continue
            groups = match.groupdict()
            rows.append({
                "id": id_,
                "camera_id": cam,
                "ts": float(ts),
                "vlm_species": species or "unknown",
                "conf": float(conf) if conf is not None else 0.0,
                "bbox_w": int(groups["bw"]),
                "bbox_h": int(groups["bh"]),
                "mean": int(groups["mean"]),
                "max": int(groups["max"]),
                "ar": float(groups["ar"]),
                "wide_mean": int(groups["wmean"]) if groups["wmean"] else None,
                "wide_max": int(groups["wmax"]) if groups["wmax"] else None,
                "label_verdict": verdict,
                "label_species": label_species,
                # y: 1 if animal, 0 if FP. Verdict "correct" AND label starts with "real_"
                "y": int(verdict == "correct" and (label_species or "").startswith("real_")),
            })
    return rows


def track_group_id(row: dict, bucket_s: int = 300) -> str:
    """Deterministic group id for split boundaries. Same (camera, 5min bucket)
    → same group → all rows go to same split. Prevents leakage from adjacent
    frames of one crossing landing in both train and test."""
    return f"{row['camera_id']}:{int(row['ts']) // bucket_s}"


def stratified_group_split(rows: list[dict], test_frac: float, val_frac: float,
                            seed: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Split rows into train/val/test by group id (see track_group_id).
    Preserves label balance within each split as best we can given the
    group constraint. Deterministic given seed."""
    import random
    rng = random.Random(seed)
    # Group rows by group_id
    by_group: dict[str, list[dict]] = {}
    for r in rows:
        by_group.setdefault(track_group_id(r), []).append(r)
    groups = list(by_group.keys())
    rng.shuffle(groups)
    n = len(groups)
    n_test = int(n * test_frac)
    n_val = int(n * val_frac)
    test_g, val_g, train_g = groups[:n_test], groups[n_test:n_test + n_val], groups[n_test + n_val:]
    def collect(gs: list[str]) -> list[dict]:
        out: list[dict] = []
        for g in gs:
            out.extend(by_group[g])
        return out
    return collect(train_g), collect(val_g), collect(test_g)


def build_frame(rows: list[dict]):
    """Build a pandas DataFrame with all features + target. Missing
    wide_mean/wide_max (pre-PR #120 rows) filled with tight equivalents."""
    import pandas as pd
    df = pd.DataFrame(rows)
    # Fill missing wide_* with tight equivalents (safe fallback for
    # older rows written before PR #120 shipped)
    df["wide_mean"] = df["wide_mean"].fillna(df["mean"])
    df["wide_max"] = df["wide_max"].fillna(df["max"])
    return df


def train_model(df, seed: int = 42, save_path: Path | None = None) -> dict:
    """Train a LightGBM classifier on the labeled feature frame. Returns
    dict of metrics + saves the trained model to save_path (if given)."""
    import lightgbm as lgb
    from sklearn.metrics import (
        precision_recall_fscore_support, roc_auc_score, average_precision_score,
        confusion_matrix,
    )

    train_rows, val_rows, test_rows = stratified_group_split(
        df.to_dict("records"), test_frac=0.15, val_frac=0.15, seed=seed,
    )
    logger.info("Split sizes — train=%d val=%d test=%d", len(train_rows), len(val_rows), len(test_rows))

    def to_xy(rows: list[dict]):
        import pandas as pd
        d = pd.DataFrame(rows)
        y = d["y"].to_numpy()
        # Categorical fields → category dtype (LightGBM native support)
        for col in CATEGORICAL_FEATURES:
            d[col] = d[col].astype("category")
        X = d[ALL_FEATURES]
        return X, y

    X_train, y_train = to_xy(train_rows)
    X_val, y_val = to_xy(val_rows)
    X_test, y_test = to_xy(test_rows)

    # Class weight — dataset is ~1:3 (positives:negatives). Boost positive
    # weight so we don't under-recall real animals.
    pos_weight = float((y_train == 0).sum()) / max(1, (y_train == 1).sum())
    logger.info("Class balance — train positive=%d negative=%d weight=%.2f",
                (y_train == 1).sum(), (y_train == 0).sum(), pos_weight)

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
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "seed": seed,
        "verbosity": -1,
    }
    model = lgb.train(
        params, train_set,
        num_boost_round=500,
        valid_sets=[val_set],
        callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)],
    )

    # Evaluate on test set
    y_pred_proba = model.predict(X_test)
    for op_threshold in (0.2, 0.3, 0.5):
        y_pred = (y_pred_proba >= op_threshold).astype(int)
        p, r, f, _ = precision_recall_fscore_support(y_test, y_pred, average="binary", zero_division=0)
        tn, fp, fn, tp = confusion_matrix(y_test, y_pred).ravel()
        logger.info(
            "op@%.1f — precision=%.3f recall=%.3f f1=%.3f  TP=%d FP=%d FN=%d TN=%d",
            op_threshold, p, r, f, tp, fp, fn, tn,
        )

    auc = roc_auc_score(y_test, y_pred_proba)
    ap = average_precision_score(y_test, y_pred_proba)
    logger.info("Test ROC-AUC=%.4f  Avg-Precision=%.4f", auc, ap)

    # Per-camera recall at operating threshold 0.2 (conservative)
    logger.info("Per-camera recall @ threshold=0.2 (positive class = real animal):")
    import pandas as pd
    test_df = pd.DataFrame(test_rows)
    test_df["proba"] = y_pred_proba
    test_df["y"] = y_test
    for cam, grp in test_df.groupby("camera_id"):
        pos = grp[grp["y"] == 1]
        if len(pos) == 0:
            logger.info("  %s — 0 positives in test set", cam)
            continue
        pos_recall = (pos["proba"] >= 0.2).mean()
        logger.info("  %s — n_pos=%d recall=%.3f", cam, len(pos), pos_recall)

    # Compare against current threshold=130 filter's implicit decision
    logger.info("Comparison against current filter (mean >= 130 → classify as insect, drop):")
    current_filter_says_animal = test_df["mean"] < 130
    current_pred = current_filter_says_animal.astype(int)
    p, r, f, _ = precision_recall_fscore_support(y_test, current_pred, average="binary", zero_division=0)
    logger.info("  threshold=130 — precision=%.3f recall=%.3f f1=%.3f", p, r, f)

    metrics = {
        "n_train": len(train_rows),
        "n_val": len(val_rows),
        "n_test": len(test_rows),
        "roc_auc": auc,
        "avg_precision": ap,
        "class_balance_train": {
            "positive": int((y_train == 1).sum()),
            "negative": int((y_train == 0).sum()),
        },
    }

    if save_path:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        model.save_model(str(save_path))
        meta_path = save_path.with_suffix(".meta.json")
        meta_path.write_text(json.dumps({
            "features": ALL_FEATURES,
            "categorical_features": CATEGORICAL_FEATURES,
            "metrics": metrics,
        }, indent=2))
        logger.info("Saved model → %s", save_path)
        logger.info("Saved metadata → %s", meta_path)

    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--dsn", default=os.getenv("DATABASE_URL", ""),
                        help="Postgres DSN (default: $DATABASE_URL)")
    parser.add_argument("--camera", help="Train single-camera model (default: all cameras)")
    parser.add_argument("--output", default="models/animal_vs_fp.txt",
                        help="Where to save trained model (default: models/animal_vs_fp.txt)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-labels", type=int, default=100,
                        help="Refuse to train if too few labeled rows fetched")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.dsn:
        logger.error("No DSN provided (--dsn or DATABASE_URL env)")
        return 2

    logger.info("Fetching labeled alerts from %s...", args.dsn)
    rows = fetch_labeled_alerts(args.dsn, camera=args.camera)
    logger.info("Fetched %d labeled rows with brightness features", len(rows))
    if len(rows) < args.min_labels:
        logger.error("Only %d rows, need at least %d — refusing to train", len(rows), args.min_labels)
        return 3

    balance = Counter(r["y"] for r in rows)
    logger.info("Label balance — positive=%d negative=%d", balance[1], balance[0])
    if balance[1] < 50 or balance[0] < 50:
        logger.error("Not enough of each class to train (need ≥50 each)")
        return 4

    df = build_frame(rows)
    train_model(df, seed=args.seed, save_path=Path(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
