import numpy as np
import pandas as pd

from hermes.config import FeatureConfig, LabelConfig, UniverseConfig
from hermes.data.universe import universe_mask
from hermes.features.library import build_features
from hermes.labels.targets import build_targets, forward_sum
from hermes.labels.triple_barrier import triple_barrier, uniqueness_weights


def test_forward_sum_window():
    s = pd.Series(np.arange(10, dtype=float))
    f = forward_sum(s, 3)
    assert f.iloc[0] == 1 + 2 + 3
    assert np.isnan(f.iloc[7])


def test_targets_include_funding_and_are_residual(small_panel):
    mask = universe_mask(small_panel, UniverseConfig(top_n=10, min_history_days=5), 24)
    feats = build_features(small_panel, mask, FeatureConfig(), 24)
    tg = build_targets(small_panel, feats, mask, LabelConfig(residualize="mean", vol_normalize=False))
    res = tg.residual[8]
    # Mean-residualised targets sum to ~0 across members at each time.
    row_means = res.mean(axis=1).dropna()
    assert np.abs(row_means).max() < 1e-9
    # Funding is subtracted: total return = price return - funding over the window.
    r1 = feats.aux["r1"]
    f = small_panel["funding_rate"].fillna(0)
    manual = forward_sum(r1 - f.where(r1.notna()), 8)
    pd.testing.assert_frame_equal(tg.total[8], manual.where(mask), check_names=False)


def test_triple_barrier_hits():
    idx = pd.date_range("2024-01-01", periods=6, freq="1h", tz="UTC")
    close = pd.Series([100, 100, 100, 100, 100, 100.0], index=idx)
    high = pd.Series([100, 100, 103, 100, 100, 100.0], index=idx)
    low = pd.Series([100, 99.5, 100, 100, 100, 100.0], index=idx)
    sigma = pd.Series(0.01, index=idx)
    out = triple_barrier(close, high, low, sigma, horizon=4, pt=1.0, sl=1.0)
    # From t=0: bar 1 low 99.5 does not reach -2% (sigma*sqrt(4)), bar 2 high 103 reaches +2%.
    assert out["label"].iloc[0] == 1
    assert out["bars"].iloc[0] == 2
    # Short side: the same up-move is a loss.
    out_s = triple_barrier(close, high, low, sigma, horizon=4, pt=1.0, sl=1.0, side=pd.Series(-1.0, index=idx))
    assert out_s["label"].iloc[0] == 0


def test_uniqueness_weights_overlap():
    bars = pd.Series([2.0, 2.0, 2.0, np.nan])
    w = uniqueness_weights(bars)
    assert 0 < w.iloc[1] < 1
    assert w.iloc[0] >= w.iloc[1]
