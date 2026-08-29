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
    """Sampled JSONL sink for pre-VLM drops. Fail-open on write errors.

    Sampling strategy is two-tier:
      1. Uniform sample at `sample_rate` for typical drops.
      2. Boundary override: rows whose `mean` brightness falls in the
         [boundary_min, boundary_max] band are ALWAYS saved regardless
         of sample rate. This is the "positive-hunt" band — drops near
         the NIGHT_INSECT_BRIGHTNESS_MIN filter threshold are the ones
         most likely to be legit animals mis-flagged as insects, so
         they're the highest-value data for a future pre-VLM classifier.

    Optional crop dump: when `crop_dir` is set AND record() is called
    with a `crop_jpeg` bytes payload, the JPEG is written to disk under
    <crop_dir>/<camera_id>/<YYYY-MM-DD>/<ts>_track<track_id>.jpg and
    the JSONL row gets a `snapshot` field with the relative path. This
    is what makes hand-labeling possible: numbers alone (mean/max/AR)
    can't tell a moth from a rat.
    """

    def __init__(
        self,
        path: str,
        sample_rate: float,
        boundary_min: float | None = None,
        boundary_max: float | None = None,
        crop_dir: str | None = None,
    ):
        self._path = Path(path) if path else None
        self._sample = max(0.0, min(1.0, sample_rate))
        self._boundary_min = boundary_min
        self._boundary_max = boundary_max
        self._crop_dir = Path(crop_dir) if crop_dir else None
        self._fh = None
        self._warned = False
        self._crop_warned = False
        if self._path and (self._sample > 0 or self._in_boundary_mode()):
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", buffering=1, encoding="utf-8")
                boundary_desc = (
                    f"boundary=[{self._boundary_min:.0f},{self._boundary_max:.0f}]"
                    if self._in_boundary_mode() else "boundary=off"
                )
                crop_desc = f"crops={self._crop_dir}" if self._crop_dir else "crops=off"
                logger.info(
                    "Pre-VLM drop shadow log open at %s (sample=%.2f %s %s)",
                    self._path, self._sample, boundary_desc, crop_desc,
                )
            except Exception:
                logger.exception("Failed to open pre-VLM drop shadow log %s", self._path)
        if self._crop_dir:
            try:
                self._crop_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                logger.exception("Failed to create pre-VLM drop crop dir %s", self._crop_dir)
                self._crop_dir = None

    def _in_boundary_mode(self) -> bool:
        return (
            self._boundary_min is not None
            and self._boundary_max is not None
            and self._boundary_min < self._boundary_max
        )

    def enabled(self) -> bool:
        return self._fh is not None

    def _should_save(self, mean: float | None) -> bool:
        # Boundary override: near-threshold rows always saved
        if self._in_boundary_mode() and mean is not None:
            if self._boundary_min <= mean <= self._boundary_max:
                return True
        if self._sample <= 0.0:
            return False
        if self._sample >= 1.0:
            return True
        return random.random() < self._sample

    def _save_crop_file(
        self, crop_jpeg: bytes, camera_id: str, ts: float, track_id: Any,
        suffix: str = "",
    ) -> str | None:
        """Write one JPEG to <crop_dir>/<camera>/<date>/<ts>_track<id>[<suffix>].jpg.
        Returns the relative path from crop_dir, or None on failure.
        `suffix` is empty for the tight crop and "_wide" for the wider-
        context crop that shows the operator enough surrounding pixels
        to actually distinguish moth-vs-rodent (the labeling-UX gap
        that motivated this)."""
        if self._crop_dir is None or not crop_jpeg:
            return None
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            cam_dir = self._crop_dir / str(camera_id) / day
            cam_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{int(ts)}_track{track_id}{suffix}.jpg"
            fpath = cam_dir / fname
            fpath.write_bytes(crop_jpeg)
            return str(fpath.relative_to(self._crop_dir)).replace("\\", "/")
        except Exception:
            if not self._crop_warned:
                logger.exception("Pre-VLM drop crop save failed (further errors suppressed)")
                self._crop_warned = True
            return None

    def record(
        self,
        crop_jpeg: bytes | None = None,
        wide_crop_jpeg: bytes | None = None,
        **fields: Any,
    ) -> None:
        if self._fh is None:
            return
        mean = fields.get("mean")
        if not self._should_save(mean if isinstance(mean, (int, float)) else None):
            return
        ts = time.time()
        row: dict[str, Any] = {"ts": ts, **fields}
        camera_id = str(fields.get("camera_id", "unknown"))
        track_id = fields.get("track_id", "unknown")
        if crop_jpeg is not None:
            snap = self._save_crop_file(crop_jpeg, camera_id, ts, track_id, suffix="")
            if snap:
                row["snapshot"] = snap
        if wide_crop_jpeg is not None:
            wide_snap = self._save_crop_file(
                wide_crop_jpeg, camera_id, ts, track_id, suffix="_wide",
            )
            if wide_snap:
                row["snapshot_wide"] = wide_snap
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
        # Empty string = boundary sampling off; parse only when both set.
        b_min_raw = os.getenv("PRE_VLM_DROP_BOUNDARY_MIN", "")
        b_max_raw = os.getenv("PRE_VLM_DROP_BOUNDARY_MAX", "")
        b_min = float(b_min_raw) if b_min_raw else None
        b_max = float(b_max_raw) if b_max_raw else None
        crop_dir = os.getenv("PRE_VLM_DROP_CROP_DIR", "") or None
        _drop_sink = PreVlmDropSink(
            path, sample,
            boundary_min=b_min, boundary_max=b_max,
            crop_dir=crop_dir,
        )
    return _drop_sink


