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
