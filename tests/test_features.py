"""Causality: no feature, target or universe decision may depend on the future."""

import numpy as np
import pandas as pd

from hermes.config import FeatureConfig, UniverseConfig
from hermes.data.universe import universe_mask
from hermes.features.library import build_features, cs_rank_gauss


def _features(panel):
    mask = universe_mask(panel, UniverseConfig(top_n=10, min_history_days=3))
    return mask, build_features(panel, mask, FeatureConfig())


def test_features_do_not_look_ahead(small_panel):
    """Truncating the future must leave every past feature value unchanged."""
    cut = 96 * 45
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
    from hermes.config import bars_for

    cfg = FeatureConfig()
    mask_full, full = _features(small_panel)
    t = len(small_panel.index) - 1
    w = 2 * bars_for(cfg.max_lookback_minutes, small_panel.bar) + 8 * bars_for(cfg.vol_halflife_minutes, "15m")
    window = small_panel.iloc(slice(t + 1 - w, t + 1))
    mask_w = mask_full.iloc[t + 1 - w : t + 1]
    part = build_features(window, mask_w, cfg)
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


def test_windows_are_named_by_the_minutes_they_span(small_panel):
    from hermes.data.panel import resample_panel

    p30 = resample_panel(small_panel, "30m")
    mask = universe_mask(p30, UniverseConfig(top_n=8, min_history_days=3))
    cfg = FeatureConfig(return_minutes=(15, 30, 60), vol_minutes=(15, 240), flow_minutes=(15, 60))
    names = build_features(p30, mask, cfg).names
    # A 15-minute window on 30-minute bars is one bar: it is the 30-minute window, named as such.
    assert "ret_30m" in names and "ret_15m" not in names and "ret_60m" in names
    assert "rv_ratio_30m" in names and "flow_30m" in names  # one-bar windows do not crash either
