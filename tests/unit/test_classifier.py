"""Unit tests for src.classifier — fail-open behavior + sink sampling.

Does NOT exercise a real LightGBM model (the training script owns that
path). These are contract tests for the graceful-degradation surface —
what happens when the model file is missing, the metadata is missing,
or the feature dict is incomplete.
"""

from __future__ import annotations

import json

import pytest

import src.classifier as clf_mod


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch):
    """Each test gets a fresh singleton — env vars we set in one test
    shouldn't bleed into the next through the module-level cache."""
    monkeypatch.setattr(clf_mod, "_singleton", None)
    monkeypatch.setattr(clf_mod, "_drop_sink", None)
    yield


def test_missing_model_file_is_no_op(monkeypatch, tmp_path):
    monkeypatch.setenv("CLASSIFIER_MODEL_PATH", str(tmp_path / "nope.txt"))
    c = clf_mod.get_classifier()
    assert not c.enabled()
    assert c.predict({"mean": 45.0}) is None


def test_missing_meta_disables_even_if_model_loads(monkeypatch, tmp_path):
    # Real model file present, meta absent → module refuses to run
    # rather than passing NaN into the model.
    pytest.importorskip("lightgbm")
    import lightgbm as lgb
    import numpy as np

    # Train a trivial model so we get a valid .txt file to load
    X = np.random.rand(50, 3)
    y = (X[:, 0] > 0.5).astype(int)
    ds = lgb.Dataset(X, label=y)
    model = lgb.train({"objective": "binary", "verbosity": -1}, ds, num_boost_round=5)
    model_path = tmp_path / "m.txt"
    model.save_model(str(model_path))
    # Deliberately do NOT write the meta sidecar.
    monkeypatch.setenv("CLASSIFIER_MODEL_PATH", str(model_path))
    c = clf_mod.get_classifier()
    assert not c.enabled()


def test_pre_vlm_drop_sink_disabled_when_no_path(monkeypatch):
    monkeypatch.setenv("PRE_VLM_DROP_LOG_PATH", "")
    s = clf_mod.get_pre_vlm_drop_sink()
    assert not s.enabled()
    s.record(camera_id="yard", mean=100)  # no-op, no crash


def test_pre_vlm_drop_sink_writes_jsonl(monkeypatch, tmp_path):
    out = tmp_path / "drops.jsonl"
    monkeypatch.setenv("PRE_VLM_DROP_LOG_PATH", str(out))
    monkeypatch.setenv("PRE_VLM_DROP_LOG_SAMPLE", "1.0")
    s = clf_mod.get_pre_vlm_drop_sink()
    assert s.enabled()
    s.record(camera_id="yard", bbox=[1, 2, 3, 4], mean=42.0)
    s.record(camera_id="backyard", bbox=[5, 6, 7, 8], mean=99.0)
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    row0 = json.loads(lines[0])
    assert row0["camera_id"] == "yard"
    assert row0["bbox"] == [1, 2, 3, 4]
    assert "ts" in row0
