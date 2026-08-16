"""LightGBM animal-vs-FP classifier — post-VLM re-scorer.

Loads a model trained by scripts/train_animal_vs_fp.py and returns
P(real_animal | bbox_features + vlm_output) for each VLM-classified
detection. Used as a gate at the alert-emit trust boundary: any
alert whose classifier prob falls below CLASSIFIER_THRESHOLD gets
suppressed (in active mode) or logged for shadow comparison.

Design notes:
  • Lazy load — training deps (lightgbm) NOT in detector image.
    If the model file is missing or lightgbm isn't installed, this
    module operates in fail-open no-op mode so a bad env doesn't
    take detection offline. Fail-fast at trust boundary + graceful
    degradation on the runtime path.
  • Feature contract is a JSON metadata sidecar next to the model
    file (models/animal_vs_fp.meta.json). If the runtime-computed
    feature dict doesn't match, we warn once and disable rather
    than passing NaN into the model.
  • Predictions are stateless — no per-track memory, no cache.
    Cheap enough (~microseconds per call) that batching adds
    complexity without payoff at our detection rate.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AnimalClassifier:
    """LightGBM post-VLM re-scorer. Fail-open on any load error."""

    def __init__(self, model_path: str, meta_path: str | None = None):
        self._model = None
        self._features: list[str] = []
        self._categorical: list[str] = []
        self._warned_missing_feature: set[str] = set()
        self._load(model_path, meta_path)

    def _load(self, model_path: str, meta_path: str | None) -> None:
        p = Path(model_path)
        if not p.exists():
            logger.warning("Classifier model not found at %s — running in no-op mode", model_path)
            return
        try:
            import lightgbm as lgb
        except ImportError:
            logger.warning("lightgbm not installed — classifier in no-op mode")
            return
        try:
            self._model = lgb.Booster(model_file=str(p))
        except Exception:
            logger.exception("Failed loading classifier model from %s — no-op mode", model_path)
            return

        meta_p = Path(meta_path) if meta_path else p.with_suffix(".meta.json")
        if not meta_p.exists():
            logger.warning("Classifier metadata not found at %s — no-op mode", meta_p)
            self._model = None
            return
        try:
            meta = json.loads(meta_p.read_text())
            self._features = list(meta["features"])
            self._categorical = list(meta.get("categorical_features", []))
            logger.info(
                "Classifier loaded from %s (features=%d, categorical=%d)",
                model_path, len(self._features), len(self._categorical),
            )
        except Exception:
            logger.exception("Failed parsing classifier metadata %s — no-op mode", meta_p)
            self._model = None

    def enabled(self) -> bool:
        return self._model is not None

    def predict(self, features: dict[str, Any]) -> float | None:
        """Return P(real_animal | features) in [0, 1], or None if disabled
        or if the feature dict is missing required keys."""
        if self._model is None:
            return None
        try:
            import pandas as pd
        except ImportError:
            return None
        row: dict[str, Any] = {}
        for name in self._features:
            if name not in features:
                if name not in self._warned_missing_feature:
                    logger.warning("Classifier feature %r missing at predict time", name)
                    self._warned_missing_feature.add(name)
                return None
            row[name] = features[name]
        df = pd.DataFrame([row], columns=self._features)
        for col in self._categorical:
            df[col] = df[col].astype("category")
        try:
            return float(self._model.predict(df)[0])
        except Exception:
            logger.exception("Classifier predict failed")
            return None


_singleton: AnimalClassifier | None = None


def get_classifier() -> AnimalClassifier:
    """Process-wide singleton so we don't reload the model per detection.

    Default path targets the container's bind-mounted /app/models-host/
    (see docker-compose.yml). For local runs of the training/eval
    scripts on the host, override with CLASSIFIER_MODEL_PATH=models/…
    or --output on the training script."""
    global _singleton
    if _singleton is None:
        model_path = os.getenv("CLASSIFIER_MODEL_PATH", "/app/models-host/animal_vs_fp.txt")
        meta_path = os.getenv("CLASSIFIER_META_PATH", "")
        _singleton = AnimalClassifier(model_path, meta_path or None)
    return _singleton


# ─────────────────────────────────────────────────────────────────────
# Pre-VLM drop shadow log — captures the population currently invisible
# to the labeling UI (rows the insect pre-filter drops before VLM runs).
# Purpose: build an unbiased training set for a future pre-VLM classifier
# by giving the operator a sampled JSONL of what's being dropped, so a
# batch can be reviewed and labeled offline.
#
# JSONL sink over a Python file, sampled + rate-limited to avoid I/O
# storms on high-motion cameras. One line per sampled drop; no schema
# guarantees beyond "each line is a valid JSON object".
# ─────────────────────────────────────────────────────────────────────


class PreVlmDropSink:
    """Sampled JSONL sink for pre-VLM drops. Fail-open on write errors."""

    def __init__(self, path: str, sample_rate: float):
        self._path = Path(path) if path else None
        self._sample = max(0.0, min(1.0, sample_rate))
        self._fh = None
        self._warned = False
        if self._path and self._sample > 0:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", buffering=1, encoding="utf-8")
                logger.info(
                    "Pre-VLM drop shadow log open at %s (sample=%.2f)",
                    self._path, self._sample,
                )
            except Exception:
                logger.exception("Failed to open pre-VLM drop shadow log %s", self._path)

    def enabled(self) -> bool:
        return self._fh is not None

    def record(self, **fields: Any) -> None:
        if self._fh is None:
            return
        if self._sample < 1.0 and random.random() >= self._sample:
            return
        row = {"ts": time.time(), **fields}
        try:
            self._fh.write(json.dumps(row, default=str) + "\n")
        except Exception:
            if not self._warned:
                logger.exception("Pre-VLM drop shadow log write failed (further errors suppressed)")
                self._warned = True


_drop_sink: PreVlmDropSink | None = None


def get_pre_vlm_drop_sink() -> PreVlmDropSink:
    global _drop_sink
    if _drop_sink is None:
        path = os.getenv("PRE_VLM_DROP_LOG_PATH", "")
        sample = float(os.getenv("PRE_VLM_DROP_LOG_SAMPLE", "0.05"))
        _drop_sink = PreVlmDropSink(path, sample)
    return _drop_sink
