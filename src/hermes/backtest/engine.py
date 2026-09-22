"""Bar-by-bar portfolio simulator with realistic frictions.

Timeline of bar ``t`` (all decisions strictly causal):

1. positions decided at ``close(t-1)`` earn the return of bar ``t`` and pay the funding settled in bar ``t``;
   weights then drift with prices;
2. the risk overlay observes the new equity (peak, UTC day, drawdown halt);
3. at ``close(t)`` (every ``rebalance_every`` bars) the constructor proposes target weights from the
   score of row ``t``; the overlay constrains them; the trade pays fees + spread + square-root impact.

Contracts that leave the universe or stop trading are exited. The engine is the same code path the live
engine uses for its decision (``PortfolioConstructor`` + ``RiskOverlay``), only the fills are simulated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from hermes.config import HermesConfig
from hermes.data.panel import Panel
from hermes.portfolio.alpha import cs_zscore
from hermes.portfolio.construct import BookInputs, PortfolioConstructor
from hermes.portfolio.costs import CostModel
from hermes.portfolio.covariance import EwmaCovariance, market_variance
from hermes.risk.overlay import RiskOverlay
from hermes.validation.metrics import performance_summary, yearly_breakdown


@dataclass
class SignalBundle:
    score: pd.DataFrame  # raw model scores (time x symbol), NaN outside the universe
    ic_est: pd.Series  # causal IC estimate used for sizing
    market_alpha: pd.Series | None = None  # expected market return over the horizon (None = neutral)
    cost_scale: pd.Series | None = None  # cost amortisation factor per bar (1 = no amortisation)


@dataclass
class BacktestResult:
    returns: pd.Series
    equity: pd.Series
    stats: pd.DataFrame  # per-bar diagnostics
    weights: pd.DataFrame | None
    risk_events: list[tuple[str, str]] = field(default_factory=list)

    def summary(self, periods_per_year: float) -> dict[str, float]:
        s = performance_summary(self.returns, periods_per_year)
        st = self.stats
        years = len(self.returns) / periods_per_year
        s.update(
            {
                "avg_gross": float(st["gross"].mean()),
                "avg_net": float(st["net"].mean()),
                "avg_abs_beta": float(st["beta_exposure"].abs().mean()),
                "turnover_annual": float(st["turnover"].sum() / max(years, 1e-9)),
                "fees_annual": float(st["fees"].sum() / max(years, 1e-9)),
                "spread_annual": float(st["spread"].sum() / max(years, 1e-9)),
                "impact_annual": float(st["impact"].sum() / max(years, 1e-9)),
                "funding_annual": float(st["funding"].sum() / max(years, 1e-9)),
                "gross_pnl_annual": float(st["gross_pnl"].sum() / max(years, 1e-9)),
                "avg_positions": float(st["n_positions"].mean()),
                "avg_ic_est": float(st["ic_est"].mean()),
            }
        )
        return s

    def yearly(self, periods_per_year: float) -> pd.DataFrame:
        return yearly_breakdown(self.returns, periods_per_year)


def run_backtest(
    panel: Panel,
    mask: pd.DataFrame,
    aux: dict[str, pd.DataFrame],
    signal: SignalBundle,
    cfg: HermesConfig,
    capital: float = 100_000.0,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    ic_ref: float | None = None,
    record_weights: bool = False,
) -> BacktestResult:
    bpd = cfg.bars_per_day
    bpy = cfg.bars_per_year
    pc = cfg.portfolio
    index = panel.index
    t0 = 0 if start is None else int(index.searchsorted(start))
    t1 = len(index) if end is None else int(index.searchsorted(end, side="right"))

    close = panel["close"]
    r = (close / close.shift(1) - 1.0).to_numpy()
    fund = panel["funding_rate"].fillna(0.0).to_numpy() if "funding_rate" in panel else np.zeros_like(r)
    member = mask.to_numpy() & close.notna().to_numpy()
    z = cs_zscore(signal.score, mask).to_numpy()
    ivol = aux["ivol"].to_numpy()
    beta = aux["beta"].to_numpy()
    mkt_var = market_variance(aux["mkt"]["mkt"], pc.cov_halflife // 4).to_numpy()
    ic = signal.ic_est.reindex(index).fillna(0.0).to_numpy()
    malpha = signal.market_alpha.reindex(index).fillna(0.0).to_numpy() if signal.market_alpha is not None else None
    cscale = signal.cost_scale.reindex(index).fillna(1.0).to_numpy() if signal.cost_scale is not None else None
    costs = CostModel.from_panel(cfg.costs, panel["high"], panel["low"], close, panel["quote_volume"], aux["vol"], bpd)
    daily_close = close.resample("1D").last()
    daily_ret = daily_close / daily_close.shift(1) - 1.0
    day_pos = daily_ret.index.searchsorted(index.floor("D"))  # index of the current day in daily_ret
    daily_np = daily_ret.to_numpy()

    constructor = PortfolioConstructor(pc, bpy, ic_ref if ic_ref is not None else pc.ic_ref)
    overlay = RiskOverlay(cfg.risk)
    N = close.shape[1]
    ewma = EwmaCovariance(N, pc.cov_halflife)
    for t in range(max(0, t0 - pc.cov_halflife * 2), t0):
        ewma.update(np.where(member[t], r[t], np.nan))

    w = np.zeros(N)
    equity = capital
    n_out = t1 - t0
    out = {
        k: np.zeros(n_out)
        for k in (
            "ret",
            "gross_pnl",
            "funding",
            "fees",
            "spread",
            "impact",
            "turnover",
            "gross",
            "net",
            "beta_exposure",
            "n_positions",
            "vol_ex_ante",
            "budget",
            "ic_est",
            "es_1d",
        )
    }
    W = np.zeros((n_out, N), dtype=np.float32) if record_weights else None
    hist_cache: dict[int, np.ndarray] = {}

    for k, t in enumerate(range(t0, t1)):
        ts = index[t]
        # 1) P&L of bar t on weights held since close(t-1)
        rt = r[t]
        held = w != 0
        valid = np.isfinite(rt)
        gross_pnl = float(np.sum(w[valid & held] * rt[valid & held]))
        fpay = float(np.sum(w[held] * np.nan_to_num(fund[t][held])))
        pnl = gross_pnl - fpay
        equity_prev = equity
        equity *= 1.0 + pnl
        if equity <= 0:
            out["ret"][k] = -1.0
            overlay.halt(ts, "equity wiped out")
            equity = 0.0
            break
        grow = np.where(valid, 1.0 + np.nan_to_num(rt), 1.0)
        w = w * grow / (1.0 + pnl)
        w[held & ~valid] = 0.0  # contract stopped trading: exited at the last price
        ewma.update(np.where(member[t], rt, np.nan))
        overlay.observe(ts, equity)
        out["gross_pnl"][k] = gross_pnl
        out["funding"][k] = -fpay

        # 2) rebalance at close(t)
        fees = spread = impact = turnover = 0.0
        if (t - t0) % pc.rebalance_every == 0:
            active = member[t] & np.isfinite(z[t])
            idx = np.nonzero(active | (w != 0))[0]
            if len(idx):
                score = np.where(active[idx], z[t, idx], np.nan)
                dollars_typ = np.abs(w[idx]).mean() * equity + 1.0
                inp = BookInputs(
                    score=score,
                    ivol=ivol[t, idx],
                    beta=beta[t, idx],
                    mkt_var=float(mkt_var[t]) if np.isfinite(mkt_var[t]) else 1e-4,
                    cost_rate=costs.linear_rate(t, dollars_typ)[idx] * (cscale[t] if cscale is not None else 1.0),
                    adv=costs.adv.iloc[t].to_numpy()[idx],
                    w0=w[idx],
                    ic=float(ic[t]),
                    market_alpha=float(malpha[t]) if malpha is not None else 0.0,
                    sample_cov=ewma.matrix(idx),
                )
                book = constructor.target(inp, equity)
                d = int(day_pos[t])
                if d not in hist_cache:
                    lo = max(0, d - 180)
                    hist_cache = {d: np.nan_to_num(daily_np[lo:d])}
                target, info = overlay.apply(ts, equity, book.weights, w[idx], book.cov_bar, bpd, hist_cache[d][:, idx])
                trade = target - w[idx]
                trade[np.abs(trade) < 1e-9] = 0.0
                if np.any(trade):
                    dollars = np.zeros(N)
                    dollars[idx] = trade * equity
                    fees, spread, impact = costs.trade_cost(t, dollars)
                    turnover = float(np.abs(trade).sum())
                    w[idx] = target
                    equity -= fees + spread + impact
                out["vol_ex_ante"][k] = book.ex_ante_vol_annual
                out["budget"][k] = info.get("budget", 1.0)
                out["es_1d"][k] = info.get("es_1d", 0.0)
        out["ret"][k] = equity / equity_prev - 1.0
        out["fees"][k] = fees / max(equity_prev, 1e-9)
        out["spread"][k] = spread / max(equity_prev, 1e-9)
        out["impact"][k] = impact / max(equity_prev, 1e-9)
        out["turnover"][k] = turnover
        out["gross"][k] = float(np.abs(w).sum())
        out["net"][k] = float(w.sum())
        bt = np.nan_to_num(beta[t], nan=1.0)
        out["beta_exposure"][k] = float(w @ bt)
        out["n_positions"][k] = float(np.count_nonzero(w))
        out["ic_est"][k] = float(ic[t])
        if W is not None:
            W[k] = w

    idx_out = index[t0:t1]
    stats = pd.DataFrame(out, index=idx_out)
    returns = stats["ret"]
    equity_curve = capital * (1 + returns).cumprod()
    weights = pd.DataFrame(W, index=idx_out, columns=close.columns) if W is not None else None
    return BacktestResult(
        returns=returns, equity=equity_curve, stats=stats, weights=weights, risk_events=list(overlay.state.events)
    )
