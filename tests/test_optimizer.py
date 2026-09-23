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


def test_small_account_drops_untradable_positions(rng):
    n = 30
    cfg = PortfolioConfig(min_position_usdt=20.0)
    pc = PortfolioConstructor(cfg, bars_per_year=8760, ic_ref=0.03)
    inp = BookInputs(
        score=rng.normal(size=n),
        ivol=np.full(n, 0.01),
        beta=np.ones(n),
        mkt_var=1e-4,
        cost_rate=np.full(n, 1e-4),
        adv=np.full(n, 1e9),
        w0=np.zeros(n),
        ic=0.03,
    )
    res = pc.target(inp, equity=500.0)
    held = res.weights[res.weights != 0]
    assert len(held) < n
    assert np.all(np.abs(held) * 500.0 >= 20.0 - 1e-9)
    assert abs(res.weights.sum()) <= 0.05 + 1e-9


def test_market_alpha_is_causal_and_follows_realised_skill():
    import pandas as pd

    from hermes.portfolio.alpha import market_alpha_series

    rng = np.random.default_rng(3)
    n = 24 * 400
    idx = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    y = pd.Series(rng.normal(size=n), index=idx)
    mkt = pd.Series(rng.normal(0, 0.005, n), index=idx)
    skilled = y * 0.3 + pd.Series(rng.normal(size=n), index=idx)
    noise = pd.Series(rng.normal(size=n), index=idx)
    a_sk = market_alpha_series(skilled, y, mkt, 0.0, 8, 24)
    a_no = market_alpha_series(noise, y, mkt, 0.0, 8, 24)
    assert a_sk.abs().iloc[-2000:].mean() > 3 * a_no.abs().iloc[-2000:].mean()
    # Causality: changing the future target leaves past alphas unchanged.
    y2 = y.copy()
    y2.iloc[-100:] = 0.0
    a2 = market_alpha_series(skilled, y2, mkt, 0.0, 8, 24)
    pd.testing.assert_series_equal(a_sk.iloc[: -100 - 8], a2.iloc[: -100 - 8])


def test_style_neutral_book_hedges_size_and_volatility_bets():
    from hermes.config import PortfolioConfig
    from hermes.portfolio.construct import BookInputs, PortfolioConstructor, style_exposures

    r = np.random.default_rng(7)
    n = 30
    adv = np.exp(r.normal(17, 1.5, n))
    ivol = np.exp(r.normal(np.log(0.006), 0.4, n))
    size_z = (np.log(adv) - np.log(adv).mean()) / np.log(adv).std()
    score = -1.5 * size_z + r.normal(0, 0.5, n)  # the signal mostly says "buy small caps"
    inp = BookInputs(
        score=score,
        ivol=ivol,
        beta=np.ones(n),
        mkt_var=2.5e-5,
        cost_rate=np.full(n, 1e-4),
        adv=adv,
        w0=np.zeros(n),
        ic=0.05,
    )
    exposures = {}
    for neutral in (False, True):
        cfg = PortfolioConfig(style_neutral=neutral, holding_horizon=16, weight_max=0.2)
        w = PortfolioConstructor(cfg, 35040, 0.03).target(inp, 1e6).weights
        S = style_exposures(adv, ivol, np.ones(n, dtype=bool))
        exposures[neutral] = np.abs(S.T @ w) / max(np.abs(w).sum(), 1e-12)
    assert exposures[True][0] < 0.5 * exposures[False][0]  # size exposure per unit of gross at least halved