# ─────────────────────────────────────────────────────────────────────
# Classifier shadow log — persistent JSONL of every post-VLM classifier
# prediction. Purpose: measure agreement-rate against user-labeled
# alerts weeks after the fact, without depending on ephemeral container
# stdout/stderr (which every `docker compose restart` wipes).
#
# Same restart-safe pattern as PreVlmDropSink — bind-mounted file, one
# row per prediction, fail-open on write errors. Not sampled — the goal
# is a complete audit trail, and prediction volume tracks alert volume
# (bounded, not motion-detection-bounded).
# ─────────────────────────────────────────────────────────────────────


class ClassifierShadowLog:
    """Persistent JSONL sink for every post-VLM classifier prediction.
    Fail-open on write errors."""

    def __init__(self, path: str):
        self._path = Path(path) if path else None
        self._fh = None
        self._warned = False
        if self._path:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", buffering=1, encoding="utf-8")
                logger.info("Classifier shadow log open at %s", self._path)
            except Exception:
                logger.exception("Failed to open classifier shadow log %s", self._path)

    def enabled(self) -> bool:
        return self._fh is not None

    def record(self, **fields: Any) -> None:
        if self._fh is None:
            return
        row = {"ts": time.time(), **fields}
        try:
            self._fh.write(json.dumps(row, default=str) + "\n")
        except Exception:
            if not self._warned:
                logger.exception("Classifier shadow log write failed (further errors suppressed)")
                self._warned = True


_shadow_log: ClassifierShadowLog | None = None


def get_classifier_shadow_log() -> ClassifierShadowLog:
    global _shadow_log
    if _shadow_log is None:
        path = os.getenv("CLASSIFIER_SHADOW_LOG_PATH", "")
        _shadow_log = ClassifierShadowLog(path)
    return _shadow_log


# ─────────────────────────────────────────────────────────────────────
# Pre-VLM filter — LightGBM re-scorer over the same brightness features
# the hand-tuned NIGHT_INSECT_BRIGHTNESS_MIN gate uses. Purpose: recover
# the real animals the insect pre-filter is killing (per the drops-
# labeling loop). Shadow-mode only for now: pipeline logs
# P(real_animal | features) alongside every drop but the current
# threshold gate is unchanged. After a soak we'll compare the
# classifier's would-suppress vs the operator's labels to pick an
# operating threshold.
#
# Fail-open (same shape as AnimalClassifier): missing model file, meta,
# or lightgbm import → no-op. The filter is a decision-augmenting layer,
# not a hard dependency of detection.
# ─────────────────────────────────────────────────────────────────────


