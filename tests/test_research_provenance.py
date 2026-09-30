"""Régressions : un cache de même taille ne prouve ni les données ni le code utilisés."""

import json

import numpy as np
import pytest

from hermes.data.synthetic import make_synthetic_panel
from hermes.research.dataset import build_dataset
from hermes.research.provenance import (
    TRAINING_PATHS,
    array_sha256,
    provenance_json,
    same_training_inputs,
    training_provenance,
    training_sources,
)
from hermes.research.run import training_hash


@pytest.fixture
def dataset(cfg_small):
    panel = make_synthetic_panel(n_assets=12, n_bars=96 * 25, bar="15m", staggered_listings=False)
    return build_dataset(panel, cfg_small)


def marker(ds, cfg):
    return provenance_json(training_provenance(ds, training_hash(cfg)))


def test_identical_inputs_resume_but_evaluation_cost_changes_do_not_retrain(dataset, cfg_small):
    original = marker(dataset, cfg_small)
    cost_change = cfg_small.model_copy(update={"costs": cfg_small.costs.model_copy(update={"taker_fee": 0.0009})})
    assert same_training_inputs(original, marker(dataset, cost_change))


@pytest.mark.parametrize("field", ["close", "funding_rate", "quote_volume"])
def test_market_revision_with_unchanged_dimensions_invalidates_cache(dataset, cfg_small, field):
    original = marker(dataset, cfg_small)
    dataset.panel[field].iloc[0, 0] = 17.123
    assert not same_training_inputs(original, marker(dataset, cfg_small))


def test_same_contract_count_does_not_allow_a_different_symbol(dataset, cfg_small):
    original = marker(dataset, cfg_small)
    previous = dataset.panel.symbols[-1]
    for frame in dataset.panel.fields.values():
        frame.rename(columns={previous: "NEWUSDT"}, inplace=True)
    assert not same_training_inputs(original, marker(dataset, cfg_small))


@pytest.mark.parametrize("array", ["X", "y", "y_raw", "market_X", "market_y"])
def test_transformed_training_values_are_part_of_provenance(dataset, cfg_small, array):
    original = marker(dataset, cfg_small)
    getattr(dataset, array).flat[0] = 13.0
    assert not same_training_inputs(original, marker(dataset, cfg_small))


def test_changed_universe_invalidates_cache(dataset, cfg_small):
    original = marker(dataset, cfg_small)
    dataset.mask.iloc[-1, 0] = not dataset.mask.iloc[-1, 0]
    assert not same_training_inputs(original, marker(dataset, cfg_small))


def test_dependency_or_source_revision_invalidates_cache(dataset, cfg_small, monkeypatch):
    original = marker(dataset, cfg_small)
    monkeypatch.setattr("hermes.research.provenance.numerical_versions", lambda: {"numpy": "future"})
    different_version = marker(dataset, cfg_small)
    assert not same_training_inputs(original, different_version)
    monkeypatch.setattr("hermes.research.provenance.training_sources", lambda: {"models/base.py": "changed"})
    assert not same_training_inputs(different_version, marker(dataset, cfg_small))


def test_sources_include_uncommitted_edits_and_missing_source_fails_closed(tmp_path):
    for name in TRAINING_PATHS:
        path = tmp_path / name
        if path.suffix != ".py":
            path = path / "example.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n")
    before = training_sources(tmp_path)
    (tmp_path / "config.py").write_text("value = 2\n")
    assert training_sources(tmp_path) != before
    (tmp_path / "config.py").unlink()
    with pytest.raises(ValueError, match=r"source.*absente"):
        training_sources(tmp_path)


def test_truncated_panel_is_not_assumed_to_have_identical_training_labels(dataset, cfg_small):
    original = marker(dataset, cfg_small)
    dataset.panel = dataset.panel.iloc(slice(None, -1))
    assert not same_training_inputs(original, marker(dataset, cfg_small))


def test_corrupted_or_incomplete_provenance_fails_closed(dataset, cfg_small):
    original = marker(dataset, cfg_small)
    edited = json.loads(original)
    edited["training_config_hash"] = "changed"
    for bad in ("abc:2022-01-01:2023-01-01:12", "{}", "null", "[]", "invalid", json.dumps(edited)):
        assert not same_training_inputs(bad, original)


def test_array_digest_uses_values_shape_dtype_and_handles_noncontiguous_arrays():
    values = np.arange(30, dtype=np.float64).reshape(10, 3)
    assert array_sha256(values) == array_sha256(np.asfortranarray(values))
    assert array_sha256(values) != array_sha256(values.astype(np.float32))
    assert array_sha256(values) != array_sha256(values.reshape(5, 6))
    with pytest.raises(ValueError, match="objets Python"):
        array_sha256(np.array([object()]))
