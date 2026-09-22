import numpy as np

from hermes.config import PortfolioConfig
from hermes.portfolio.construct import BookInputs, PortfolioConstructor
from hermes.portfolio.optimizer import solve


def _cov(n, rng):
    A = rng.normal(size=(n, n)) * 0.01
    return A @ A.T + np.eye(n) * 1e-4


def test_zero_alpha_zero_book(rng):
    n = 10
    res = solve(np.zeros(n), _cov(n, rng), np.zeros(n), np.full(n, 1e-3), np.full(n, 0.2), lam=10)
    assert np.allclose(res.weights, 0)


def test_no_trade_region(rng):
    n = 8
    cov = _cov(n, rng)
    w0 = rng.normal(0, 0.05, n)
    alpha = cov @ w0 * 10  # alpha that makes w0 exactly optimal without costs for lam=10
    res = solve(alpha, cov, w0, np.full(n, 1e-3), np.full(n, 1.0), lam=10)
    assert np.allclose(res.weights, w0, atol=1e-6)
    # Tiny alpha change, costs prevent trading.
    res2 = solve(alpha * 1.01, cov, w0, np.full(n, 5e-3), np.full(n, 1.0), lam=10)
    assert np.allclose(res2.weights, w0, atol=1e-6)


def test_caps_net_and_vol(rng):
    n = 12
    cov = _cov(n, rng)
    alpha = rng.normal(0, 0.05, n)
    beta = np.ones(n)
    res = solve(
        alpha,
        cov,
        np.zeros(n),
        np.zeros(n),
        np.full(n, 0.1),
        lam=0.1,
        beta=beta,
        net_max=0.05,
        vol_cap=0.02,
        gross_max=1.0,
    )
    w = res.weights
    assert np.all(np.abs(w) <= 0.1 + 1e-12)
    assert abs(w.sum()) <= 0.05 + 1e-9
    assert np.sqrt(w @ cov @ w) <= 0.02 + 1e-9
    assert np.abs(w).sum() <= 1.0 + 1e-9


def test_constructor_scales_with_ic(rng):
    n = 20
    cfg = PortfolioConfig()
    pc = PortfolioConstructor(cfg, bars_per_year=8760, ic_ref=0.03)
    base = dict(
        score=rng.normal(size=n),
        ivol=np.full(n, 0.01),
        beta=np.ones(n),
        mkt_var=1e-4,
        cost_rate=np.full(n, 3e-4),
        adv=np.full(n, 1e9),
        w0=np.zeros(n),
    )
    lo = pc.target(BookInputs(ic=0.01, **base), equity=1e5)
    hi = pc.target(BookInputs(ic=0.03, **base), equity=1e5)
    zero = pc.target(BookInputs(ic=0.0, **base), equity=1e5)
    assert np.abs(zero.weights).sum() == 0
    assert np.abs(lo.weights).sum() < np.abs(hi.weights).sum()
    assert hi.ex_ante_vol_annual <= cfg.vol_target_annual * 1.05
    assert abs(hi.weights.sum()) <= 0.05 + 1e-9  # beta-neutral by default (betas all 1)