class PreVlmFilter:
    """LightGBM pre-VLM filter — reuses AnimalClassifier's contract with
    a distinct feature set and its own model file. Fail-open on any
    load error. Same pattern as [[AnimalClassifier]] — separate class
    only so future changes to one contract don't perturb the other."""

    def __init__(self, model_path: str, meta_path: str | None = None):
        self._model = None
        self._features: list[str] = []
        self._categorical: list[str] = []
        self._warned_missing_feature: set[str] = set()
        self._load(model_path, meta_path)

    def _load(self, model_path: str, meta_path: str | None) -> None:
        p = Path(model_path)
        if not p.exists():
            logger.warning(
                "Pre-VLM filter model not found at %s — running in no-op mode",
                model_path,
            )
            return
        try:
            import lightgbm as lgb
        except ImportError:
            logger.warning("lightgbm not installed — pre-VLM filter in no-op mode")
            return
        try:
            self._model = lgb.Booster(model_file=str(p))
        except Exception:
            logger.exception(
                "Failed loading pre-VLM filter model from %s — no-op mode",
                model_path,
            )
            return

        meta_p = Path(meta_path) if meta_path else p.with_suffix(".meta.json")
        if not meta_p.exists():
            logger.warning("Pre-VLM filter metadata not found at %s — no-op mode", meta_p)
            self._model = None
            return
        try:
            meta = json.loads(meta_p.read_text())
            self._features = list(meta["features"])
            self._categorical = list(meta.get("categorical_features", []))
            logger.info(
                "Pre-VLM filter loaded from %s (features=%d, categorical=%d)",
                model_path, len(self._features), len(self._categorical),
            )
        except Exception:
            logger.exception(
                "Failed parsing pre-VLM filter metadata %s — no-op mode", meta_p,
            )
            self._model = None

    def enabled(self) -> bool:
        return self._model is not None

    def predict(self, features: dict[str, Any]) -> float | None:
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
                    logger.warning("Pre-VLM filter feature %r missing at predict time", name)
                    self._warned_missing_feature.add(name)
                return None
            row[name] = features[name]
        df = pd.DataFrame([row], columns=self._features)
        for col in self._categorical:
            df[col] = df[col].astype("category")
        try:
            return float(self._model.predict(df)[0])
        except Exception:
            logger.exception("Pre-VLM filter predict failed")
            return None


_pre_vlm_filter: PreVlmFilter | None = None


def get_pre_vlm_filter() -> PreVlmFilter:
    global _pre_vlm_filter
    if _pre_vlm_filter is None:
        model_path = os.getenv("PRE_VLM_FILTER_MODEL_PATH", "/app/models-host/pre_vlm_filter.txt")
        meta_path = os.getenv("PRE_VLM_FILTER_META_PATH", "")
        _pre_vlm_filter = PreVlmFilter(model_path, meta_path or None)
    return _pre_vlm_filter


class PreVlmFilterShadowLog:
    """Persistent JSONL sink for every pre-VLM filter prediction.
    Unsampled — one row per drop — so aggregate would-suppress volume
    is measurable without extrapolating from a sample rate. Rows are
    small (no crops, no bbox), so I/O cost is bounded by drop volume.
    Same fail-open shape as [[ClassifierShadowLog]]."""

    def __init__(self, path: str):
        self._path = Path(path) if path else None
        self._fh = None
        self._warned = False
        if self._path:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = self._path.open("a", buffering=1, encoding="utf-8")
                logger.info("Pre-VLM filter shadow log open at %s", self._path)
            except Exception:
                logger.exception("Failed to open pre-VLM filter shadow log %s", self._path)

    def enabled(self) -> bool:
        return self._fh is not None

    def record(self, **fields: Any) -> None:
        if self._fh is None:
            return
        row = {"ts": time.time(), **fields}
        try:
            self._fh.write(json.dumps(row, default=str) + "\n")
        except Exception:
            if not self._warned:
                logger.exception(
                    "Pre-VLM filter shadow log write failed (further errors suppressed)",
                )
                self._warned = True


_pre_vlm_filter_shadow_log: PreVlmFilterShadowLog | None = None


def get_pre_vlm_filter_shadow_log() -> PreVlmFilterShadowLog:
    global _pre_vlm_filter_shadow_log
    if _pre_vlm_filter_shadow_log is None:
        path = os.getenv("PRE_VLM_FILTER_SHADOW_LOG_PATH", "")
        _pre_vlm_filter_shadow_log = PreVlmFilterShadowLog(path)
    return _pre_vlm_filter_shadow_log
