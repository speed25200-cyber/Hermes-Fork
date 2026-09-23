"""Forecast-quality and performance metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd

from hermes.validation.stats import probabilistic_sharpe, sharpe


def cross_sectional_ic(pred: pd.Series, target: pd.Series, method: str = "spearman", min_names: int = 5) -> pd.Series:
    """Per-timestamp correlation between predictions and realised targets (long format, level 0 = time)."""
    df = pd.DataFrame({"p": pred, "y": target}).dropna()
    if df.empty:
        return pd.Series(dtype=float)
    if method == "spearman":
        g = df.groupby(level=0)
        ranked = pd.DataFrame({"p": g["p"].rank(), "y": g["y"].rank()})
        df = ranked
    g = df.groupby(level=0)
    counts = g.size()
    mp = g["p"].transform("mean")
    my = g["y"].transform("mean")
    dp = df["p"] - mp
    dy = df["y"] - my
    num = (dp * dy).groupby(level=0).sum()
    den = np.sqrt((dp**2).groupby(level=0).sum() * (dy**2).groupby(level=0).sum())
    ic = num / den.replace(0, np.nan)
    return ic[counts >= min_names].dropna()


def ic_summary(ic: pd.Series, horizon: int = 1) -> dict[str, float]:
    """Mean IC, IC information ratio and a t-stat robust to label overlap and IC clustering.

    With a time index the t-stat is computed on **daily mean ICs** with a Newey-West long-run variance
    (Bartlett, automatic bandwidth, at least the horizon in days): overlapping ``h``-bar labels and
    persistent scores make intraday ICs strongly dependent, and a short kernel on bar-level ICs overstates
    significance. Without a time index, bar-level ICs are used with a kernel of ``2h`` lags.
    """
    s_ = ic.dropna()
    x = s_.to_numpy()
    n = len(x)
    if n < 10:
        return {"ic_mean": float("nan"), "ic_ir": float("nan"), "ic_t": float("nan"), "n": n}
    m, s = x.mean(), x.std(ddof=1)
    if isinstance(s_.index, pd.DatetimeIndex):
        daily = s_.groupby(s_.index.floor("D")).mean().to_numpy()
        per_day = n / max(len(daily), 1)
        lag = max(int(np.ceil(horizon / max(per_day, 1.0))), int(np.floor(4 * (len(daily) / 100.0) ** (2.0 / 9.0))))
        t = _nw_t(daily, lag) if len(daily) >= 10 else float("nan")
    else:
        t = _nw_t(x, 2 * horizon)
    return {
        "ic_mean": float(m),
        "ic_ir": float(m / s) if s > 0 else 0.0,
        "ic_t": float(t),
        "n": n,
        "ic_hit": float((x > 0).mean()),
    }


def _nw_t(x: np.ndarray, lag: int) -> float:
    """t-stat of the mean with a Newey-West (Bartlett) long-run variance."""
    n = len(x)
    xc = x - x.mean()
    lrv = xc @ xc / n
    for k in range(1, min(lag, n - 1) + 1):
        lrv += 2 * (1 - k / (lag + 1)) * (xc[k:] @ xc[:-k]) / n
    return float(x.mean() / np.sqrt(max(lrv, 1e-18) / n))


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def performance_summary(returns: pd.Series, periods_per_year: float) -> dict[str, float]:
    r = returns.dropna()
    if len(r) < 2:
        return {}
    eq = (1 + r).cumprod()
    years = len(r) / periods_per_year
    total = float(eq.iloc[-1] - 1)
    cagr = float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else -1.0
    vol = float(r.std(ddof=1) * np.sqrt(periods_per_year))
    downside = r[r < 0]
    sortino = float(r.mean() / (np.sqrt((downside**2).mean()) + 1e-18) * np.sqrt(periods_per_year))
    mdd = max_drawdown(eq)
    daily = (1 + r).groupby(r.index.floor("D")).prod() - 1 if isinstance(r.index, pd.DatetimeIndex) else r
    return {
        "total_return": total,
        "cagr": cagr,
        "ann_vol": vol,
        "sharpe": sharpe(r.to_numpy(), periods_per_year),
        "sharpe_daily": sharpe(daily.to_numpy(), 365.0) if len(daily) > 20 else float("nan"),
        "sortino": sortino,
        "max_drawdown": mdd,
        "calmar": float(cagr / abs(mdd)) if mdd < 0 else float("nan"),
        "psr_0": probabilistic_sharpe(daily.to_numpy()) if len(daily) > 20 else float("nan"),
        "hit_rate_daily": float((daily > 0).mean()) if len(daily) else float("nan"),
        "worst_day": float(daily.min()) if len(daily) else float("nan"),
        "best_day": float(daily.max()) if len(daily) else float("nan"),
        "n_days": float(len(daily)),
    }


def yearly_breakdown(returns: pd.Series, periods_per_year: float) -> pd.DataFrame:
    rows = []
    for year, r in returns.dropna().groupby(returns.dropna().index.year):
        s = performance_summary(r, periods_per_year)
        rows.append(
            {
                "year": int(year),
                "return": s.get("total_return"),
                "sharpe": s.get("sharpe"),
                "max_drawdown": s.get("max_drawdown"),
            }
        )
    return pd.DataFrame(rows).set_index("year") if rows else pd.DataFrame()
