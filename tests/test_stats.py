import numpy as np

from hermes.validation.stats import (
    deflated_sharpe,
    min_track_record,
    pbo,
    probabilistic_sharpe,
    sharpe,
    spa_test,
)


def test_sharpe_annualisation_iid(rng):
    r = rng.normal(0.001, 0.01, 20000)
    s = sharpe(r, 365, lo_adjust=False)
    assert abs(s - 0.1 * np.sqrt(365)) < 0.35
    # Lo's correction barely changes an iid series.
    assert abs(sharpe(r, 365) - s) / s < 0.1


def test_lo_adjustment_penalises_positive_autocorrelation(rng):
    e = rng.normal(0, 0.01, 20000)
    r = np.empty_like(e)
    r[0] = e[0]
    for t in range(1, len(e)):
        r[t] = 0.5 * r[t - 1] + e[t]
    r += 0.0005
    assert sharpe(r, 365) < sharpe(r, 365, lo_adjust=False)


def test_psr_and_dsr(rng):
    good = rng.normal(0.002, 0.01, 1000)
    noise = rng.normal(0.0, 0.01, 1000)
    assert probabilistic_sharpe(good) > 0.95
    assert 0.02 < probabilistic_sharpe(noise) < 0.98
    assert deflated_sharpe(good, 1000) < deflated_sharpe(good, 1)
    assert min_track_record(good) < min_track_record(good - 0.001)


def test_pbo_noise_vs_dominant(rng):
    noise = rng.normal(0, 0.01, (1200, 12))
    p_noise = pbo(noise, n_splits=10)
    assert 0.25 < p_noise < 0.75
    dominant = noise.copy()
    dominant[:, 3] += 0.004
    assert pbo(dominant, n_splits=10) < 0.1


def test_spa(rng):
    strong = rng.normal(0.003, 0.01, 800)
    noise = rng.normal(0.0, 0.01, (800, 5))
    assert spa_test(strong, n_samples=500) < 0.05
    assert spa_test(noise, n_samples=500) > 0.05


def test_negative_autocorrelation_never_inflates_the_sharpe(rng):
    e = rng.normal(0, 0.01, 3000)
    r = e.copy()
    r[1:] -= 0.6 * e[:-1]  # strongly mean-reverting daily P&L
    r += 0.0005
    assert sharpe(r, 365) <= sharpe(r, 365, lo_adjust=False) + 1e-12


def test_ic_t_stat_accounts_for_daily_clustering(rng):
    import pandas as pd

    from hermes.validation.metrics import ic_summary

    days = 200
    idx = pd.date_range("2024-01-01", periods=days * 96, freq="15min", tz="UTC")
    day_effect = np.repeat(rng.normal(0.0, 0.05, days), 96)  # IC regimes that last a day
    ic = pd.Series(0.004 + day_effect + rng.normal(0, 0.02, len(idx)), index=idx)
    t_clustered = ic_summary(ic, horizon=4)["ic_t"]
    t_naive = ic_summary(ic.reset_index(drop=True), horizon=4)["ic_t"]
    assert abs(t_clustered) < abs(t_naive) / 3  # bar-level kernels overstate significance


def test_dsr_trials_and_variance_floor():
    import pandas as pd

    from hermes.research.evaluate import positive_year_fraction, trial_count_and_variance

    rng_ = np.random.default_rng(3)
    base = rng_.normal(0.001, 0.01, 500)
    same = pd.DataFrame({f"g{i}": base + rng_.normal(0, 1e-5, 500) for i in range(9)})
    n, var = trial_count_and_variance(4, same, 500)
    assert n == 4  # nine near-identical variants are one effective trial
    assert var >= 1.0 / 499  # never below the null sampling variance of a daily Sharpe
    indep = pd.DataFrame({f"g{i}": rng_.normal(0, 0.01, 500) for i in range(9)})
    assert trial_count_and_variance(4, indep, 500)[0] >= 30
    # A stub year does not vote.
    idx = pd.date_range("2024-01-01", "2025-01-20", freq="D", tz="UTC")
    d = pd.Series(0.001, index=idx)
    d[d.index.year == 2025] = -0.01
    assert positive_year_fraction(d) == 1.0


