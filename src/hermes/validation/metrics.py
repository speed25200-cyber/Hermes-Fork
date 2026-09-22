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
    """Mean IC, IC information ratio and a t-stat corrected for label overlap (Newey-West-like, lag=h)."""
    x = ic.dropna().to_numpy()
    n = len(x)
    if n < 10:
        return {"ic_mean": float("nan"), "ic_ir": float("nan"), "ic_t": float("nan"), "n": n}
    m, s = x.mean(), x.std(ddof=1)
    xc = x - m
    lrv = xc @ xc / n
    for k in range(1, min(horizon, n - 1) + 1):
        lrv += 2 * (1 - k / (horizon + 1)) * (xc[k:] @ xc[:-k]) / n
    se = np.sqrt(max(lrv, 1e-18) / n)
    return {
        "ic_mean": float(m),
        "ic_ir": float(m / s) if s > 0 else 0.0,
        "ic_t": float(m / se),
        "n": n,
        "ic_hit": float((x > 0).mean()),
    }


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
