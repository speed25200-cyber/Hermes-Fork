"""A bundle scores only its verified payloads, including the evaluated configuration."""

import hashlib
import json
from dataclasses import replace

import lightgbm as lgb
import numpy as np
import pytest

from hermes.config import LinearConfig, load_config
from hermes.models.bundle import ModelBundle
from hermes.models.gbm import GBMModel
from hermes.models.linear import RidgeModel


@pytest.fixture
def bundle():
    cfg = load_config(None)
    gbm = GBMModel(cfg.model.gbm)
    X = np.arange(24, dtype=np.float32).reshape(-1, 1)
    gbm.boosters = [
        lgb.train(
            {"objective": "regression", "min_data_in_leaf": 2, "num_threads": 1, "verbosity": -1},
            lgb.Dataset(X, label=np.sin(X[:, 0])),
            num_boost_round=3,
        )
    ]
    ridge = RidgeModel(LinearConfig())
    ridge.mu, ridge.sd, ridge.coef = np.array([1.0]), np.array([2.0]), np.array([0.5])
    return ModelBundle(
        config=cfg,
        feature_names=["signal"],
        gbm=gbm,
        ridge=ridge,
        weights={"gbm": 0.6, "ridge": 0.4},
        prior_ic=0.04,
        market=ridge,
        market_feature_names=["market_signal"],
        market_prior_ic=0.02,
        meta={"promoted": True, "evaluation": {"passed": True}},
    )


def _metadata(directory):
    return json.loads((directory / "bundle.json").read_text())


def _write_metadata(directory, meta):
    (directory / "bundle.json").write_text(json.dumps(meta))


def test_roundtrip_preserves_scores_and_hashes_configuration(bundle, tmp_path):
    bundle.save(tmp_path)
    meta = _metadata(tmp_path)
    assert meta["format_version"] == 2
    assert set(meta["files"]) == {"gbm_0.txt", "ridge.npz", "market.npz", "config.json"}
    assert meta["files"]["config.json"] == hashlib.sha256((tmp_path / "config.json").read_bytes()).hexdigest()
    restored = ModelBundle.load(tmp_path)
    X = np.arange(12, dtype=np.float32).reshape(-1, 1)
    groups = np.repeat(np.arange(3), 4)
    np.testing.assert_allclose(restored.score(X, groups), bundle.score(X, groups))
    assert restored.market_score(X[0]) == bundle.market_score(X[0])
    assert restored.config == bundle.config
    assert restored.feature_names == bundle.feature_names
    assert restored.market_feature_names == bundle.market_feature_names
    assert restored.weights == bundle.weights
    assert restored.prior_ic == bundle.prior_ic
    assert restored.market_prior_ic == bundle.market_prior_ic
    assert restored.promoted


@pytest.mark.parametrize("name", ["config.json", "gbm_0.txt", "ridge.npz", "market.npz"])
def test_modified_payload_is_rejected_before_deserialization(bundle, tmp_path, name, monkeypatch):
    bundle.save(tmp_path)
    if name == "config.json":
        cfg = json.loads((tmp_path / name).read_text())
        cfg["portfolio"]["cost_aversion"] += 1.0
        (tmp_path / name).write_text(json.dumps(cfg))
    else:
        (tmp_path / name).write_bytes(b"modified model")

    def unexpected_deserialization(**kwargs):
        raise AssertionError("models must not be deserialized before all payloads have passed integrity checks")

    monkeypatch.setattr("hermes.models.bundle.lgb.Booster", unexpected_deserialization)
    with pytest.raises(ValueError, match=f"{name} does not match its recorded hash"):
        ModelBundle.load(tmp_path)


def test_save_into_reused_directory_ignores_obsolete_models(bundle, tmp_path):
    bundle.save(tmp_path)
    replacement = replace(bundle, gbm=None, market=None, weights={"ridge": 1.0}, market_feature_names=[])
    replacement.save(tmp_path)
    # Old files may remain for inspection, but only the new manifest controls scoring.
    assert (tmp_path / "gbm_0.txt").exists() and (tmp_path / "market.npz").exists()
    (tmp_path / "gbm_99.txt").write_text("not a model")
    restored = ModelBundle.load(tmp_path)
    assert restored.gbm is None and restored.market is None and restored.ridge is not None
    X = np.arange(12, dtype=np.float32).reshape(-1, 1)
    groups = np.repeat(np.arange(3), 4)
    np.testing.assert_allclose(restored.score(X, groups), replacement.score(X, groups))


