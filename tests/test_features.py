"""Causality: no feature, target or universe decision may depend on the future."""

import numpy as np
import pandas as pd

from hermes.config import FeatureConfig, UniverseConfig
from hermes.data.universe import universe_mask
from hermes.features.library import build_features, cs_rank_gauss


def _features(panel, bpd=24):
    mask = universe_mask(panel, UniverseConfig(top_n=10, min_history_days=5), bpd)
    return mask, build_features(panel, mask, FeatureConfig(), bpd)


def test_features_do_not_look_ahead(small_panel):
    """Truncating the future must leave every past feature value unchanged."""
    cut = 24 * 90
    mask_full, full = _features(small_panel)
    mask_cut, part = _features(small_panel.iloc(slice(0, cut)))
    pd.testing.assert_frame_equal(mask_full.iloc[:cut], mask_cut)
    for name in part.frames:
        a = full.frames[name].iloc[:cut].to_numpy()
        b = part.frames[name].to_numpy()
        np.testing.assert_allclose(a, b, rtol=1e-5, atol=1e-6, equal_nan=True, err_msg=name)
    for name in part.market:
        np.testing.assert_allclose(
            full.market[name].iloc[:cut].to_numpy(),
            part.market[name].to_numpy(),
            rtol=1e-5,
            atol=1e-6,
            equal_nan=True,
            err_msg=name,
        )


def test_live_window_parity(small_panel):
    """Features computed on the live lookback window match research features at the decision bar."""
    cfg = FeatureConfig()
    mask_full, full = _features(small_panel)
    t = len(small_panel.index) - 1
    window = small_panel.iloc(slice(t + 1 - cfg.max_lookback * 2, t + 1))
    mask_w = mask_full.iloc[t + 1 - cfg.max_lookback * 2 : t + 1]
    part = build_features(window, mask_w, cfg, 24)
    members = mask_full.iloc[t].to_numpy()
    bad = []
    for name in part.frames:
        a = full.frames[name].iloc[t].to_numpy()[members]
        b = part.frames[name].iloc[-1].to_numpy()[members]
        ok = np.isclose(a, b, rtol=2e-2, atol=2e-2) | (np.isnan(a) & np.isnan(b))
        if ok.mean() < 0.9:
            bad.append(name)
    assert not bad, f"live/research divergence: {bad}"


def test_cs_rank_gauss_is_standard_normal_scores():
    df = pd.DataFrame(np.random.default_rng(1).normal(size=(50, 30)))
    g = cs_rank_gauss(df)
    assert np.allclose(g.mean(axis=1), 0, atol=1e-9)
    assert (g.std(axis=1) > 0.8).all()
    # Monotone within each row.
    r = df.iloc[0].rank()
    assert (g.iloc[0].rank() == r).all()
