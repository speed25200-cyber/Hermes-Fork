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