def test_psi_flags_a_shifted_feature_and_not_a_stable_one():
    from hermes.models.drift import feature_profile, psi

    r = np.random.default_rng(1)
    train = np.column_stack([r.normal(size=50_000), r.normal(size=50_000)])
    train[::50, 1] = np.nan
    prof = feature_profile(train, ["stable", "shifted"])
    live = np.column_stack([r.normal(size=3_000), r.normal(1.0, 1.0, size=3_000)])
    out = psi(prof, live, ["stable", "shifted"])
    assert out["stable"] < 0.05 and out["shifted"] > 0.25


def test_null_permutation_is_a_stable_derangement_within_blocks():
    import pandas as pd

    from hermes.research.evaluate import block_permute, trial_count_and_variance

    r = np.random.default_rng(2)
    idx = pd.date_range("2024-01-01", periods=96 * 21, freq="15min", tz="UTC")
    cols = [f"S{i}" for i in range(8)]
    S = pd.DataFrame(r.normal(size=(len(idx), 8)), index=idx, columns=cols)
    mask = pd.DataFrame(True, index=idx, columns=cols)
    mask.iloc[500:, 7] = False  # one contract leaves mid-block
    out = block_permute(S, mask, seed=0, block_days=7).to_numpy()
    s = S.to_numpy()
    for t in range(1, len(idx)):
        for i in range(7):
            if np.isfinite(out[t, i]):
                src = int(np.flatnonzero(np.isclose(s[t], out[t, i]))[0])
                assert src != i  # never its own score
                if np.isfinite(out[t - 1, i]) and idx[t].floor("D") == idx[t - 1].floor("D"):
                    assert np.isclose(out[t - 1, i], s[t - 1, src])  # same partner as the previous bar
    flat = pd.DataFrame({"a": r.normal(0, 0.01, 200), "b": 0.0, "c": 0.0})
    n, var = trial_count_and_variance(3, flat, 200)  # flat variants: no crash, counted as independent
    assert n >= 3 and var > 0


def test_wide_rank_ic_equals_the_long_format_ic():
    import pandas as pd

    from hermes.validation.metrics import cross_sectional_ic, rank_ic_wide

    r = np.random.default_rng(4)
    idx = pd.date_range("2024-01-01", periods=300, freq="15min", tz="UTC")
    a = pd.DataFrame(r.normal(size=(300, 9)), index=idx)
    b = 0.3 * a + pd.DataFrame(r.normal(size=(300, 9)), index=idx)
    a.iloc[::7, 2] = np.nan
    b.iloc[::5, 4] = np.nan
    wide = rank_ic_wide(a, b)
    long = cross_sectional_ic(a.stack(), b.stack())
    np.testing.assert_allclose(wide.to_numpy(), long.reindex(wide.index).to_numpy(), atol=1e-12)


def test_smoothed_scores_are_time_decayed_and_masked():
    import pandas as pd

    from hermes.portfolio.alpha import smooth_scores

    idx = pd.date_range("2024-01-01", periods=6, freq="15min", tz="UTC")
    s = pd.DataFrame({"A": [1.0, 1.0, np.nan, -1.0, -1.0, -1.0]}, index=idx)
    out = smooth_scores(s, 1.0)
    assert np.isnan(out.iloc[2, 0])  # not a member: no score
    assert 0 > out.iloc[3, 0] > -1 and out.iloc[5, 0] < out.iloc[3, 0]  # moves toward the new sign
    assert smooth_scores(s, 0.0).equals(s)


def test_regime_scale_gates_the_day_after_a_close_below_the_threshold():
    import pandas as pd

    from hermes.portfolio.alpha import regime_scale

    days = pd.date_range("2025-01-01", periods=120, freq="1D", tz="UTC")
    close = pd.Series(100.0, index=days)
    close.iloc[100:] = 80.0  # -20 % from the 90-day high from day 100
    g = regime_scale(close, 0.15, 90, 0.5)
    assert g.iloc[100] == 1.0 and g.iloc[101] == 0.5  # decided from the previous close only
    assert g.index[-1] == days[-1] + pd.Timedelta(days=1) and g.iloc[-1] == 0.5  # "today" for the live engine
    assert (regime_scale(close, 0.0, 90, 0.5) == 1.0).all()
    # Causal: the gate of day D does not change when later closes do.
    later = close.copy()
    later.iloc[105:] = 100.0
    assert regime_scale(later, 0.15, 90, 0.5).iloc[:106].equals(g.iloc[:106])
