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


def regime_scale(daily_close: pd.Series, drawdown: float, lookback_days: int, scale: float) -> pd.Series:
    """Per UTC day ``D``: ``scale`` if the reference contract's close of ``D-1`` was more than ``drawdown`` below
    its highest close over the ``lookback_days`` ending ``D-1``, else 1. Only closed days are read, so research
    and the live engine (which holds closed daily bars up to yesterday) gate the same days."""
    c = daily_close.astype(float).sort_index()
    if len(c):  # one more day: the live engine, which holds closes up to yesterday, reads today's value
        c = c.reindex(c.index.append(pd.DatetimeIndex([c.index[-1] + pd.Timedelta(days=1)])))
    if drawdown <= 0 or c.dropna().empty:
        return pd.Series(1.0, index=c.index, dtype=float)
    dd = c / c.rolling(lookback_days, min_periods=min(lookback_days, 20)).max() - 1.0
    on = (dd < -drawdown).shift(1, fill_value=False)  # day D is decided from the close of D-1
    return pd.Series(np.where(on, scale, 1.0), index=c.index, dtype=float)


def rowwise_corr(a: pd.DataFrame, b: pd.DataFrame, min_names: int = 5) -> pd.Series:
    """Per-row Pearson correlation over the columns where both frames are finite (few temporaries)."""
    b = b.reindex(index=a.index, columns=a.columns)
    A = a.to_numpy(dtype=np.float64, copy=True)
    B = b.to_numpy(dtype=np.float64, copy=True)
    m = np.isfinite(A) & np.isfinite(B)
    A[~m] = 0.0
    B[~m] = 0.0
    n = m.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        ma = A.sum(axis=1) / n
        mb = B.sum(axis=1) / n
        A -= ma[:, None]
        B -= mb[:, None]
        A[~m] = 0.0
        B[~m] = 0.0
        num = (A * B).sum(axis=1)
        den = np.sqrt((A * A).sum(axis=1) * (B * B).sum(axis=1))
        ic = np.where((den > 0) & (n >= min_names), num / den, np.nan)
    return pd.Series(ic, index=a.index)


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


def smooth_scores(score: pd.DataFrame, halflife_bars: float) -> pd.DataFrame:
    """Exponentially smoothed scores per contract (time-decayed across gaps), NaN where the raw score is.

    Short-horizon forecasts carry noise that flips from bar to bar; trading every flip costs more than the
    forecast is worth. Smoothing keeps the persistent part (Garleanu & Pedersen's "trade toward the aim"
    applied to the signal) and is computed identically in research and live.
    """
    if halflife_bars <= 0:
        return score
    return score.ewm(halflife=halflife_bars, ignore_na=False, min_periods=1).mean().where(score.notna())


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


def market_alpha_series(
    market_score: pd.Series,
    market_target: pd.Series,
    mkt_return: pd.Series,
    prior_ic: float | pd.Series,
    horizon: int,
    bars_per_day: int,
    prior_weight_obs: float = 30.0,
) -> pd.Series:
    """Expected market return over the horizon from a market-timing score (Grinold, causal IC).

    ``market_target[t]`` is the normalised market return over ``(t, t+h]``; it is only used once realised
    (shifted by the horizon). The score is z-scored on a trailing 90-day window.
    """
    H = horizon
    win = bars_per_day * 90
    known_y = market_target.shift(H)
    known_s = market_score.shift(H)
    rc = known_s.rolling(win, min_periods=win // 3).corr(known_y)
    n_eff = known_s.notna().astype(float).rolling(win, min_periods=1).sum() / H
    prior = prior_ic if isinstance(prior_ic, pd.Series) else pd.Series(prior_ic, index=market_score.index)
    ic = ((n_eff * rc.fillna(0) + prior_weight_obs * prior.fillna(0)) / (n_eff + prior_weight_obs)).clip(0, 0.2)
    mu = market_score.rolling(win, min_periods=24).mean()
    sd = market_score.rolling(win, min_periods=24).std()
    z = ((market_score - mu) / sd).clip(-3, 3)
    mvol = np.sqrt((mkt_return**2).ewm(halflife=max(2, bars_per_day), adjust=False).mean())
    return (ic * z * mvol * np.sqrt(H)).fillna(0.0)