def test_unlisted_ridge_is_not_loaded(bundle, tmp_path):
    bundle.save(tmp_path)
    meta = _metadata(tmp_path)
    del meta["files"]["ridge.npz"]
    meta["weights"] = {"gbm": 1.0}
    _write_metadata(tmp_path, meta)
    assert ModelBundle.load(tmp_path).ridge is None


@pytest.mark.parametrize("model, name", [("gbm", "gbm_0.txt"), ("ridge", "ridge.npz")])
def test_weighted_model_missing_from_manifest_is_rejected(bundle, tmp_path, model, name):
    bundle.save(tmp_path)
    meta = _metadata(tmp_path)
    del meta["files"][name]
    _write_metadata(tmp_path, meta)
    with pytest.raises(ValueError, match=f"positive weight to {model}"):
        ModelBundle.load(tmp_path)


def test_freeform_metadata_cannot_override_scoring_or_integrity_fields(bundle, tmp_path):
    bundle.meta.update(
        format_version=1,
        files={},
        feature_names=["wrong"],
        market_feature_names=["wrong"],
        weights={"gbm": 1.0},
        prior_ic=100.0,
        market_prior_ic=100.0,
    )
    bundle.save(tmp_path)
    restored = ModelBundle.load(tmp_path)
    assert _metadata(tmp_path)["format_version"] == 2
    assert "config.json" in _metadata(tmp_path)["files"]
    assert restored.feature_names == bundle.feature_names
    assert restored.market_feature_names == bundle.market_feature_names
    assert restored.weights == bundle.weights
    assert restored.prior_ic == bundle.prior_ic
    assert restored.market_prior_ic == bundle.market_prior_ic
    assert restored.meta["evaluation"] == {"passed": True}


@pytest.mark.parametrize("files", [None, [], "", {"../ridge.npz": "0" * 64}, {"ridge.npz": "bad"}])
def test_legacy_manifest_is_required_and_validated(bundle, tmp_path, files):
    bundle.save(tmp_path)
    meta = _metadata(tmp_path)
    del meta["format_version"]
    meta["files"] = files
    _write_metadata(tmp_path, meta)
    with pytest.raises(ValueError, match=r"manifest|file name|SHA-256"):
        ModelBundle.load(tmp_path)


@pytest.mark.parametrize("version", [None, True, "2", 3])
def test_unknown_or_invalid_format_version_is_rejected(bundle, tmp_path, version):
    bundle.save(tmp_path)
    meta = _metadata(tmp_path)
    meta["format_version"] = version
    _write_metadata(tmp_path, meta)
    with pytest.raises(ValueError, match="unsupported bundle format"):
        ModelBundle.load(tmp_path)


def test_format_2_requires_configuration_hash(bundle, tmp_path):
    bundle.save(tmp_path)
    meta = _metadata(tmp_path)
    del meta["files"]["config.json"]
    _write_metadata(tmp_path, meta)
    with pytest.raises(ValueError, match=r"requires config\.json"):
        ModelBundle.load(tmp_path)


def test_missing_declared_file_is_rejected(bundle, tmp_path):
    bundle.save(tmp_path)
    (tmp_path / "ridge.npz").unlink()
    with pytest.raises(ValueError, match=r"ridge\.npz cannot be read"):
        ModelBundle.load(tmp_path)


def test_nonconsecutive_booster_manifest_is_rejected(bundle, tmp_path):
    bundle.save(tmp_path)
    (tmp_path / "gbm_0.txt").rename(tmp_path / "gbm_1.txt")
    meta = _metadata(tmp_path)
    meta["files"]["gbm_1.txt"] = meta["files"].pop("gbm_0.txt")
    _write_metadata(tmp_path, meta)
    with pytest.raises(ValueError, match="consecutive indices"):
        ModelBundle.load(tmp_path)


def test_legacy_bundles_warn_and_can_be_explicitly_migrated(bundle, tmp_path):
    source = bundle.save(tmp_path / "legacy")
    meta = _metadata(source)
    del meta["format_version"]
    del meta["files"]["config.json"]
    _write_metadata(source, meta)
    # Keep deployed legacy models readable, without claiming their configuration was protected.
    with pytest.warns(RuntimeWarning, match="Legacy bundle has no config.json integrity hash"):
        restored = ModelBundle.load(source)
    assert restored.promoted
    migrated = restored.save(tmp_path / "migrated")
    assert _metadata(migrated)["format_version"] == 2
    assert "config.json" in _metadata(migrated)["files"]
    assert ModelBundle.load(migrated).promoted
    (migrated / "config.json").write_text("{}")
    with pytest.raises(ValueError, match=r"config\.json does not match"):
        ModelBundle.load(migrated)
