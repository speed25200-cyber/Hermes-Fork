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


@dataclass
class _Context:
    columns: pd.Index
    r: np.ndarray
    fund: np.ndarray
    member: np.ndarray
    ivol: np.ndarray
    beta: np.ndarray
    mkt_var: np.ndarray
    costs: CostModel
    open_: np.ndarray  # bar open / high / low: exchange-side stops trigger inside the bar
    high: np.ndarray
    low: np.ndarray
    day_pos: np.ndarray
    daily_np: np.ndarray
    refs: tuple[object, ...] = ()  # keeps the keyed objects alive so their ids cannot be reused


# Heavy per-panel preparation, memoised: the evaluation runs dozens of backtests (null, grid, stress) on the
# same panel, and forked workers inherit the parent's cache instead of recomputing it.
_CONTEXTS: dict[tuple[object, ...], _Context] = {}


def _context(
    panel: Panel, mask: pd.DataFrame, aux: dict[str, pd.DataFrame], cfg: HermesConfig, t0: int, t1: int
) -> _Context:
    cov_hl = cfg.days(cfg.portfolio.cov_halflife_days)
    key = (id(panel), id(mask), id(aux), t0, t1, cfg.costs, cov_hl, cfg.bars_per_day)
    if key in _CONTEXTS:
        return _CONTEXTS[key]
    bpd = cfg.bars_per_day
    lo = max(0, t0 - cov_hl * 2)
    # Only contracts that are universe members at some point of the simulated window can ever be held.
    cols = mask.columns[mask.iloc[lo:t1].to_numpy().any(axis=0)]
    close = panel["close"][cols]
    r = (close / close.shift(1) - 1.0).to_numpy()
    fund = panel["funding_rate"][cols].fillna(0.0).to_numpy() if "funding_rate" in panel else np.zeros_like(r)
    member = mask[cols].to_numpy() & close.notna().to_numpy()
    costs = CostModel.from_panel(
        cfg.costs, panel["high"][cols], panel["low"][cols], close, panel["quote_volume"][cols], aux["vol"][cols], bpd
    )
    daily_close = close.resample("1D").last()
    daily_ret = daily_close / daily_close.shift(1) - 1.0
    ctx = _Context(
        columns=cols,
        r=r,
        fund=fund,
        member=member,
        ivol=aux["ivol"][cols].to_numpy(),
        beta=aux["beta"][cols].to_numpy(),
        mkt_var=market_variance(aux["mkt"]["mkt"], max(2, cov_hl // 4)).to_numpy(),
        costs=costs,
        open_=panel["open"][cols].to_numpy(),
        high=panel["high"][cols].to_numpy(),
        low=panel["low"][cols].to_numpy(),
        day_pos=daily_ret.index.searchsorted(panel.index.floor("D")),
        daily_np=daily_ret.to_numpy(),
        refs=(panel, mask, aux),
    )
    if len(_CONTEXTS) > 4:
        _CONTEXTS.clear()
    _CONTEXTS[key] = ctx
    return ctx


def is_rebalance_bar(index: pd.DatetimeIndex, bar: pd.Timedelta, every: int) -> np.ndarray:
    """Decision bars: every ``every`` bars on a clock-aligned grid (same bars in research and live)."""
    if every <= 1:
        return np.ones(len(index), dtype=bool)
    return (index.as_unit("ns").asi8 // bar.value) % every == 0  # pandas may store us: compare in ns


def run_backtest(*args: object, **kwargs: object) -> BacktestResult:
    """Simulate (see ``_run_backtest``) with single-threaded BLAS: the matrices are tiny and parallelism
    comes from running several backtests in separate processes; threaded BLAS only adds contention."""
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=1):
        return _run_backtest(*args, **kwargs)  # type: ignore[arg-type]


def _run_backtest(
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
    cost_multiplier: float = 1.0,
) -> BacktestResult:
    """``cost_multiplier`` scales what trades actually pay, not what the optimiser expects (cost stress)."""
    bpd = cfg.bars_per_day
    bpy = cfg.bars_per_year
    pc = cfg.portfolio
    index = panel.index
    t0 = 0 if start is None else int(index.searchsorted(start))
    t1 = len(index) if end is None else int(index.searchsorted(end, side="right"))

    ctx = _context(panel, mask, aux, cfg, t0, t1)
    cols = ctx.columns
    close = panel["close"][cols]
    r, fund, member, ivol, beta = ctx.r, ctx.fund, ctx.member, ctx.ivol, ctx.beta
    mkt_var, costs, day_pos, daily_np = ctx.mkt_var, ctx.costs, ctx.day_pos, ctx.daily_np
    open_, high, low = ctx.open_, ctx.high, ctx.low
    k_stop = cfg.risk.stop_loss_daily_sigmas
    stop_px = np.full(close.shape[1], np.nan)  # catastrophe stop of each open position (as placed on OKX)
    prev_side = np.zeros(close.shape[1])
    close_np = close.to_numpy()
    z = cs_zscore(signal.score[cols], mask[cols]).to_numpy()
    ic = signal.ic_est.reindex(index).fillna(0.0).to_numpy()
    malpha = signal.market_alpha.reindex(index).fillna(0.0).to_numpy() if signal.market_alpha is not None else None
    cscale = signal.cost_scale.reindex(index).fillna(1.0).to_numpy() if signal.cost_scale is not None else None

    constructor = PortfolioConstructor(pc, bpy, ic_ref if ic_ref is not None else pc.ic_ref)
    overlay = RiskOverlay(cfg.risk, check_kill_file=False)
    day_keys = index.floor("D").asi8
    rebalance_bar = is_rebalance_bar(index, panel.bar_delta, pc.rebalance_every)
    N = close.shape[1]
    cov_hl = cfg.days(pc.cov_halflife_days)
    ewma = EwmaCovariance(N, cov_hl)
    for t in range(max(0, t0 - cov_hl * 2), t0):
        ewma.update(np.where(member[t], r[t], np.nan))

    w = np.zeros(N)
    equity = capital
    n_out = t1 - t0
    out = {
        k: np.zeros(n_out)
        for k in (
            "ret",
            "gross_pnl",
            "pnl_long",
            "pnl_short",
            "stops",
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
        hit = np.zeros(len(w), dtype=bool)
        if k_stop > 0 and held.any() and t > 0:
            # A stop placed at close(t-1) triggers on the bar's range; a gap through it fills at the open.
            with np.errstate(invalid="ignore"):
                hit_long = (w > 0) & (low[t] <= stop_px)
                hit_short = (w < 0) & (high[t] >= stop_px)
            hit = (hit_long | hit_short) & np.isfinite(stop_px)
            if hit.any():
                exit_px = np.where(
                    hit_long,
                    np.minimum(stop_px, np.nan_to_num(open_[t], nan=np.inf)),
                    np.maximum(stop_px, np.nan_to_num(open_[t], nan=-np.inf)),
                )
                rt = np.where(hit, exit_px / close_np[t - 1] - 1.0, rt)
        valid = np.isfinite(rt)
        contrib = np.where(valid & held, w * np.nan_to_num(rt), 0.0)
        gross_pnl = float(contrib.sum())
        pnl_long, pnl_short = float(contrib[w > 0].sum()), float(contrib[w < 0].sum())
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
        stop_fees = stop_spread = stop_impact = stop_turn = 0.0
        if hit.any():
            stop_fees, stop_spread, stop_impact = (
                cost_multiplier * x for x in costs.taker_cost(t, np.where(hit, w, 0.0) * equity)
            )
            stop_turn = float(np.abs(w[hit]).sum())
            equity -= stop_fees + stop_spread + stop_impact
            w[hit] = 0.0
            stop_px[hit] = np.nan
            out["stops"][k] = float(hit.sum())
        ewma.update(np.where(member[t], rt, np.nan))
        overlay.observe(ts, equity, day=int(day_keys[t]))
        out["gross_pnl"][k] = gross_pnl
        out["pnl_long"][k] = pnl_long  # price P&L of each leg (before costs and funding)
        out["pnl_short"][k] = pnl_short
        out["funding"][k] = -fpay

        # 2) rebalance at close(t)
        fees = spread = impact = turnover = 0.0
        if rebalance_bar[t]:
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
                    adv=costs.adv_at(t)[idx],
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
                    fees, spread, impact = (cost_multiplier * x for x in costs.trade_cost(t, dollars))
                    turnover = float(np.abs(trade).sum())
                    w[idx] = target
                    equity -= fees + spread + impact
                out["vol_ex_ante"][k] = book.ex_ante_vol_annual
                out["budget"][k] = info.get("budget", 1.0)
                out["es_1d"][k] = info.get("es_1d", 0.0)
        # Stops carried by the positions now held, as the live broker places them: one per position, k daily
        # sigmas from the price at which it was opened; a resized position keeps its stop, a flip gets a new one.
        if k_stop > 0:
            side = np.sign(w)
            new = (side != 0) & ((side != prev_side) | ~np.isfinite(stop_px))
            frac = np.clip(k_stop * costs._sig[t], 0.03, 0.5)
            stop_px = np.where(new, close_np[t] * (1.0 - side * frac), stop_px)
            stop_px[side == 0] = np.nan
            prev_side = side
        fees, spread, impact = fees + stop_fees, spread + stop_spread, impact + stop_impact
        turnover += stop_turn
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
