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
    monkeypatch.setattr(clf_mod, "_shadow_log", None)
    yield


def test_classifier_shadow_log_disabled_when_no_path(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_SHADOW_LOG_PATH", "")
    s = clf_mod.get_classifier_shadow_log()
    assert not s.enabled()
    s.record(track_id=1, prob=0.4)  # no-op, no crash


def test_classifier_shadow_log_writes_jsonl(monkeypatch, tmp_path):
    out = tmp_path / "shadow.jsonl"
    monkeypatch.setenv("CLASSIFIER_SHADOW_LOG_PATH", str(out))
    s = clf_mod.get_classifier_shadow_log()
    assert s.enabled()
    s.record(track_id=1, camera_id="yard", vlm_species="rat",
             prob=0.87, threshold=0.2, mode="shadow", action="alert")
    s.record(track_id=2, camera_id="yard", vlm_species="rat",
             prob=0.12, threshold=0.2, mode="shadow", action="alert")
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    row0 = json.loads(lines[0])
    assert row0["track_id"] == 1
    assert row0["prob"] == 0.87
    assert row0["action"] == "alert"
    assert "ts" in row0


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


def test_pre_vlm_drop_sink_boundary_override_saves_when_sample_zero(monkeypatch, tmp_path):
    """Boundary band forces save even when uniform sample rate is 0 —
    the whole point of boundary sampling is that near-threshold rows
    (the high-value positive-hunt zone) don't get lost to random sampling."""
    out = tmp_path / "drops.jsonl"
    monkeypatch.setenv("PRE_VLM_DROP_LOG_PATH", str(out))
    monkeypatch.setenv("PRE_VLM_DROP_LOG_SAMPLE", "0.0")
    monkeypatch.setenv("PRE_VLM_DROP_BOUNDARY_MIN", "110")
    monkeypatch.setenv("PRE_VLM_DROP_BOUNDARY_MAX", "145")
    s = clf_mod.get_pre_vlm_drop_sink()
    assert s.enabled()
    s.record(camera_id="yard", track_id=1, mean=125.0)   # in-band, saved
    s.record(camera_id="yard", track_id=2, mean=200.0)   # out-of-band, sample=0
    s.record(camera_id="yard", track_id=3, mean=110.0)   # boundary inclusive
    s.record(camera_id="yard", track_id=4, mean=145.0)   # boundary inclusive
    s.record(camera_id="yard", track_id=5, mean=50.0)    # out-of-band, dropped
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 3
    track_ids = sorted(json.loads(ln)["track_id"] for ln in lines)
    assert track_ids == [1, 3, 4]


def test_pre_vlm_drop_sink_dumps_crop(monkeypatch, tmp_path):
    """Crop bytes get written to <crop_dir>/<camera>/<date>/<ts>_track<id>.jpg
    and the JSONL row picks up the relative `snapshot` path."""
    out = tmp_path / "drops.jsonl"
    crop_dir = tmp_path / "crops"
    monkeypatch.setenv("PRE_VLM_DROP_LOG_PATH", str(out))
    monkeypatch.setenv("PRE_VLM_DROP_LOG_SAMPLE", "1.0")
    monkeypatch.setenv("PRE_VLM_DROP_CROP_DIR", str(crop_dir))
    s = clf_mod.get_pre_vlm_drop_sink()
    assert s.enabled()
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # valid JPEG-ish header
    s.record(crop_jpeg=fake_jpeg, camera_id="yard", track_id=42, mean=125.0)
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert "snapshot" in row
    assert row["snapshot"].startswith("yard/")
    assert row["snapshot"].endswith("_track42.jpg")
    dumped = crop_dir / row["snapshot"]
    assert dumped.exists()
    assert dumped.read_bytes() == fake_jpeg


def test_pre_vlm_drop_sink_dumps_wide_crop_alongside_tight(monkeypatch, tmp_path):
    """When wide_crop_jpeg is passed, saves it with `_wide` suffix and
    surfaces the relative path in the JSONL row as `snapshot_wide`.
    Older callers that don't pass wide_crop_jpeg keep working with
    just the tight crop (backward compat)."""
    out = tmp_path / "drops.jsonl"
    crop_dir = tmp_path / "crops"
    monkeypatch.setenv("PRE_VLM_DROP_LOG_PATH", str(out))
    monkeypatch.setenv("PRE_VLM_DROP_LOG_SAMPLE", "1.0")
    monkeypatch.setenv("PRE_VLM_DROP_CROP_DIR", str(crop_dir))
    s = clf_mod.get_pre_vlm_drop_sink()
    tight = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    wide = b"\xff\xd8\xff\xe0" + b"\x11" * 200
    s.record(
        crop_jpeg=tight,
        wide_crop_jpeg=wide,
        camera_id="backyard",
        track_id=99,
        mean=130.0,
    )
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["snapshot"].endswith("_track99.jpg")
    assert row["snapshot_wide"].endswith("_track99_wide.jpg")
    assert (crop_dir / row["snapshot"]).read_bytes() == tight
    assert (crop_dir / row["snapshot_wide"]).read_bytes() == wide
