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
from hermes.portfolio.alpha import cs_zscore, estimate_ic, market_alpha_series, rowwise_corr, signal_persistence
from hermes.research.dataset import Dataset
from hermes.research.walkforward import WalkForwardResult
from hermes.validation.metrics import cross_sectional_ic, ic_summary
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
    ric = rowwise_corr(score, target)
    ic_est = estimate_ic(ric, H, prior_ic, halflife_bars=cfg.bars_per_day * 30)
    malpha = None
    if market_score is not None and market_prior is not None:
        malpha = market_alpha(market_score, market_prior, ds, cfg)
    persist = signal_persistence(score, H, cfg.bars_per_day * 30)
    return SignalBundle(score=score, ic_est=ic_est, market_alpha=malpha, cost_scale=persist)


def market_alpha(market_score: pd.Series, market_prior: pd.Series, ds: Dataset, cfg: HermesConfig) -> pd.Series:
    H, _ = holding_target(ds, cfg)
    return market_alpha_series(
        market_score, ds.targets.market[H], ds.feats.aux["mkt"]["mkt"], market_prior, H, cfg.bars_per_day
    )


def block_permute(score: pd.DataFrame, mask: pd.DataFrame, seed: int, block_bars: int) -> pd.DataFrame:
    """Null score: within each block (one week), members' scores are reassigned by a random permutation of names.

    Preserves each score path's time structure (hence realistic turnover) while destroying its alignment
    with the contract whose return it is supposed to predict.
    """
    rng = np.random.default_rng(seed)
    S = score.to_numpy()
    M = mask.to_numpy() & np.isfinite(S)
    out = np.full_like(S, np.nan)
    T, N = S.shape
    for b0 in range(0, T, block_bars):
        keys = rng.random(N)
        for t in range(b0, min(T, b0 + block_bars)):
            mem = np.nonzero(M[t])[0]
            if len(mem) < 2:
                continue
            src = mem[np.argsort(keys[mem])]
            out[t, mem] = S[t, src]
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
        score = block_permute(wf.score, ds.mask, seed=int(param), block_bars=cfg.days(7))  # type: ignore[arg-type]
        sig = make_signal(score, ds, wf.prior_ic, cfg)
        c = cfg
    else:
        c = cfg.model_copy(update={"portfolio": cfg.portfolio.model_copy(update=param)})  # type: ignore[arg-type]
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
        return [_bt_job(j) for j in jobs]
    import multiprocessing as mp
    from concurrent.futures.process import BrokenProcessPool

    done: dict[int, tuple[str, pd.Series, dict[str, float]]] = {}
    try:
        with ProcessPoolExecutor(workers, mp_context=mp.get_context("fork")) as ex:
            futures = {ex.submit(_bt_job, j): i for i, j in enumerate(jobs)}
            for fut, i in futures.items():
                try:
                    done[i] = fut.result()
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
    rho = float(np.clip(np.nanmean(corr[np.triu_indices(n_grid, 1)]), 0.0, 1.0))
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
        }


def evaluate(
    ds: Dataset,
    wf: WalkForwardResult,
    cfg: HermesConfig,
    n_null: int | None = None,
    workers: int | None = None,
    grid: list[dict[str, object]] | None = None,
) -> tuple[Evaluation, BacktestResult]:
    v = cfg.validation
    bpy = cfg.bars_per_year
    start = wf.oof_start
    workers = workers or max(1, min(os.cpu_count() or 2, _workers_for_memory()))
    n_null = n_null if n_null is not None else min(v.null_permutations, 40)

    # --- forecast quality -------------------------------------------------------------------------------
    long_score = wf.score.stack()
    ic: dict[str, object] = {}
    for h, tgt in ds.targets.residual.items():
        ic[f"h{h}"] = ic_summary(cross_sectional_ic(long_score, tgt.stack()), horizon=h)
    for name, sc in wf.model_scores.items():
        H, tgt = holding_target(ds, cfg)
        ic[f"model_{name}_h{H}"] = ic_summary(cross_sectional_ic(sc.stack(), tgt.stack()), horizon=H)
    H, tgt = holding_target(ds, cfg)
    realized = rowwise_corr(wf.score, tgt).dropna()
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

    # --- robustness & null backtests (parallel) ---------------------------------------------------------
    _CTX.update({"ds": ds, "wf": wf, "cfg": cfg, "start": start})
    grid = grid or [{"holding_horizon": h, "cost_aversion": ca} for h in cfg.labels.horizons for ca in (0.5, 1.0, 2.0)]
    jobs: list[tuple[str, int | dict[str, object]]] = [("null", s) for s in range(n_null)]
    jobs += [("grid", g) for g in grid]
    jobs += [("costx2", {}), ("lag1", {})]
    if wf.market_score is not None:
        jobs.append(("market", {"beta_neutral": True}))
    results = _run_parallel(jobs, workers)
    null_sr = [sharpe(d.to_numpy(), 365.0) for k, d, _ in results if k.startswith("null")]
    grid_rows, grid_daily = [], {}
    stress: dict[str, float] = {}
    for k, d, s in results:
        if k.startswith("grid"):
            grid_rows.append(
                {
                    "config": k[5:],
                    "sharpe": s.get("sharpe_daily"),
                    "return": s.get("cagr"),
                    "max_dd": s.get("max_drawdown"),
                    "turnover": s.get("turnover_annual"),
                }
            )
            grid_daily[k[5:]] = d
        elif k.startswith("market"):
            stress["sharpe_with_market"] = float(s.get("sharpe_daily", np.nan))
        elif k.startswith("costx2"):
            stress["sharpe_costx2"] = float(s.get("sharpe_daily", np.nan))
        elif k.startswith("lag1"):
            stress["sharpe_lag1"] = float(s.get("sharpe_daily", np.nan))
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
    )
    return ev, bt


__all__ = ["Evaluation", "block_permute", "cs_zscore", "evaluate", "make_signal"]
