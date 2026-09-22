"""From model scores to expected returns (Grinold's 'alpha = IC x volatility x score').

The model outputs a cross-sectional score. Its *economic* value is only known once outcomes are realised,
so the information coefficient used for sizing is **estimated online from realised performance**, lagged
by the horizon (strictly causal), and shrunk toward the prior IC measured on the model's validation block.

This is the system's first line of defence against alpha decay: if the model stops working, its realised
IC falls, expected returns shrink toward zero, and the cost-aware optimiser stops trading -- without any
human in the loop.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rowwise_corr(a: pd.DataFrame, b: pd.DataFrame, min_names: int = 5) -> pd.Series:
    x = a.where(b.notna())
    y = b.where(a.notna())
    xm = x.sub(x.mean(axis=1), axis=0)
    ym = y.sub(y.mean(axis=1), axis=0)
    num = (xm * ym).sum(axis=1)
    den = np.sqrt((xm**2).sum(axis=1) * (ym**2).sum(axis=1))
    ic = num / den.replace(0, np.nan)
    return ic.where(x.notna().sum(axis=1) >= min_names)


def cs_zscore(df: pd.DataFrame, mask: pd.DataFrame | None = None, clip: float = 3.0) -> pd.DataFrame:
    x = df.where(mask) if mask is not None else df
    z = x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1).replace(0, np.nan), axis=0)
    return z.clip(-clip, clip)


def estimate_ic(
    realized_ic: pd.Series,
    horizon: int,
    prior_ic: float | pd.Series,
    halflife_bars: int,
    prior_weight_obs: float = 30.0,
    cap: float = 0.15,
) -> pd.Series:
    """Causal IC estimate at each bar.

    ``realized_ic[t]`` compares the score at ``t`` with the outcome over ``(t, t+h]``; it becomes known at
    ``t+h``, hence the shift. The EWMA mean is combined with the prior in proportion to the number of
    *independent* observations (bars / horizon) behind it.
    """
    known = realized_ic.shift(horizon)
    ewm = known.ewm(halflife=halflife_bars, min_periods=1, adjust=True).mean()
    n = known.notna().astype(float).rolling(halflife_bars * 2, min_periods=1).sum()
    n_eff = n / max(horizon, 1)
    prior = prior_ic if isinstance(prior_ic, pd.Series) else pd.Series(prior_ic, index=realized_ic.index)
    est = (n_eff * ewm.fillna(0.0) + prior_weight_obs * prior.fillna(0.0)) / (n_eff + prior_weight_obs)
    return est.clip(lower=0.0, upper=cap)


def signal_persistence(score: pd.DataFrame, horizon: int, window_bars: int, floor: float = 0.2) -> pd.Series:
    """Cost amortisation factor ``1 - rho_H`` from the causal lag-``H`` autocorrelation of the scores.

    A position is held as long as its signal persists. If scores keep a correlation ``rho_H`` over one
    horizon, the expected life of a position is ``H / (1 - rho_H)`` bars, so a one-off trading cost is
    spread over ``1 / (1 - rho_H)`` horizons of alpha. The single-period optimiser therefore charges
    ``cost * (1 - rho_H)`` -- the practical form of Garleanu & Pedersen's trade-toward-the-aim rule.
    """
    rho = rowwise_corr(score, score.shift(horizon))
    rho_s = rho.rolling(window_bars, min_periods=max(10, window_bars // 10)).mean()
    return (1.0 - rho_s).clip(lower=floor, upper=1.0).fillna(1.0)
