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


def test_funding_interval_is_inferred_from_settlements():
    from hermes.features.library import funding_interval_hours

    idx = pd.date_range("2025-01-01", periods=48 * 6, freq="30min", tz="UTC")
    fr = pd.DataFrame(np.nan, index=idx, columns=["A", "B", "C"])
    fr.loc[idx[::16], "A"] = 1e-4  # every 8 h
    fr.loc[idx[::8], "B"] = 1e-4  # every 4 h
    fr.loc[idx[: 48 * 3 : 16], "C"] = 1e-4  # 8 h for three days, then 4 h
    fr.loc[idx[48 * 3 :: 8], "C"] = 1e-4
    fr.loc[idx[48 * 2 + 16], "A"] = np.nan  # one missing settlement
    iv = funding_interval_hours(fr, 48)
    assert iv.iloc[0].tolist() == [8.0, 8.0, 8.0]  # nothing known yet: the default
    assert (iv["A"].iloc[20:] == 8.0).all()  # a missing settlement does not double it
    assert (iv["B"].iloc[8:] == 4.0).all()
    assert (iv["C"].iloc[16 : 48 * 3] == 8.0).all() and (iv["C"].iloc[48 * 3 + 8 :] == 4.0).all()
    # Causal: truncating the future changes nothing.
    pd.testing.assert_frame_equal(funding_interval_hours(fr.iloc[:200], 48), iv.iloc[:200])


def test_funding_on_an_8h_basis(small_panel):
    """A contract settling every 4 h at half the rate reads like the 8-hour contract it pays the same as."""
    cfg = FeatureConfig(funding_per_8h=True)
    fr = small_panel["funding_rate"].copy()
    a, b = fr.columns[:2]
    fr[b] = np.nan
    s8 = fr[a].dropna()
    fr.loc[s8.index, b] = s8.to_numpy() / 2
    half = pd.Series(s8.to_numpy() / 2, index=s8.index + pd.Timedelta(hours=4))
    half = half[half.index.isin(fr.index)]
    fr.loc[half.index, b] = half  # same cash per 8 h, in two settlements
    panel = small_panel.with_fields({"funding_rate": fr})
    mask = universe_mask(panel, UniverseConfig(top_n=10, min_history_days=3))
    f = build_features(panel, mask, cfg)
    t = slice(96 * 3, None)
    fa, fb = f.frames["funding_last"][a].iloc[t], f.frames["funding_last"][b].iloc[t]
    both = fa.notna() & fb.notna()
    assert both.mean() > 0.9
    assert np.allclose(fa[both], fb[both], atol=0.05 + 0.51 * fa[both].abs().max())  # up to the 4-h lag
    assert (f.frames["funding_short_interval"][b].iloc[t] == 1).all()
    assert (f.frames["funding_short_interval"][a].iloc[t] == 0).all()
    tf = f.frames["to_funding"].iloc[t]
    close_hour = (tf.index + small_panel.bar_delta).hour + (tf.index + small_panel.bar_delta).minute / 60
    at4 = np.isclose(np.mod(close_hour, 8), 4)  # a settlement of the 4-hour contract only
    assert at4.any() and (tf[b][at4] == 0).all() and (tf[a][at4] == 0.5).all() and "to_funding" not in f.market
    # Unchanged sums: the cash paid over a window does not depend on how often it is settled.
    np.testing.assert_allclose(
        f.frames["funding_sum_1440m"][a].iloc[t], f.frames["funding_sum_1440m"][b].iloc[t], atol=0.5 * fa.abs().max()
    )
    # The flag off leaves the historical definitions alone.
    off = build_features(panel, mask, FeatureConfig())
    assert "funding_short_interval" not in off.frames and "to_funding" in off.market


def test_funding_per_8h_features_are_causal_and_live_consistent(small_panel):
    from hermes.config import bars_for

    cfg = FeatureConfig(funding_per_8h=True)
    mask = universe_mask(small_panel, UniverseConfig(top_n=10, min_history_days=3))
    full = build_features(small_panel, mask, cfg)
    cut = 96 * 45
    part = build_features(small_panel.iloc(slice(0, cut)), mask.iloc[:cut], cfg)
    t = len(small_panel.index) - 1
    w = 2 * bars_for(cfg.max_lookback_minutes, small_panel.bar) + 8 * bars_for(cfg.vol_halflife_minutes, "15m")
    live = build_features(small_panel.iloc(slice(t + 1 - w, t + 1)), mask.iloc[t + 1 - w : t + 1], cfg)
    for name in ("funding_last", "funding_short_interval", "to_funding", "funding_z", "funding_chg_day"):
        np.testing.assert_allclose(
            full.frames[name].iloc[:cut].to_numpy(), part.frames[name].to_numpy(), rtol=1e-5, atol=1e-6, equal_nan=True
        )
        np.testing.assert_allclose(
            full.frames[name].iloc[t].to_numpy(),
            live.frames[name].iloc[-1].to_numpy(),
            rtol=2e-2,
            atol=2e-2,
            equal_nan=True,
            err_msg=name,
        )


def test_positioning_families_are_selected_and_read_one_bar_late(small_panel):
    rng = np.random.default_rng(3)
    ls = pd.DataFrame(
        np.exp(rng.normal(0, 0.2, size=small_panel["close"].shape)).cumprod(axis=0) ** 0.01,
        index=small_panel.index,
        columns=small_panel.symbols,
    )
    panel = small_panel.with_fields({"ls_top": ls})
    mask = universe_mask(panel, UniverseConfig(top_n=10, min_history_days=3))
    f = build_features(panel, mask, FeatureConfig(positioning=("ls_top",)))
    assert {"ls_top_z", "ls_top_chg_day", "cs_ls_top_z"} <= set(f.frames)
    assert not any(n.startswith("oi_") for n in f.frames)  # the synthetic panel carries OI: not selected
    assert any(n.startswith("oi_") for n in build_features(panel, mask, FeatureConfig()).frames)
    # The last bar's ratio is not read at that bar (published late): changing it changes nothing there.
    bumped = ls.copy()
    bumped.iloc[-1] *= 3.0
    g = build_features(panel.with_fields({"ls_top": bumped}), mask, FeatureConfig(positioning=("ls_top",)))
    for name in ("ls_top_z", "ls_top_chg_day"):
        np.testing.assert_allclose(f.frames[name].to_numpy(), g.frames[name].to_numpy(), equal_nan=True)
    # 28-day z-score window at most: 30 days of live history reproduce it.
    t = len(panel.index) - 1
    w = 30 * 96
    live = build_features(
        panel.iloc(slice(t + 1 - w, t + 1)), mask.iloc[t + 1 - w :], FeatureConfig(positioning=("ls_top",))
    )
    np.testing.assert_allclose(
        f.frames["ls_top_z"].iloc[t].to_numpy(), live.frames["ls_top_z"].iloc[-1].to_numpy(), rtol=1e-6, atol=1e-6
    )
