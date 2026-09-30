#!/usr/bin/env python3
"""One locked OOF engine replay; choose the engine with PYTHONPATH, never retrain.

Run baseline first, then corrected with --reference-inputs baseline/inputs.json.
The script lives outside hermes so both processes execute this identical file.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import logging
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd

import hermes.backtest.engine as engine
from hermes.config import HermesConfig, bars_for
from hermes.data.panel import Panel
from hermes.features.library import FeatureSet, build_features, ewm_vol, market_return, rolling_beta
from hermes.labels.targets import build_targets
from hermes.research.dataset import Dataset
from hermes.research.evaluate import book_signal

ROOT = Path(__file__).resolve().parent
ARTIFACT = ROOT.parent / "research_books/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756"
CAPITAL = 10_000.0
log = logging.getLogger("fixed_oof_replay")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def frame_hash(frame: pd.DataFrame | pd.Series) -> str:
    if isinstance(frame, pd.Series):
        frame = frame.to_frame()
    h = hashlib.sha256()
    h.update(json.dumps({"columns": list(map(str, frame.columns)), "dtypes": list(map(str, frame.dtypes))}).encode())
    h.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return h.hexdigest()


def write_json(path: Path, value: object) -> None:
    # Explicit null for undefined ratios; strict JSON for machine comparisons.
    def clean(x):
        if isinstance(x, dict):
            return {str(k): clean(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [clean(v) for v in x]
        if isinstance(x, (float, np.floating)):
            return float(x) if np.isfinite(x) else None
        if isinstance(x, np.integer):
            return int(x)
        return x

    path.write_text(json.dumps(clean(value), indent=2, sort_keys=True, allow_nan=False) + "\n")


def minimal_features(panel: Panel, mask: pd.DataFrame, cfg: HermesConfig) -> FeatureSet:
    """Exact auxiliary block from build_features; no learned feature matrix is needed."""
    r1 = np.log(panel["close"].astype("float64")).diff()
    hl = max(2, bars_for(cfg.features.vol_halflife_minutes, panel.bar))
    vol = ewm_vol(r1, hl).where(lambda v: v > 0).ffill()
    mkt = market_return(r1, mask).fillna(0.0)
    beta = rolling_beta(r1, mkt, halflife=hl * 2)
    ivol = ewm_vol(r1 - beta.mul(mkt, axis=0), hl).where(lambda v: v > 0).ffill()
    mkt_vol = np.sqrt((mkt**2).ewm(halflife=hl, min_periods=min(24, hl), adjust=False).mean())
    return FeatureSet(
        frames={},
        market={},
        aux={
            "r1": r1,
            "vol": vol,
            "ivol": ivol,
            "beta": beta,
            "mkt": mkt.to_frame("mkt"),
            "mkt_vol": mkt_vol.to_frame("mkt_vol"),
        },
    )


def verify_auxiliary_parity(panel: Panel, mask: pd.DataFrame, cfg: HermesConfig) -> dict:
    """Same fresh 30-day prefix and same symbols for each implementation, exact equality."""
    small = panel.iloc(slice(0, min(len(panel.index), cfg.days(30))))
    membership = mask.loc[small.index]
    full = build_features(small, membership, cfg.features)
    minimal = minimal_features(small, membership, cfg)
    for name in minimal.aux:
        pd.testing.assert_frame_equal(minimal.aux[name], full.aux[name], check_exact=True)
    full_targets = build_targets(small, full, membership, cfg.labels)
    minimal_targets = build_targets(small, minimal, membership, cfg.labels)
    for h in cfg.labels.horizons:
        pd.testing.assert_frame_equal(minimal_targets.residual[h], full_targets.residual[h], check_exact=True)
        if not minimal_targets.residual[h].notna().to_numpy().any():
            raise ValueError(f"Vacuous target parity check at horizon {h}")
    return {
        "status": "exact",
        "bars": len(small.index),
        "symbols": len(small.symbols),
        "auxiliary_fields": sorted(minimal.aux),
        "label_horizons": list(cfg.labels.horizons),
    }


def common_metrics(r: pd.Series, bars_per_year: float) -> dict:
    """Identical formulas for both versions, including loss from initial cash in drawdown."""
    if r.empty or not np.isfinite(r.to_numpy()).all() or (r < -1).any():
        raise ValueError("Invalid engine returns")
    eq = (1 + r).cumprod()
    daily = (1 + r).groupby(r.index.floor("D")).prod() - 1
    years = len(r) / bars_per_year
    drawdown = float((eq / eq.cummax().clip(lower=1.0) - 1).min())

    def sr(s, annual):
        sd = s.std(ddof=1)
        return float(s.mean() / sd * np.sqrt(annual)) if sd > 0 else None

    cagr = float(eq.iloc[-1] ** (1 / years) - 1) if eq.iloc[-1] > 0 else -1.0
    return {
        "total_return": float(eq.iloc[-1] - 1),
        "cagr": cagr,
        "ann_vol": float(r.std(ddof=1) * np.sqrt(bars_per_year)),
        "sharpe": sr(r, bars_per_year),
        "sharpe_daily": sr(daily, 365),
        "max_drawdown": drawdown,
        "calmar": cagr / abs(drawdown) if drawdown < 0 else None,
        "worst_day": float(daily.min()),
        "best_day": float(daily.max()),
        "n_bars": len(r),
        "n_days": len(daily),
    }


@contextmanager
def final_portfolio_observer(cfg: HermesConfig):
    """Observe returned decisions without changing any arguments, results or mutable risk state."""
    constructor, overlay = engine.PortfolioConstructor, engine.RiskOverlay
    old_target, old_apply = constructor.target, overlay.apply
    old_limit = getattr(constructor, "limit_after_overlay", None)
    context = {}
    names = ("weight", "adv", "gross", "beta_exposure", "vol_annual", "es_daily", "positions")
    report = {
        "n_decisions": 0,
        "min_final_scale": 1.0 if old_limit else None,
        "max_abs_weight": 0.0,
        "max_adv_ratio": 0.0,
        "max_gross": 0.0,
        "max_abs_beta_exposure": 0.0,
        "max_vol_annual": 0.0,
        "max_es_daily": 0.0,
        "max_positions": 0,
        "violations": {name: 0 for name in names},
        "first_violation": {},
        "absolute_tolerance": 1e-9,
        "stage": "after_final_limiter" if old_limit else "after_overlay",
        "equity_basis": "decision_equity_before_rebalance_fees",
    }

    def observe(w, inp, equity, cov, net_max=None):
        pc = cfg.portfolio
        abs_w = np.abs(w)
        adv = np.where(np.isfinite(inp.adv) & (inp.adv > 0), inp.adv, 0.0)
        cap_adv = pc.adv_participation_max * adv / max(equity, 1e-9)
        beta = np.where(np.isfinite(inp.beta), inp.beta, 1.0) if pc.beta_neutral else np.ones(len(w))
        bound = min(pc.net_max, 0.05) if pc.beta_neutral and inp.market_alpha == 0 else pc.net_max
        if net_max is not None:
            bound = net_max
        gross = float(abs_w.sum())
        exposure = abs(float(beta @ w))
        vol = float(np.sqrt(max(w @ cov @ w, 0.0) * cfg.bars_per_year))
        risk = context["overlay"]
        _, es = risk.es_scale(w, cov, context["bars_per_day"], context["history"])
        ratio = np.divide(abs_w, cap_adv, out=np.zeros_like(abs_w), where=cap_adv > 0)
        if np.any((cap_adv <= 0) & (abs_w > 0)):
            ratio[(cap_adv <= 0) & (abs_w > 0)] = np.inf
        positions = int(np.count_nonzero(w))
        report["n_decisions"] += 1
        for key, val in {
            "max_abs_weight": float(abs_w.max(initial=0)),
            "max_adv_ratio": float(ratio.max(initial=0)),
            "max_gross": gross,
            "max_abs_beta_exposure": exposure,
            "max_vol_annual": vol,
            "max_es_daily": es,
            "max_positions": positions,
        }.items():
            report[key] = max(report[key], val)
        tol = report["absolute_tolerance"]
        violations = {
            "weight": np.any(abs_w > pc.weight_max + tol),
            "adv": np.any(abs_w > cap_adv + tol),
            "gross": gross > pc.gross_max + tol,
            "beta_exposure": exposure > bound + tol,
            "vol_annual": vol > pc.vol_target_annual + tol,
            "es_daily": es > cfg.risk.es_limit_daily + tol,
            "positions": positions > cfg.risk.max_positions,
        }
        for key, bad in violations.items():
            if bad:
                report["violations"][key] += 1
                report["first_violation"].setdefault(key, str(context["ts"]))

    def target(self, inp, equity):
        # target() itself invokes its limiter. Only the later aggregate decision
        # following overlay.apply() is the portfolio this observer measures.
        context["pending_final"] = False
        result = old_target(self, inp, equity)
        context["input"] = inp  # Same beta/ADV/index on each of the fixed nine sub-books.
        return result

    def apply(self, ts, equity, proposal, current, cov_bar, bars_per_day, hist_daily_returns=None):
        result = old_apply(self, ts, equity, proposal, current, cov_bar, bars_per_day, hist_daily_returns)
        context.update(
            overlay=self,
            ts=ts,
            bars_per_day=bars_per_day,
            history=hist_daily_returns,
            pending_final=old_limit is not None,
        )
        if old_limit is None:
            observe(result[0], context["input"], equity, cov_bar)
        return result

    def limit(self, weights, inp, equity, cov_bar, *, net_max=None):
        result = old_limit(self, weights, inp, equity, cov_bar, net_max=net_max)
        if not context.get("pending_final", False):
            return result
        context["pending_final"] = False
        report["min_final_scale"] = min(report["min_final_scale"], float(result[1]))
        observe(result[0], inp, equity, cov_bar, net_max)
        return result

    constructor.target, overlay.apply = target, apply
    if old_limit:
        constructor.limit_after_overlay = limit
    try:
        yield report
    finally:
        constructor.target, overlay.apply = old_target, old_apply
        if old_limit:
            constructor.limit_after_overlay = old_limit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, default=ROOT / "panel")
    parser.add_argument("--artifact", type=Path, default=ARTIFACT)
    parser.add_argument("--declaration", type=Path, default=ROOT / "declaration.json")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reference-inputs", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    began = time.monotonic()
    declaration = json.loads(args.declaration.read_text())
    start = pd.Timestamp(declaration["evaluation_start"])
    end = pd.Timestamp(declaration["evaluation_end"])
    warmup = pd.Timestamp(declaration["warmup_start"], tz="UTC")
    if (start, end, warmup) != (
        pd.Timestamp("2026-01-01", tz="UTC"),
        pd.Timestamp("2026-08-31 19:30", tz="UTC"),
        pd.Timestamp("2025-07-01", tz="UTC"),
    ):
        raise ValueError("This diagnostic is locked to the declared dates")
    if declaration.get("capital_usdt", CAPITAL) != CAPITAL:
        raise ValueError("Declared capital differs from locked 10000 USDT")
    paths = {
        "declaration": args.declaration,
        "config": args.artifact / "model/config.json",
        "score": args.artifact / "walkforward/score.parquet",
        "series": args.artifact / "walkforward/series.parquet",
    }
    hashes = {k: file_hash(p) for k, p in paths.items()}
    if hashes["config"] != declaration["config_sha256"] or hashes["score"] != declaration["oof_sha256"]:
        raise ValueError("OOF/config does not match the prefixed declaration")
    cfg = HermesConfig.model_validate_json(paths["config"].read_text())
    panel = Panel.load(args.panel).loc(warmup, end)
    # Parquet sources can store the same UTC instants at ms/us/ns precision.
    # Canonicalise the index representation only; no rows or values are changed.
    for frame in panel.fields.values():
        frame.index = frame.index.as_unit("ns")
    expected_index = pd.date_range(warmup, end, freq="30min").as_unit("ns")
    if panel.bar != "30m" or not panel.index.equals(expected_index):
        raise ValueError("Panel does not cover the entire regular declared 30-minute interval")
    score = pd.read_parquet(paths["score"]).loc[warmup:end].astype("float64")
    score.index = score.index.as_unit("ns")
    if not score.index.equals(expected_index) or list(score.columns) != declaration["symbols"]:
        raise ValueError("OOF index/symbols differ from the fixed declaration")
    if not (score.notna().sum(axis=1) == cfg.data.universe.top_n).all():
        raise ValueError("OOF membership is not the expected fixed count on every bar")
    needed = list(score.columns[score.notna().any(axis=0)])
    missing = sorted(set(needed) - set(panel.symbols))
    if missing:
        raise ValueError(f"Panel lacks historical OOF members: {missing}")
    panel = panel.subset(needed)
    score = score[needed]
    mask = score.notna()
    required = ("open", "high", "low", "close", "quote_volume", "funding_rate", "vwap_first")
    if set(required) - set(panel.fields):
        raise ValueError("Panel lacks required price/cost/funding/execution fields")
    quality = {}
    for name in ("open", "high", "low", "close", "quote_volume"):
        frame = panel[name]
        bad = mask & (~np.isfinite(frame) | (frame <= 0))
        quality[f"invalid_member_{name}"] = int(bad.to_numpy().sum())
        if quality[f"invalid_member_{name}"]:
            raise ValueError(f"Missing/nonpositive {name} on a historical OOF member")
    # Funding is sparse at actual settlements, so NaN elsewhere is expected, not zeroed here.
    quality["member_funding_observations"] = int((mask & panel["funding_rate"].notna()).to_numpy().sum())
    if quality["member_funding_observations"] == 0:
        raise ValueError("No observed funding settlements")
    quality["missing_member_vwap"] = int((mask & panel["vwap_first"].isna()).to_numpy().sum())
    log.info("Loaded %d bars, %d historical members; verifying minimal-feature parity", *panel.shape)
    parity = verify_auxiliary_parity(panel, mask, cfg)
    feats = minimal_features(panel, mask, cfg)
    targets = build_targets(panel, feats, mask, cfg.labels)
    empty = np.empty(0, dtype=np.float32)
    ds = Dataset(
        panel=panel,
        mask=mask,
        feats=feats,
        targets=targets,
        X=np.empty((0, 0), np.float16),
        t_pos=np.empty(0, np.int32),
        s_pos=np.empty(0, np.int32),
        y=empty,
        y_raw=empty,
        feature_names=[],
        market_X=np.empty((0, 0), np.float32),
        market_y=empty,
        market_names=[],
    )
    original_series = pd.read_parquet(paths["series"])
    original_series.index = original_series.index.as_unit("ns")
    prior = original_series["prior_ic"].reindex(panel.index)
    if prior.isna().any():
        raise ValueError("Missing original validation IC prior")
    signal = book_signal(score, ds, prior, cfg)  # Deliberately no market-alpha arguments.
    members = signal.members if isinstance(signal, engine.BookSignals) else [(cfg.portfolio, signal)]
    derived = {
        "mask": frame_hash(mask),
        "score": frame_hash(score),
        "prior_ic": frame_hash(prior),
        **{f"aux/{k}": frame_hash(v) for k, v in feats.aux.items()},
        **{f"label/{h}": frame_hash(v) for h, v in targets.residual.items()},
    }
    for i, (pc, sb) in enumerate(members):
        assert sb.market_alpha is None
        derived[f"book/{i}/config"] = hashlib.sha256(pc.model_dump_json().encode()).hexdigest()
        derived[f"book/{i}/score"] = frame_hash(sb.score)
        derived[f"book/{i}/ic_est"] = frame_hash(sb.ic_est)
        derived[f"book/{i}/cost_scale"] = frame_hash(sb.cost_scale)
    inputs = {
        "schema": 1,
        "script_sha256": file_hash(Path(__file__)),
        "capital_usdt": CAPITAL,
        "evaluation_start": str(start),
        "evaluation_end": str(end),
        "warmup_start": str(warmup),
        "input_files": hashes,
        "panel_files": {p.name: file_hash(p) for p in sorted(args.panel.iterdir()) if p.is_file()},
        "derived": derived,
        "symbols_used": needed,
        "data_quality": quality,
        "minimal_feature_parity": parity,
        "mask_source": "finite_original_oof_score",
    }
    if args.reference_inputs and inputs != json.loads(args.reference_inputs.read_text()):
        raise ValueError("Baseline and corrected replay inputs differ; engine comparison refused")
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / "inputs.json", inputs)
    log.info("Fixed %d books and input hashes; flat cash %.2f at %s; beginning engine", len(members), CAPITAL, start)
    with final_portfolio_observer(cfg) as constraints:
        result = engine.run_backtest(
            panel, mask, feats.aux, signal, cfg, capital=CAPITAL, start=start, end=end, record_weights=True
        )
    if not result.returns.index.equals(expected_index[expected_index >= start]):
        raise ValueError("Engine returned a different evaluation interval")
    result.returns.to_frame("return").to_parquet(args.output / "returns.parquet", compression="zstd")
    result.stats.to_parquet(args.output / "stats.parquet", compression="zstd")
    result.weights.to_parquet(args.output / "weights.parquet", compression="zstd")
    daily = ((1 + result.returns).resample("1D").prod() - 1).rename("return").to_frame()
    for col in ("fees", "spread", "impact", "funding", "gross_pnl", "turnover", "stops"):
        daily[col] = result.stats[col].resample("1D").sum()
    for col in ("gross", "net", "beta_exposure", "n_positions", "ic_est"):
        daily[f"mean_{col}"] = result.stats[col].resample("1D").mean()
    daily.to_csv(args.output / "daily.csv", index_label="date")
    ((1 + result.returns).resample("MS").prod() - 1).rename("return").to_csv(args.output / "monthly.csv")
    weights = result.weights.astype("float64")
    beta = feats.aux["beta"].reindex(index=weights.index, columns=weights.columns).fillna(1)
    engine_file = Path(inspect.getfile(engine)).resolve()
    repo = engine_file.parents[3]
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    summary = {
        "purpose": declaration["purpose"],
        "limitations": declaration["limits"],
        "engine_commit": revision,
        "engine_source": str(engine_file),
        "engine_sha256": file_hash(engine_file),
        "capital_usdt": CAPITAL,
        "final_equity_usdt": float(result.equity.iloc[-1]),
        "start": str(start),
        "end_bar_open": str(end),
        "end_bar_close": str(end + panel.bar_delta),
        "metrics": common_metrics(result.returns, cfg.bars_per_year),
        "cost_return_sums": {
            col: float(result.stats[col].sum())
            for col in ("fees", "spread", "impact", "funding", "gross_pnl", "slippage")
        },
        "turnover_sum": float(result.stats.turnover.sum()),
        "stop_count": int(result.stats.stops.sum()),
        "positions": {
            "max_gross": float(weights.abs().sum(axis=1).max()),
            "max_abs_weight": float(weights.abs().to_numpy().max()),
            "max_abs_beta_exposure": float((weights * beta).sum(axis=1).abs().max()),
            "max_positions": int((weights != 0).sum(axis=1).max()),
            "vol_ex_ante_stats_is_pre_overlay": True,
            "weights_storage": "float32",
        },
        "risk_events": result.risk_events,
        "elapsed_seconds": time.monotonic() - began,
        "final_decision_constraints": constraints,
        "inputs_sha256": file_hash(args.output / "inputs.json"),
        "no_promotion_decision": True,
    }
    write_json(args.output / "summary.json", summary)
    log.info("Completed %s: %s", args.output, summary["metrics"])


if __name__ == "__main__":
    main()
