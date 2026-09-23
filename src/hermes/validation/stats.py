"""Statistics that decide whether a backtest is evidence or noise.

* :func:`sharpe` with Lo's (2002) autocorrelation-consistent annualisation;
* :func:`probabilistic_sharpe` (PSR) and :func:`deflated_sharpe` (DSR) -- Bailey & López de Prado (2012,
  2014): probability that the true Sharpe exceeds a benchmark given skew, kurtosis, sample length and, for
  DSR, the number of configurations tried;
* :func:`min_track_record` -- bars needed before a Sharpe is distinguishable from the benchmark;
* :func:`stationary_bootstrap` -- Politis & Romano (1994) resampling for confidence intervals that respect
  serial dependence;
* :func:`spa_test` -- Hansen's (2005) Superior Predictive Ability p-value against "no strategy beats zero";
* :func:`pbo` -- Probability of Backtest Overfitting via CSCV (Bailey, Borwein, López de Prado, Zhu 2017).
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
from scipy import stats

EULER = 0.5772156649015329


def sharpe(returns: np.ndarray, periods_per_year: float, lo_adjust: bool = True) -> float:
    """Annualised Sharpe ratio; with ``lo_adjust`` the annualisation uses the long-run variance (Lo 2002).

    The long-run variance is a Newey-West estimate (Bartlett weights, automatic bandwidth
    ``4 (n/100)^(2/9)``). Positive autocorrelation lowers the annualised Sharpe; negative autocorrelation is
    **not** allowed to raise it: estimated mean reversion in daily P&L is too noisy to be paid for.
    """
    r = np.asarray(returns, float)
    r = r[np.isfinite(r)]
    if len(r) < 3 or r.std(ddof=1) == 0:
        return 0.0
    sr = r.mean() / r.std(ddof=1)
    q = periods_per_year
    if not lo_adjust:
        return float(sr * np.sqrt(q))
    n = len(r)
    L = int(min(np.floor(4 * (n / 100.0) ** (2.0 / 9.0)), n // 4, q - 1))
    rc = r - r.mean()
    denom = float(rc @ rc)
    factor = 1.0
    for k in range(1, L + 1):
        factor += 2 * (1 - k / (L + 1)) * float(rc[k:] @ rc[:-k]) / denom
    return float(sr * np.sqrt(q) / np.sqrt(max(factor, 1.0)))


def _sr_std(sr: float, n: int, skew: float, kurt: float) -> float:
    """Std of the (per-period) Sharpe estimator under non-normal iid returns (Mertens 2002)."""
    v = (1 - skew * sr + (kurt - 1) / 4.0 * sr**2) / max(n - 1, 1)
    return float(np.sqrt(max(v, 1e-18)))


def probabilistic_sharpe(returns: np.ndarray, sr_benchmark: float = 0.0) -> float:
    """PSR: P[true per-period SR > sr_benchmark]. ``sr_benchmark`` is per period (not annualised)."""
    r = np.asarray(returns, float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 10 or r.std(ddof=1) == 0:
        return 0.0
    sr = r.mean() / r.std(ddof=1)
    sk = float(stats.skew(r))
    ku = float(stats.kurtosis(r, fisher=False))
    return float(stats.norm.cdf((sr - sr_benchmark) / _sr_std(sr, n, sk, ku)))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum of ``n_trials`` per-period Sharpe estimates under the null (true SR = 0)."""
    if n_trials <= 1:
        return 0.0
    z1 = stats.norm.ppf(1 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
    return float(np.sqrt(max(sr_variance, 0.0)) * ((1 - EULER) * z1 + EULER * z2))


def deflated_sharpe(returns: np.ndarray, n_trials: int, trial_sr_variance: float | None = None) -> float:
    """DSR: PSR against the Sharpe the best of ``n_trials`` useless strategies would show by luck.

    ``trial_sr_variance`` is the cross-trial variance of per-period Sharpe estimates; when unknown it is
    approximated by the sampling variance of one estimate (conservative for few, similar trials).
    """
    r = np.asarray(returns, float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 10:
        return 0.0
    if trial_sr_variance is None:
        sr = r.mean() / (r.std(ddof=1) + 1e-18)
        trial_sr_variance = _sr_std(sr, n, float(stats.skew(r)), float(stats.kurtosis(r, fisher=False))) ** 2
    sr0 = expected_max_sharpe(n_trials, trial_sr_variance)
    return probabilistic_sharpe(r, sr0)


def min_track_record(returns: np.ndarray, sr_benchmark: float = 0.0, confidence: float = 0.95) -> float:
    """Minimum number of periods for PSR(sr_benchmark) to reach ``confidence`` (inf if SR <= benchmark)."""
    r = np.asarray(returns, float)
    r = r[np.isfinite(r)]
    if len(r) < 10 or r.std(ddof=1) == 0:
        return float("inf")
    sr = r.mean() / r.std(ddof=1)
    if sr <= sr_benchmark:
        return float("inf")
    sk = float(stats.skew(r))
    ku = float(stats.kurtosis(r, fisher=False))
    z = stats.norm.ppf(confidence)
    return float(1 + (1 - sk * sr + (ku - 1) / 4 * sr**2) * (z / (sr - sr_benchmark)) ** 2)


def stationary_bootstrap_indices(n: int, mean_block: float, rng: np.random.Generator) -> np.ndarray:
    p = 1.0 / max(mean_block, 1.0)
    idx = np.empty(n, dtype=np.int64)
    idx[0] = rng.integers(n)
    jumps = rng.random(n) < p
    starts = rng.integers(0, n, size=n)
    for t in range(1, n):
        idx[t] = starts[t] if jumps[t] else (idx[t - 1] + 1) % n
    return idx


def stationary_bootstrap(
    returns: np.ndarray,
    statistic,  # type: ignore[no-untyped-def]
    n_samples: int = 2000,
    mean_block: float = 24.0,
    seed: int = 0,
) -> np.ndarray:
    r = np.asarray(returns, float)
    r = r[np.isfinite(r)]
    rng = np.random.default_rng(seed)
    return np.array([statistic(r[stationary_bootstrap_indices(len(r), mean_block, rng)]) for _ in range(n_samples)])


def spa_test(excess: np.ndarray, n_samples: int = 2000, mean_block: float = 24.0, seed: int = 0) -> float:
    """Hansen's SPA consistent p-value that the best of the strategies (columns) has positive mean.

    ``excess`` is (T x K) excess returns over the benchmark (zero exposure -> raw returns).
    """
    X = np.atleast_2d(np.asarray(excess, float))
    if X.shape[0] == 1:
        X = X.T
    X = X[np.all(np.isfinite(X), axis=1)]
    n, k = X.shape
    if n < 20:
        return 1.0
    mean = X.mean(axis=0)
    rng = np.random.default_rng(seed)
    boots = np.empty((n_samples, k))
    for b in range(n_samples):
        idx = stationary_bootstrap_indices(n, mean_block, rng)
        boots[b] = X[idx].mean(axis=0)
    omega = np.sqrt(n) * boots.std(axis=0, ddof=1) + 1e-18
    t_stat = max(0.0, float(np.max(np.sqrt(n) * mean / omega)))
    # Hansen's consistent recentring: the null mean is 0, except for strategies so poor
    # (t < -sqrt(2 log log n)) that they cannot matter, which keep their own mean.
    thresh = -np.sqrt(2 * np.log(np.log(n))) * omega / np.sqrt(n)
    mu_c = np.where(mean <= thresh, mean, 0.0)
    null = np.sqrt(n) * (boots - mean + mu_c) / omega
    t_null = np.maximum(null.max(axis=1), 0.0)
    return float(np.mean(t_null >= t_stat))


def pbo(performance: np.ndarray, n_splits: int = 16, metric=None) -> float:  # type: ignore[no-untyped-def]
    """Probability of Backtest Overfitting via Combinatorially Symmetric Cross-Validation.

    ``performance`` is a (T x N) matrix of per-period returns for N candidate configurations. For each
    symmetric split of the T periods into in-sample/out-of-sample halves, the in-sample winner's
    out-of-sample relative rank ``w`` is recorded; PBO is the share of splits where ``logit(w) <= 0``,
    i.e. where the chosen configuration is below the out-of-sample median.
    """
    M = np.asarray(performance, float)
    M = M[np.all(np.isfinite(M), axis=1)]
    T, N = M.shape
    if N < 2 or n_splits * 2 > T:
        return float("nan")
    metric = metric or (lambda x: x.mean(axis=0) / (x.std(axis=0, ddof=1) + 1e-18))
    blocks = np.array_split(np.arange(T), n_splits)
    logits = []
    for combo in combinations(range(n_splits), n_splits // 2):
        is_idx = np.concatenate([blocks[i] for i in combo])
        oos_idx = np.concatenate([blocks[i] for i in range(n_splits) if i not in combo])
        best = int(np.argmax(metric(M[is_idx])))
        oos_perf = metric(M[oos_idx])
        rank = stats.rankdata(oos_perf)[best] / (N + 1)
        logits.append(np.log(rank / (1 - rank)))
    return float(np.mean(np.array(logits) <= 0))
