import numpy as np
import pandas as pd

from hermes.config import FeatureConfig, LabelConfig, UniverseConfig
from hermes.data.synthetic import make_synthetic_panel
from hermes.data.universe import universe_mask
from hermes.features.library import build_features
from hermes.labels.targets import build_targets, forward_sum, project_out, style_frames
from hermes.labels.triple_barrier import triple_barrier, uniqueness_weights


def test_forward_sum_window():
    s = pd.Series(np.arange(10, dtype=float))
    f = forward_sum(s, 3)
    assert f.iloc[0] == 1 + 2 + 3
    assert np.isnan(f.iloc[7])


def test_targets_include_funding_and_are_residual(small_panel):
    mask = universe_mask(small_panel, UniverseConfig(top_n=10, min_history_days=3))
    feats = build_features(small_panel, mask, FeatureConfig())
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


def test_project_out_is_orthogonal_to_intercept_and_exposures():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2024-01-01", periods=5, freq="30min", tz="UTC")
    cols = [f"S{i}" for i in range(12)]
    a = pd.DataFrame(rng.normal(size=(5, 12)), idx, cols)
    b = pd.DataFrame(rng.normal(size=(5, 12)), idx, cols)
    y = 0.3 + 2.0 * a - 1.0 * b + pd.DataFrame(rng.normal(scale=0.1, size=(5, 12)), idx, cols)
    y.iloc[1, 3] = np.nan
    b.iloc[2, 5] = np.nan
    y.iloc[4, :6] = np.nan  # too few members left: the row is dropped
    res = project_out(y, [a, b], min_members=8)
    assert res.iloc[4].isna().all()
    assert np.isnan(res.iloc[1, 3]) and np.isnan(res.iloc[2, 5])
    for t in range(4):
        r = res.iloc[t]
        ok = r.notna()
        assert abs(r[ok].sum()) < 1e-7
        assert abs((r[ok] * a.iloc[t][ok]).sum()) < 1e-7
        assert abs((r[ok] * b.iloc[t][ok]).sum()) < 1e-7
        assert r[ok].std() < 0.2  # the planted style loadings are gone


def test_style_residual_targets_have_no_style_loading(small_panel):
    mask = universe_mask(small_panel, UniverseConfig(top_n=10, min_history_days=3))
    feats = build_features(small_panel, mask, FeatureConfig())
    tg = build_targets(small_panel, feats, mask, LabelConfig(residualize="style", clip_sigma=1e6))
    size, vol = (x.where(mask) for x in style_frames(small_panel, feats.aux["ivol"]))
    res = tg.residual[8]
    rows = res.dropna(how="all").index[:50]
    assert len(rows) > 10
    for t in rows:
        r = res.loc[t]
        ok = r.notna()
        assert abs(r[ok].mean()) < 1e-8
        assert abs(np.corrcoef(r[ok], size.loc[t][ok])[0, 1]) < 1e-6
        assert abs(np.corrcoef(r[ok], vol.loc[t][ok])[0, 1]) < 1e-6


def test_style_projection_is_not_driven_by_one_jump():
    # One member jumps +30 % (tens of its own sigmas): its target is clipped, and the projection must be fitted
    # on that winsorised value, so the jump barely moves the other members' style-free targets.
    panel0 = make_synthetic_panel(n_assets=30, n_bars=96 * 60, bar="15m", seed=5)
    mask = universe_mask(panel0, UniverseConfig(top_n=24, min_history_days=3))
    k = len(panel0.index) - 400
    sym = mask.columns[mask.iloc[k].to_numpy()][-1]
    close = panel0["close"].copy()
    close.loc[close.index[k] :, sym] *= 1.3
    panel = panel0.with_fields({"close": close})
    feats = build_features(panel, mask, FeatureConfig())
    styles = [x.where(mask) for x in style_frames(panel, feats.aux["ivol"])]
    style_t = build_targets(panel, feats, mask, LabelConfig(residualize="style")).residual[8]
    beta_t = build_targets(panel, feats, mask, LabelConfig(residualize="beta")).residual[8].where(mask)
    without = beta_t.copy()
    without[sym] = np.nan
    reference = project_out(without, [x.where(without.notna()) for x in styles])  # jump left out of the fit
    rows = style_t.index[k - 8 : k]  # every forward window that contains the jump
    assert (beta_t.loc[rows, sym] == 5.0).all()
    moved = (style_t.loc[rows].drop(columns=sym) - reference.loc[rows].drop(columns=sym)).abs().max().max()
    assert moved < 1.0  # fitting on the raw jump moves them by more than 2 sigma
    for t in rows:
        r = style_t.loc[t]
        ok = r.notna()
        for x in styles:
            assert abs(np.corrcoef(r[ok], x.loc[t][ok])[0, 1]) < 1e-6
