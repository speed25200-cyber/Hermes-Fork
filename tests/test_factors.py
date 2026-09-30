"""Fixed challengers must replay through the same persisted model used in serving."""

import numpy as np
import pandas as pd
import pytest

from hermes.config import load_config
from hermes.features.library import STORAGE_DTYPE, FeatureSet
from hermes.models.bundle import ModelBundle
from hermes.models.factors import FACTOR_NAMES, build_factor_bundle, score_factor_bundle, score_factors


@pytest.fixture
def factor_inputs():
    cfg = load_config("configs/research_30m_xl_lb_sres_books.yaml")
    index = pd.date_range("2025-10-01", periods=4, freq="30min", tz="UTC")
    columns = ["A", "B", "C", "D", "E"]
    mask = pd.DataFrame(True, index=index, columns=columns)
    mask.iloc[1, -1] = False
    names = build_factor_bundle(cfg, "combined").feature_names
    rng = np.random.default_rng(42)
    frames = {name: pd.DataFrame(rng.normal(size=mask.shape), index=index, columns=columns) for name in names}
    frames["flow_ret_div"] *= 0.1
    # Precision, clipping and NaN handling must survive the complete research/serve path.
    frames["iret_1440m"].iloc[0, 0] = 7.0012
    frames["cs_premium"].iloc[2, 1] = np.nan
    frames["unused"] = pd.DataFrame(123.0, index=index, columns=columns)
    return cfg, FeatureSet(frames=frames), mask


@pytest.mark.parametrize("name", FACTOR_NAMES)
def test_factor_research_scores_equal_loaded_serving_rows(factor_inputs, tmp_path, name):
    cfg, feats, mask = factor_inputs
    bundle = build_factor_bundle(cfg, name)
    expected = score_factor_bundle(bundle, feats, mask)
    bundle.save(tmp_path)
    restored = ModelBundle.load(tmp_path)
    actual = pd.DataFrame(np.nan, index=mask.index, columns=mask.columns)
    for ts in mask.index:
        members = mask.columns[mask.loc[ts]]
        X = np.column_stack([feats.frames[key].loc[ts, members] for key in restored.feature_names])
        X = X.astype(STORAGE_DTYPE).astype(np.float32)
        actual.loc[ts, members] = restored.score(X, np.zeros(len(members), dtype=np.int64))
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    assert not restored.promoted
    assert restored.prior_ic == 0.0
    assert restored.market is None and restored.gbm is None
    assert restored.meta["factor_rule"] == name
    assert restored.config == cfg


def test_combined_averages_raw_rules_before_standardisation(factor_inputs):
    cfg, feats, mask = factor_inputs
    raw = []
    for name in FACTOR_NAMES[:-1]:
        bundle = build_factor_bundle(cfg, name)
        X, _ = FeatureSet({key: feats.frames[key] for key in bundle.feature_names}).stack(mask, dtype=STORAGE_DTYPE)
        raw.append(bundle.ridge.predict(X.astype(np.float32)))
    combined = build_factor_bundle(cfg, "combined")
    X, _ = FeatureSet({key: feats.frames[key] for key in combined.feature_names}).stack(mask, dtype=STORAGE_DTYPE)
    np.testing.assert_allclose(combined.ridge.predict(X.astype(np.float32)), np.mean(raw, axis=0), atol=1e-15)


def test_missing_required_feature_is_rejected(factor_inputs):
    cfg, feats, mask = factor_inputs
    del feats.frames["cs_premium"]
    with pytest.raises(ValueError, match=r"Missing factor features.*cs_premium"):
        score_factor_bundle(build_factor_bundle(cfg, "basis"), feats, mask)


def test_all_rules_preserve_mask_and_ignore_feature_insertion_order(factor_inputs):
    cfg, feats, mask = factor_inputs
    first = score_factors(feats, mask, cfg)
    reordered = FeatureSet(dict(reversed(list(feats.frames.items()))))
    second = score_factors(reordered, mask, cfg)
    assert tuple(first) == ("carry", "basis", "reversal", "momentum", "flow", "combined")
    for name in first:
        pd.testing.assert_frame_equal(first[name], second[name], check_exact=True)
        assert first[name].where(~mask).isna().all().all()


def test_invalid_factor_name_is_rejected():
    with pytest.raises(ValueError, match="Unknown fixed factor"):
        build_factor_bundle(load_config(None), "best_after_looking")


@pytest.mark.parametrize("names", [[], ["cs_premium", "cs_premium"]])
def test_malformed_feature_names_are_rejected(factor_inputs, names):
    cfg, feats, mask = factor_inputs
    bundle = build_factor_bundle(cfg, "basis")
    bundle.feature_names = names
    with pytest.raises(ValueError, match="nonempty and unique"):
        score_factor_bundle(bundle, feats, mask)
