"""Out-of-sample evaluation and the promotion gate.

Everything here consumes walk-forward (out-of-fold) predictions only. The gate is written *before* looking
at results and is deliberately hard to pass: a strategy is promoted to real money only if its net-of-cost
performance is (1) better than the same machinery fed with block-permuted scores, (2) significant after
deflating for the number of configurations tried, (3) not the product of choosing the best of a grid
(PBO), (4) robust to doubled costs and one bar of extra latency, and (5) positive in most years.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from hermes.backtest.engine import BacktestResult, SignalBundle, run_backtest
from hermes.config import HermesConfig
from hermes.portfolio.alpha import (
    cs_zscore,
    estimate_ic,
    market_alpha_series,
    rowwise_corr,
    signal_persistence,
    smooth_scores,
)
from hermes.research.dataset import Dataset
from hermes.research.walkforward import WalkForwardResult
from hermes.validation.metrics import ic_summary, rank_ic_wide
from hermes.validation.stats import (
    deflated_sharpe,
    min_track_record,
    pbo,
    probabilistic_sharpe,
    sharpe,
    spa_test,
    stationary_bootstrap,
)

log = logging.getLogger(__name__)


def holding_target(ds: Dataset, cfg: HermesConfig) -> tuple[int, pd.DataFrame]:
    H = cfg.portfolio.holding_horizon
    if H not in ds.targets.residual:
        H = min(ds.targets.residual, key=lambda h: abs(h - H))
    return H, ds.targets.residual[H]


def make_signal(
    score: pd.DataFrame,
    ds: Dataset,
    prior_ic: pd.Series,
    cfg: HermesConfig,
    market_score: pd.Series | None = None,
    market_prior: pd.Series | None = None,
) -> SignalBundle:
    H, target = holding_target(ds, cfg)
    score = traded_score(score, cfg, H)
    ric = rowwise_corr(score, target)
    ic_est = estimate_ic(ric, H, prior_ic, halflife_bars=cfg.bars_per_day * 30)
    malpha = None
    if market_score is not None and market_prior is not None:
        malpha = market_alpha(market_score, market_prior, ds, cfg)
    persist = signal_persistence(score, H, cfg.bars_per_day * 30, floor=cfg.portfolio.cost_scale_floor)
    return SignalBundle(score=score, ic_est=ic_est, market_alpha=malpha, cost_scale=persist)


def traded_score(score: pd.DataFrame, cfg: HermesConfig, horizon: int) -> pd.DataFrame:
    """The score the book is built from: the model score, smoothed over ``signal_halflife`` horizons."""
    return smooth_scores(score, cfg.portfolio.signal_halflife * horizon)


def market_alpha(market_score: pd.Series, market_prior: pd.Series, ds: Dataset, cfg: HermesConfig) -> pd.Series:
    H, _ = holding_target(ds, cfg)
    return market_alpha_series(
        market_score, ds.targets.market[H], ds.feats.aux["mkt"]["mkt"], market_prior, H, cfg.bars_per_day
    )


def block_permute(score: pd.DataFrame, mask: pd.DataFrame, seed: int, block_days: int = 7) -> pd.DataFrame:
    """Null score: within each block, every contract takes the score path of a fixed random partner.

    Blocks follow the universe's calendar reselection grid (``block_days`` from the same Monday anchor), so
    membership is constant inside a block and the partner map never shuffles mid-block (a reshuffle would add
    artificial turnover and costs to the null book, making the real one look better than it is). The map is a
    random cyclic derangement of the block's members: no contract keeps its own score. The time structure of
    each score path -- hence realistic turnover -- is preserved; its alignment with the right contract is not.
    """
    from hermes.data.universe import EPOCH

    rng = np.random.default_rng(seed)
    S = score.to_numpy()
    M = mask.to_numpy() & np.isfinite(S)
    out = np.full_like(S, np.nan)
    block = np.asarray((score.index.floor("D") - EPOCH).days // block_days)
    starts = np.flatnonzero(np.r_[True, block[1:] != block[:-1]])
    ends = np.r_[starts[1:], len(block)]
    for b0, b1 in zip(starts, ends):
        members = np.flatnonzero(M[b0:b1].any(axis=0))
        if len(members) < 2:
            continue
        order = members[rng.permutation(len(members))]
        partner = np.empty(S.shape[1], dtype=np.int64)
        partner[order] = np.roll(order, -1)
        for t in range(b0, b1):
            mem = np.flatnonzero(M[t])
            src = partner[mem]
            ok = M[t, src]  # a partner that is not a member at t (delisting, gap) gives no score this bar
            out[t, mem[ok]] = S[t, src[ok]]
    return pd.DataFrame(out, index=score.index, columns=score.columns)


# -- parallel backtests (fork start method shares the dataset copy-on-write) -----------------------------------
_CTX: dict[str, object] = {}


def _bt_job(args: tuple[str, int | dict[str, object]]) -> tuple[str, pd.Series, dict[str, float]]:
    kind, param = args
    ds: Dataset = _CTX["ds"]  # type: ignore[assignment]
    wf: WalkForwardResult = _CTX["wf"]  # type: ignore[assignment]
    cfg: HermesConfig = _CTX["cfg"]  # type: ignore[assignment]
    start = _CTX["start"]
    if kind == "null":
        score = block_permute(wf.score, ds.mask, seed=int(param), block_days=cfg.data.universe.reselect_every_days)  # type: ignore[arg-type]
        sig = make_signal(score, ds, wf.prior_ic, cfg)
        c = cfg
    else:
        c = cfg.model_copy(update={"portfolio": cfg.portfolio.model_copy(update=param)})  # type: ignore[arg-type]
        if kind == "nohalt":
            # Diagnostic only (never gated): the signal's economics over the whole period, with the drawdown
            # and daily-loss controls that would stop a losing book switched off.
            risk = cfg.risk.model_copy(update={"drawdown_soft": 0.98, "drawdown_hard": 0.99, "daily_loss_limit": 1.0})
            c = c.model_copy(update={"risk": risk})
        score = wf.score.shift(1) if kind == "lag1" else wf.score
        if kind == "market":
            sig = make_signal(score, ds, wf.prior_ic, c, wf.market_score, wf.market_prior_ic)
        else:
            sig = make_signal(score, ds, wf.prior_ic, c)
    # Cost stress: the book is built on the usual cost estimates, but every trade pays twice as much
    # (fees, spread and impact) -- an execution that turns out worse than modelled, not a re-optimised book.
    mult = 2.0 if kind == "costx2" else 1.0
    bt = run_backtest(ds.panel, ds.mask, ds.feats.aux, sig, c, start=start, cost_multiplier=mult)  # type: ignore[arg-type]
    daily = (1 + bt.returns).groupby(bt.returns.index.floor("D")).prod() - 1
    return f"{kind}:{param}", daily, bt.summary(c.bars_per_year)


def _run_parallel(
    jobs: list[tuple[str, int | dict[str, object]]], workers: int
) -> list[tuple[str, pd.Series, dict[str, float]]]:
    if workers <= 1 or len(jobs) <= 1:
        out = []
        for i, j in enumerate(jobs):
            out.append(_bt_job(j))
            log.info("backtest %d/%d done (%s)", i + 1, len(jobs), out[-1][0])
        return out
    import multiprocessing as mp
    from concurrent.futures.process import BrokenProcessPool

    done: dict[int, tuple[str, pd.Series, dict[str, float]]] = {}
    try:
        with ProcessPoolExecutor(workers, mp_context=mp.get_context("fork")) as ex:
            futures = {ex.submit(_bt_job, j): i for i, j in enumerate(jobs)}
            for fut, i in futures.items():
                try:
                    done[i] = fut.result()
                    log.info("backtest %d/%d done (%s)", len(done), len(jobs), done[i][0])
                except BrokenProcessPool:
                    break
    except BrokenProcessPool:
        pass
    missing = [i for i in range(len(jobs)) if i not in done]
    if missing:
        # A worker died (typically out of memory): finish the remaining jobs one at a time in this process.
        log.warning("process pool broken, running %d remaining backtests sequentially", len(missing))
        for i in missing:
            done[i] = _bt_job(jobs[i])
    return [done[i] for i in range(len(jobs))]


def _workers_for_memory(per_worker_gb: float = 2.5) -> int:
    """Parallel backtests that fit in the available memory (Linux; falls back to 2)."""
    try:
        with open("/proc/meminfo") as fh:
            info = {line.split(":")[0]: float(line.split()[1]) for line in fh}
        avail_gb = info.get("MemAvailable", 0.0) / 1e6
    except OSError:
        return 2
    return max(1, int(avail_gb // per_worker_gb))


def trial_count_and_variance(ledger_trials: int, grid_daily: pd.DataFrame, n_days: int) -> tuple[int, float]:
    """Trials and cross-trial Sharpe variance for the Deflated Sharpe Ratio.

    * The robustness grid's variants are strongly correlated: they count as ``N_eff = rho + (1 - rho) N``
      effective trials (``rho`` their mean pairwise correlation), times the configurations in the ledger.
    * The Sharpe dispersion across trials is never taken below the sampling variance of one daily Sharpe
      estimate under the null (``1 / (T - 1)``): near-identical grid variants would otherwise make the
      expected best-of-N Sharpe, hence the deflation, vanish.
    """
    n_grid = grid_daily.shape[1]
    null_var = 1.0 / max(n_days - 1, 1)
    if n_grid < 2 or len(grid_daily) < 10:
        return max(1, ledger_trials), null_var
    corr = grid_daily.corr().to_numpy()
    pairs = corr[np.triu_indices(n_grid, 1)]
    pairs = pairs[np.isfinite(pairs)]  # a flat variant (no trade) has no correlation: it counts as independent
    rho = float(np.clip(pairs.mean(), 0.0, 1.0)) if len(pairs) else 0.0
    n_eff = rho + (1.0 - rho) * n_grid
    per_sr = grid_daily.mean() / grid_daily.std()
    var = float(per_sr.var()) if np.isfinite(per_sr.var()) else 0.0
    return max(1, round(ledger_trials * n_eff)), max(var, null_var)


def positive_year_fraction(daily: pd.Series, min_days: int = 90) -> float:
    """Share of calendar years with a positive return; stub years shorter than ``min_days`` do not vote."""
    d = daily.dropna()
    if d.empty:
        return 0.0
    by_year = (1 + d).groupby(d.index.year).agg(["prod", "size"])
    full = by_year[by_year["size"] >= min_days]
    use = full if len(full) else by_year
    return float(((use["prod"] - 1) > 0).mean())


def _stopped_at(bt: BacktestResult) -> str | None:
    """When the main backtest stopped trading: a formal halt, or the drawdown budget falling below 5 %."""
    halt = next((str(ts) for ts, msg in bt.risk_events if msg.startswith("HALT")), None)
    low = bt.stats.index[bt.stats["budget"].between(1e-12, 0.05)]
    exhausted = str(low[0]) if len(low) else None
    return min((x for x in (halt, exhausted) if x), default=None)


@dataclass
class Evaluation:
    ic: dict[str, object]
    summary: dict[str, float]
    yearly: pd.DataFrame
    tests: dict[str, float]
    gate: dict[str, dict[str, object]]
    promoted: bool
    grid: pd.DataFrame
    null_sharpes: list[float] = field(default_factory=list)
    halted_at: str | None = None  # hard drawdown halt of the main backtest (the book stops trading)
    nohalt: dict[str, object] = field(default_factory=dict)  # diagnostic without drawdown controls

    def to_dict(self) -> dict[str, object]:
        return {
            "ic": self.ic,
            "summary": self.summary,
            "yearly": self.yearly.reset_index().to_dict(orient="records"),
            "tests": self.tests,
            "gate": self.gate,
            "promoted": self.promoted,
            "grid": self.grid.reset_index().to_dict(orient="records"),
            "null_sharpes": self.null_sharpes,
            "halted_at": self.halted_at,
            "nohalt": self.nohalt,
        }


def evaluation_window(ds: Dataset, wf: WalkForwardResult, cfg: HermesConfig) -> tuple[Dataset, WalkForwardResult]:
    """Dataset and predictions restricted to what the evaluation reads: the out-of-sample period plus the
    look-back the backtest needs before it (181 days of daily returns for the historical ES, covariance
    warm-up), and the contracts that are members there. Same results, a fraction of the memory per worker."""
    from dataclasses import replace

    from hermes.features.library import FeatureSet
    from hermes.labels.targets import Targets

    idx = ds.mask.index
    t0 = int(idx.searchsorted(wf.oof_start))
    warm = max(2 * cfg.days(cfg.portfolio.cov_halflife_days), cfg.days(181)) + cfg.bars_per_day
    lo = max(0, t0 - warm)
    if lo == 0:
        return ds, wf
    all_cols = ds.mask.columns
    cols = all_cols[ds.mask.iloc[lo:].to_numpy().any(axis=0)]

    def frame(x: pd.DataFrame) -> pd.DataFrame:
        x = x.iloc[lo:]
        return x[cols] if list(x.columns) == list(all_cols) else x

    feats = FeatureSet(
        frames={},
        market={k: v.iloc[lo:] for k, v in ds.feats.market.items()},
        aux={k: frame(v) for k, v in ds.feats.aux.items()},
    )
    targets = Targets(
        residual={h: frame(x) for h, x in ds.targets.residual.items()},
        total={},
        market={h: x.iloc[lo:] for h, x in ds.targets.market.items()},
        horizon=ds.targets.horizon,
    )
    ds2 = replace(
        ds, panel=ds.panel.iloc(slice(lo, None)).subset(list(cols)), mask=frame(ds.mask), feats=feats, targets=targets
    )
    return ds2, wf.restricted_to(idx[lo:], list(cols))


def evaluate(
    ds: Dataset,
    wf: WalkForwardResult,
    cfg: HermesConfig,
    n_null: int | None = None,
    workers: int | None = None,
    grid: list[dict[str, object]] | None = None,
    restrict: bool = True,
) -> tuple[Evaluation, BacktestResult]:
    if restrict:
        ds, wf = evaluation_window(ds, wf, cfg)
    v = cfg.validation
    bpy = cfg.bars_per_year
    start = wf.oof_start
    # Each worker holds a few (bar x contract) float64 arrays of the backtest window on top of the shared data.
    n_cols = int(ds.mask.iloc[ds.mask.index.searchsorted(start) :].to_numpy().any(axis=0).sum())
    per_worker = max(1.0, 1.5 * 10 * 8 * len(ds.mask.index) * max(n_cols, 1) / 1e9)  # ~10 live float64 arrays
    workers = workers or max(1, min(os.cpu_count() or 2, _workers_for_memory(per_worker)))
    log.info("evaluation: %d parallel backtests (%.1f GB each, %d contracts)", workers, per_worker, n_cols)
    n_null = n_null if n_null is not None else min(v.null_permutations, 40)

    # --- forecast quality -------------------------------------------------------------------------------
    ic: dict[str, object] = {}
    for h, tgt in ds.targets.residual.items():
        ic[f"h{h}"] = ic_summary(rank_ic_wide(wf.score, tgt), horizon=h)
    for name, sc in wf.model_scores.items():
        H, tgt = holding_target(ds, cfg)
        ic[f"model_{name}_h{H}"] = ic_summary(rank_ic_wide(sc, tgt), horizon=H)
    H, tgt = holding_target(ds, cfg)
    realized = rowwise_corr(traded_score(wf.score, cfg, H), tgt).dropna()
    by_year = realized.groupby(realized.index.year).mean()
    ic["by_year"] = {int(k): round(float(x), 4) for k, x in by_year.items()}
    if wf.market_score is not None:
        ms, my = wf.market_score, ds.targets.market[H]
        ok = ms.notna() & my.notna()
        if ok.sum() > 100:
            c = float(np.corrcoef(ms[ok], my[ok])[0, 1])
            n_ind = ok.sum() / H
            t_stat = c * np.sqrt(max(n_ind - 2, 1))
            yearly_c = (
                pd.concat([ms[ok], my[ok]], axis=1)
                .groupby(ms[ok].index.year)
                .apply(lambda g: g.iloc[:, 0].corr(g.iloc[:, 1]))
            )
            pos_years = float((yearly_c > 0).mean()) if len(yearly_c) else 0.0
            ic["market_timing"] = {
                "corr": round(c, 4),
                "t": round(float(t_stat), 2),
                "by_year": {int(k): round(float(v_), 4) for k, v_ in yearly_c.items()},
                "gate": bool(t_stat >= 2.5 and pos_years >= 0.6),
            }

    # --- main backtest ------------------------------------------------------------------------------------
    sig = make_signal(wf.score, ds, wf.prior_ic, cfg)
    bt = run_backtest(ds.panel, ds.mask, ds.feats.aux, sig, cfg, start=start, record_weights=True)
    summary = bt.summary(bpy)
    daily = (1 + bt.returns).groupby(bt.returns.index.floor("D")).prod() - 1
    yearly = bt.yearly(bpy)
    st = bt.stats
    econ = st.groupby(st.index.year).agg(
        gross_pnl=("gross_pnl", "sum"),
        pnl_long=("pnl_long", "sum"),
        pnl_short=("pnl_short", "sum"),
        funding=("funding", "sum"),
        turnover=("turnover", "sum"),
    )
    econ["costs"] = st[["fees", "spread", "impact"]].sum(axis=1).groupby(st.index.year).sum()
    if len(yearly):
        yearly = yearly.join(econ)

    # --- robustness & null backtests (parallel) ---------------------------------------------------------
    _CTX.update({"ds": ds, "wf": wf, "cfg": cfg, "start": start})
    grid = grid or [{"holding_horizon": h, "cost_aversion": ca} for h in cfg.labels.horizons for ca in (0.5, 1.0, 2.0)]
    jobs: list[tuple[str, int | dict[str, object]]] = [("null", s) for s in range(n_null)]
    jobs += [("grid", g) for g in grid]
    jobs += [("costx2", {}), ("lag1", {}), ("nohalt", {})]
    if wf.market_score is not None:
        jobs.append(("market", {"beta_neutral": True}))
    results = _run_parallel(jobs, workers)
    null_sr = [sharpe(d.to_numpy(), 365.0) for k, d, _ in results if k.startswith("null")]
    grid_rows, grid_daily = [], {}
    stress: dict[str, float] = {}
    nohalt: dict[str, object] = {}
    for k, d, s in results:
        if k.startswith("grid"):
            grid_rows.append(
                {
                    "config": k[5:],
                    "sharpe": sharpe(d.to_numpy(), 365.0),
                    "return": s.get("cagr"),
                    "max_dd": s.get("max_drawdown"),
                    "turnover": s.get("turnover_annual"),
                }
            )
            grid_daily[k[5:]] = d
        elif k.startswith("market"):
            stress["sharpe_with_market"] = sharpe(d.to_numpy(), 365.0)
        elif k.startswith("costx2"):
            stress["sharpe_costx2"] = sharpe(d.to_numpy(), 365.0)
        elif k.startswith("lag1"):
            stress["sharpe_lag1"] = sharpe(d.to_numpy(), 365.0)
        elif k.startswith("nohalt"):
            by_year = (1 + d).groupby(d.index.year).prod() - 1
            nohalt = {
                "sharpe": sharpe(d.to_numpy(), 365.0),
                "cagr": float(s.get("cagr", np.nan)),
                "max_drawdown": float(s.get("max_drawdown", np.nan)),
                "gross_pnl_annual": float(s.get("gross_pnl_annual", np.nan)),
                "costs_annual": float(sum(s.get(x, 0.0) for x in ("fees_annual", "spread_annual", "impact_annual"))),
                "turnover_annual": float(s.get("turnover_annual", np.nan)),
                "avg_gross": float(s.get("avg_gross", np.nan)),
                "by_year": {int(y): round(float(r), 4) for y, r in by_year.items()},
            }
    grid_df = pd.DataFrame(grid_rows).set_index("config") if grid_rows else pd.DataFrame()

    # --- statistics -----------------------------------------------------------------------------------------
    d = daily.to_numpy()
    sr_d = sharpe(d, 365.0)
    grid_mat = pd.DataFrame(grid_daily).dropna()
    n_trials, trial_var = trial_count_and_variance(v.n_trials, grid_mat, len(d))
    tests = {
        "sharpe_daily": sr_d,
        "psr": probabilistic_sharpe(d),
        "dsr": deflated_sharpe(d, n_trials, trial_var),
        "n_trials": float(n_trials),
        "min_track_record_days": min_track_record(d),
        "spa_pvalue": spa_test(d, n_samples=v.bootstrap_samples, mean_block=5.0),
        "pbo": pbo(grid_mat.to_numpy(), n_splits=10) if grid_mat.shape[1] >= 2 else float("nan"),
        "null_percentile": float(np.mean(np.array(null_sr) < sr_d)) if null_sr else float("nan"),
        # Exact permutation p-value (Phipson & Smyth 2010): its size is at most alpha for any null count.
        "null_pvalue": float((1 + np.sum(np.array(null_sr) >= sr_d)) / (len(null_sr) + 1)) if null_sr else 1.0,
        "null_sharpe_p95": float(np.quantile(null_sr, 0.95)) if null_sr else float("nan"),
        "oos_months": float(len(d) / 30.4),
    }
    boot = stationary_bootstrap(
        d, lambda x: sharpe(x, 365.0, lo_adjust=False), n_samples=v.bootstrap_samples, mean_block=5.0
    )
    tests["sharpe_ci_low"] = float(np.quantile(boot, 0.05))
    tests["sharpe_ci_high"] = float(np.quantile(boot, 0.95))
    tests.update(stress)
    pos_years = positive_year_fraction(daily)
    tests["positive_year_fraction"] = pos_years

    gate = {
        "dsr": {"value": tests["dsr"], "threshold": v.gate_min_dsr, "pass": tests["dsr"] >= v.gate_min_dsr},
        "null_pvalue": {
            "value": tests["null_pvalue"],
            "threshold": 1.0 - v.gate_min_null_percentile,
            "pass": tests["null_pvalue"] <= 1.0 - v.gate_min_null_percentile,
        },
        "pbo": {
            "value": tests["pbo"],
            "threshold": v.gate_max_pbo,
            "pass": bool(np.isfinite(tests["pbo"]) and tests["pbo"] <= v.gate_max_pbo),
        },
        "sharpe": {"value": sr_d, "threshold": v.gate_min_sharpe, "pass": sr_d >= v.gate_min_sharpe},
        "positive_years": {
            "value": pos_years,
            "threshold": v.gate_min_positive_year_fraction,
            "pass": pos_years >= v.gate_min_positive_year_fraction,
        },
        "oos_months": {
            "value": tests["oos_months"],
            "threshold": v.gate_min_months,
            "pass": tests["oos_months"] >= v.gate_min_months,
        },
        "cost_stress": {
            "value": stress.get("sharpe_costx2", np.nan),
            "threshold": 0.0,
            "pass": bool(stress.get("sharpe_costx2", -1) > 0),
        },
        "latency_stress": {
            "value": stress.get("sharpe_lag1", np.nan),
            "threshold": 0.0,
            "pass": bool(stress.get("sharpe_lag1", -1) > 0),
        },
    }
    promoted = all(g["pass"] for g in gate.values())
    mt = ic.get("market_timing")
    # The market model may steer net exposure only if it passes its own out-of-sample test AND improves the
    # promoted cross-sectional book once costs are paid.
    tests["market_promoted"] = float(
        bool(isinstance(mt, dict) and mt.get("gate") and stress.get("sharpe_with_market", -np.inf) > sr_d)
    )
    ev = Evaluation(
        ic=ic,
        summary=summary,
        yearly=yearly,
        tests=tests,
        gate=gate,
        promoted=promoted,
        grid=grid_df,
        null_sharpes=[round(x, 3) for x in null_sr],
        halted_at=_stopped_at(bt),
        nohalt=nohalt,
    )
    return ev, bt


__all__ = ["Evaluation", "block_permute", "cs_zscore", "evaluate", "make_signal"]
