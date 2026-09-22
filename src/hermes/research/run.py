"""End-to-end research run: data -> dataset -> walk-forward -> evaluation -> final model -> report.

Every run is appended to a trial ledger (``reports/trials.jsonl``). The number of distinct configurations
ever evaluated feeds the Deflated Sharpe Ratio: trying many ideas on the same history is not free, and the
ledger makes that cost explicit instead of forgotten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hermes.config import HermesConfig
from hermes.data.panel import Panel
from hermes.data.store import load_panel
from hermes.models.bundle import ModelBundle
from hermes.research.dataset import Dataset, build_dataset
from hermes.research.evaluate import Evaluation, evaluate
from hermes.research.report import write_report
from hermes.research.walkforward import (
    WalkForwardResult,
    _ensemble_weights,
    _market_fold,
    train_fold_models,
    walk_forward_train,
)

log = logging.getLogger(__name__)


def config_hash(cfg: HermesConfig) -> str:
    """Hash of everything that changes the *strategy* (paths and workers excluded)."""
    d = json.loads(cfg.model_dump_json())
    d["data"].pop("cache_dir", None)
    d["data"].pop("download_workers", None)
    d["data"].pop("end", None)
    d.pop("live", None)
    d.pop("execution", None)
    d["validation"].pop("n_trials", None)
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]


def ledger_trials(ledger: Path, current: str) -> int:
    seen = {current}
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            try:
                seen.add(json.loads(line)["config_hash"])
            except (ValueError, KeyError):
                continue
    return len(seen)


def train_final(
    ds: Dataset,
    cfg: HermesConfig,
    promoted: bool,
    evaluation: dict[str, object],
    extra_meta: dict[str, object] | None = None,
) -> ModelBundle:
    """Fit the production models on all labelled history (same procedure as one walk-forward fold)."""
    H = max(cfg.labels.horizons)
    last = ds.n_bars - H - 1
    bars = np.arange(0, last)
    models, ics = train_fold_models(ds, bars, cfg)
    lcbs = {k[5:]: float(models.pop(k)) for k in list(models) if k.startswith("_lcb_")}  # type: ignore[arg-type]
    weights = _ensemble_weights(ics, cfg.model.ensemble)
    prior = max(0.0, sum(weights[k] * lcbs.get(k, 0.0) for k in weights))
    market = None
    market_prior = 0.0
    if cfg.model.market_model and ds.market_X.shape[1]:
        _, market_prior = _market_fold(ds, bars, np.array([ds.n_bars - 1]), cfg)
        from hermes.config import LinearConfig
        from hermes.models.base import TrainData
        from hermes.models.linear import RidgeModel

        ok = bars[np.isfinite(ds.market_y[bars]) & np.all(np.isfinite(ds.market_X[bars]), axis=1)]
        market = RidgeModel(LinearConfig(alpha=3000.0)).fit(
            TrainData(ds.market_X[ok], ds.market_y[ok], np.zeros(len(ok), dtype=np.int64))
        )
    return ModelBundle(
        config=cfg,
        feature_names=ds.feature_names,
        gbm=models.get("gbm"),  # type: ignore[arg-type]
        ridge=models.get("ridge"),  # type: ignore[arg-type]
        weights=weights,
        prior_ic=prior,
        market=market,
        market_feature_names=ds.market_names,
        market_prior_ic=max(0.0, market_prior),
        meta={
            "promoted": promoted,
            "train_start": str(ds.panel.index[0]),
            "train_end": str(ds.panel.index[last]),
            "symbols": ds.panel.symbols,
            "config_hash": config_hash(cfg),
            "val_ic": ics,
            "evaluation": evaluation,
            **(extra_meta or {}),
        },
    )


def run_research(
    cfg: HermesConfig,
    out_dir: str | Path,
    panel: Panel | None = None,
    n_null: int | None = None,
    workers: int | None = None,
    ledger: str | Path | None = "reports/trials.jsonl",
    save_model: bool = True,
) -> tuple[Evaluation, WalkForwardResult, Dataset]:
    t0 = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    chash = config_hash(cfg)
    if ledger is not None:
        n_trials = max(cfg.validation.n_trials, ledger_trials(Path(ledger), chash))
        cfg = cfg.model_copy(update={"validation": cfg.validation.model_copy(update={"n_trials": n_trials})})
    panel = panel if panel is not None else load_panel(cfg.data, cfg.seed)
    log.info("panel %s from %s to %s", panel.shape, panel.index[0], panel.index[-1])
    ds = build_dataset(panel, cfg)
    wf = walk_forward_train(ds, cfg)
    ev, bt = evaluate(ds, wf, cfg, n_null=n_null, workers=workers)
    elapsed = time.time() - t0
    write_report(out, cfg, ds, wf, ev, bt, chash, elapsed)
    if save_model:
        from hermes.portfolio.alpha import signal_persistence

        H = cfg.portfolio.holding_horizon
        persist = signal_persistence(wf.score, H, cfg.bars_per_day * 30).dropna()
        extra = {"cost_scale": float(persist.iloc[-1]) if len(persist) else 1.0}
        bundle = train_final(ds, cfg, ev.promoted, {k: v for k, v in ev.tests.items()}, extra)
        bundle.save(out / "model")
    if ledger is not None:
        Path(ledger).parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "config_hash": chash,
            "data": f"{cfg.data.source}:{cfg.data.bar}:{panel.index[0].date()}..{panel.index[-1].date()}",
            "sharpe_daily": round(float(ev.tests["sharpe_daily"]), 3),
            "dsr": round(float(ev.tests["dsr"]), 3),
            "ic_h": {k: round(float(v["ic_mean"]), 4) for k, v in ev.ic.items() if k.startswith("h")},  # type: ignore[index]
            "promoted": ev.promoted,
        }
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    log.info("research done in %.0fs, promoted=%s", time.time() - t0, ev.promoted)
    return ev, wf, ds


__all__ = ["config_hash", "pd", "run_research", "train_final"]
