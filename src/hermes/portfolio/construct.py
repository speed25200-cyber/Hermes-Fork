"""One rebalance step: scores -> target weights. Shared verbatim by the backtest and the live engine."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from hermes.config import PortfolioConfig
from hermes.portfolio.covariance import blended_covariance
from hermes.portfolio.optimizer import project_exposure, risk_aversion, solve


@dataclass
class BookInputs:
    score: np.ndarray  # cross-sectional z-score, NaN = not tradable
    ivol: np.ndarray  # residual volatility per bar
    beta: np.ndarray
    mkt_var: float  # market variance per bar
    cost_rate: np.ndarray  # linear cost per unit traded
    adv: np.ndarray  # average daily dollar volume
    w0: np.ndarray  # current weights (fraction of equity)
    ic: float  # estimated IC of the score
    market_alpha: float = 0.0  # expected market return over the horizon (0 = market-neutral book)
    sample_cov: np.ndarray | None = None  # per-bar EWMA covariance (optional)


def style_exposures(adv: np.ndarray, ivol: np.ndarray, tradable: np.ndarray) -> np.ndarray:
    """(n x 2) cross-sectional z-scores of log dollar volume (size/liquidity) and log residual volatility
    over the tradable contracts; zero elsewhere. Same inputs in the backtest and the live engine."""
    out = np.zeros((len(adv), 2))
    for j, x in enumerate((np.log(np.where(adv > 0, adv, np.nan)), np.log(np.where(ivol > 0, ivol, np.nan)))):
        ok = tradable & np.isfinite(x)
        if ok.sum() >= 3 and np.nanstd(x[ok]) > 0:
            out[ok, j] = (x[ok] - x[ok].mean()) / x[ok].std()
    return out


@dataclass
class BookResult:
    weights: np.ndarray
    alpha: np.ndarray
    cov_bar: np.ndarray
    ex_ante_vol_annual: float
    n_tradable: int
    lam: float


class PortfolioConstructor:
    def __init__(self, cfg: PortfolioConfig, bars_per_year: float, ic_ref: float, cov_shrink: float = 0.3):
        self.cfg = cfg
        self.bars_per_year = bars_per_year
        self.ic_ref = ic_ref
        self.cov_shrink = cov_shrink

    def target(self, inp: BookInputs, equity: float) -> BookResult:
        c = self.cfg
        n = len(inp.score)
        H = float(c.holding_horizon)
        tradable = np.isfinite(inp.score) & np.isfinite(inp.ivol) & (inp.ivol > 0) & np.isfinite(inp.beta)
        z = np.where(tradable, inp.score, 0.0)
        ivol = np.where(
            np.isfinite(inp.ivol) & (inp.ivol > 0),
            inp.ivol,
            np.nanmedian(inp.ivol) if np.any(np.isfinite(inp.ivol)) else 0.01,
        )
        beta = np.where(np.isfinite(inp.beta), inp.beta, 1.0)
        alpha = inp.ic * z * ivol * np.sqrt(H)
        alpha = np.where(tradable, alpha + beta * inp.market_alpha, 0.0)

        cov_bar = blended_covariance(beta, inp.mkt_var, ivol, inp.sample_cov, self.cov_shrink)
        cov_h = cov_bar * H
        adv = np.where(np.isfinite(inp.adv) & (inp.adv > 0), inp.adv, 0.0)
        cap = np.minimum(c.weight_max, c.adv_participation_max * adv / max(equity, 1e-9))
        cap = np.where(tradable, cap, 0.0)

        n_tr = int(tradable.sum())
        vol_target_h = c.vol_target_annual * np.sqrt(H / self.bars_per_year)
        lam = risk_aversion(self.ic_ref, n_tr, vol_target_h)
        net_max = c.net_max if inp.market_alpha != 0.0 or not c.beta_neutral else min(c.net_max, 0.05)
        penalty_q = None
        if c.style_neutral:
            # Style factors priced like the market: a unit of size or volatility exposure costs as much risk as
            # a unit of market beta, so the optimiser keeps the book's P&L idiosyncratic.
            S = style_exposures(inp.adv, ivol, tradable)
            penalty_q = c.style_risk * inp.mkt_var * H * (S @ S.T)
        res = solve(
            penalty_q=penalty_q,
            alpha=alpha,
            cov=cov_h,
            w0=np.nan_to_num(inp.w0),
            cost=np.nan_to_num(inp.cost_rate, nan=0.002) * c.cost_aversion,
            cap=cap,
            lam=lam,
            beta=beta if c.beta_neutral else np.ones(n),
            net_max=net_max,
            vol_cap=vol_target_h,
            gross_max=c.gross_max,
        )
        w = res.weights
        # Dust control: never open or adjust by less than the minimum trade size.
        small = np.abs(w - inp.w0) < c.min_trade_weight
        w = np.where(small & tradable, inp.w0, w)
        w = np.where(tradable, w, 0.0)
        # Positions too small to be held at the exchange (lot sizes) are dropped, then the exposure bound is
        # restored: silently losing them at order rounding would unbalance a beta-neutral book.
        min_w = c.min_position_usdt / max(equity, 1e-9)
        if min_w > 0 and np.any((np.abs(w) > 0) & (np.abs(w) < min_w)):
            w = np.where(np.abs(w) < min_w, 0.0, w)
            b = beta if c.beta_neutral else np.ones(n)
            w = project_exposure(w, b, np.where(w != 0, cap, 0.0), net_max)
        vol_ann = float(np.sqrt(max(w @ cov_bar @ w, 0.0) * self.bars_per_year))
        return BookResult(w, alpha, cov_bar, vol_ann, n_tr, lam)
