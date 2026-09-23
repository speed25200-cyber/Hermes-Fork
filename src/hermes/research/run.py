"""End-to-end research run: data -> dataset -> walk-forward -> evaluation -> final model -> report.

Every run is recorded in a trial ledger (``reports/trials/``, one file per run). The number of distinct configurations
ever evaluated feeds the Deflated Sharpe Ratio: trying many ideas on the same history is not free, and the
ledger makes that cost explicit instead of forgotten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
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


def _memory_note(phase: str) -> None:
    """Log resident and available memory at a phase boundary (diagnoses out-of-memory kills on runners)."""
    try:
        status = Path("/proc/self/status").read_text().splitlines()
        meminfo = Path("/proc/meminfo").read_text().splitlines()
        rss = next(int(x.split()[1]) for x in status if x.startswith("VmRSS"))
        avail = next(int(x.split()[1]) for x in meminfo if x.startswith("MemAvailable"))
        log.info("memory after %s: process %.1f GB, available %.1f GB", phase, rss / 1e6, avail / 1e6)
    except (OSError, StopIteration, ValueError):
        pass


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


TRAINING_VALIDATION_KEYS = (
    "train_days",
    "expanding",
    "test_days",
    "embargo_minutes",
    "min_train_days",
    "val_days",
    "train_sample_minutes",
    "recency_halflife_days",
)


def training_hash(cfg: HermesConfig) -> str:
    """Hash of what the walk-forward *training* depends on (data, features, labels, models, splits).

    A saved walk-forward is reused when only evaluation settings changed (costs, portfolio, risk, gate
    thresholds): the out-of-sample predictions are the same, only their evaluation differs.
    """
    d = json.loads(cfg.model_dump_json())
    keep = {k: d[k] for k in ("data", "features", "labels", "model", "seed")}
    keep["data"] = {k: v for k, v in keep["data"].items() if k not in ("cache_dir", "download_workers", "end")}
    keep["validation"] = {k: d["validation"][k] for k in TRAINING_VALIDATION_KEYS}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]


def _resumable(saved: str, current: str) -> bool:
    """A saved walk-forward is reusable when training settings, panel start and contract set match and it
    covers at least the current panel (a longer history is truncated, never extended)."""
    parts0, parts1 = _marker_parts(saved), _marker_parts(current)
    if parts0 is None or parts1 is None:
        return False
    return (
        parts0[0] == parts1[0]
        and parts0[1] == parts1[1]
        and parts0[3] == parts1[3]
        and pd.Timestamp(parts0[2]) >= pd.Timestamp(parts1[2])
    )


def _marker_parts(marker: str) -> tuple[str, str, str, str] | None:
    """(training hash, panel start, panel end, contracts) from ``hash:start:end:n`` (timestamps contain ':')."""
    try:
        head, n = marker.rsplit(":", 1)
        h, stamps = head.split(":", 1)
        # Each timestamp is 'YYYY-MM-DD HH:MM:SS+00:00': split the two on the '+00:00:' boundary.
        start, end = stamps.split("+00:00:", 1)
        return h, start + "+00:00", end, n
    except ValueError:
        return None


def _ledger_records(ledger: Path) -> list[dict[str, object]]:
    """Trial records: one JSON file per run in the ledger directory (parallel jobs never conflict), plus the
    legacy ``trials.jsonl`` next to it if present."""
    recs: list[dict[str, object]] = []
    files = sorted(ledger.glob("*.json")) if ledger.is_dir() else []
    legacy = ledger.with_suffix(".jsonl") if ledger.suffix != ".jsonl" else ledger
    lines = legacy.read_text().splitlines() if legacy.exists() else []
    for text in [f.read_text() for f in files] + lines:
        try:
            recs.append(json.loads(text))
        except ValueError:
            continue
    return recs


def ledger_trials(ledger: Path, current: str) -> int:
    seen = {current} | {str(r.get("config_hash")) for r in _ledger_records(ledger) if r.get("config_hash")}
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
    # Reference distribution of the features for train/serve drift checks (most recent 90 days of training).
    from hermes.models.drift import feature_profile

    recent = np.nonzero((ds.t_pos < last) & (ds.t_pos >= last - cfg.days(90)))[0]
    profile = feature_profile(ds.X[recent], ds.feature_names) if len(recent) else {}
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
            "feature_profile": profile,
            **(extra_meta or {}),
        },
    )


def run_research(
    cfg: HermesConfig,
    out_dir: str | Path,
    panel: Panel | None = None,
    n_null: int | None = None,
    workers: int | None = None,
    ledger: str | Path | None = "reports/trials",
    save_model: bool = True,
    resume: bool = True,
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
    _memory_note("dataset")
    wf_dir = out / "walkforward"
    marker = wf_dir / "training_hash"
    thash = f"{training_hash(cfg)}:{panel.index[0]}:{panel.index[-1]}:{len(panel.symbols)}"
    if resume and marker.exists() and _resumable(marker.read_text(), thash):
        log.info("resuming from the saved walk-forward in %s", wf_dir)
        wf = WalkForwardResult.load(wf_dir).restricted_to(ds.panel.index, ds.panel.symbols)
    else:
        # The marker is only valid for a complete save: remove it (and stale artefacts) before retraining.
        if wf_dir.exists():
            shutil.rmtree(wf_dir)
        wf = walk_forward_train(ds, cfg)
        wf.save(wf_dir)
        marker.write_text(thash)
    _memory_note("walk-forward")
    bundle = train_final(ds, cfg, False, {}) if save_model else None
    _memory_note("final model")
    # Training arrays are no longer needed: free them before the forking evaluation.
    ds.release_training_arrays()
    ev, bt = evaluate(ds, wf, cfg, n_null=n_null, workers=workers)
    elapsed = time.time() - t0
    write_report(out, cfg, ds, wf, ev, bt, chash, elapsed)
    if bundle is not None:
        from hermes.portfolio.alpha import signal_persistence
        from hermes.research.evaluate import holding_target, traded_score

        H, _ = holding_target(ds, cfg)
        persist = signal_persistence(
            traded_score(wf.score, cfg, H), H, cfg.bars_per_day * 30, floor=cfg.portfolio.cost_scale_floor
        ).dropna()
        bundle.meta.update(
            {
                "promoted": ev.promoted,
                "evaluation": dict(ev.tests.items()),
                "cost_scale": float(persist.iloc[-1]) if len(persist) else 1.0,
                "market_promoted": bool(ev.tests.get("market_promoted", 0.0)),
            }
        )
        bundle.save(out / "model")
    if ledger is not None:
        Path(ledger).mkdir(parents=True, exist_ok=True)
        rec = {
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "config_hash": chash,
            "data": f"{cfg.data.source}:{cfg.data.bar}:{panel.index[0].date()}..{panel.index[-1].date()}",
            "sharpe_daily": round(float(ev.tests["sharpe_daily"]), 3),
            "dsr": round(float(ev.tests["dsr"]), 3),
            "ic_h": {k: round(float(v["ic_mean"]), 4) for k, v in ev.ic.items() if k.startswith("h")},  # type: ignore[index]
            "promoted": ev.promoted,
        }
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        (Path(ledger) / f"{stamp}-{chash}.json").write_text(json.dumps(rec) + "\n")
    log.info("research done in %.0fs, promoted=%s", time.time() - t0, ev.promoted)
    return ev, wf, ds


__all__ = ["config_hash", "pd", "run_research", "train_final", "training_hash"]
