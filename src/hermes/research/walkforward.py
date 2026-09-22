"""Walk-forward training: the model is refit every ``test_bars`` on the past only and predicts the next block.

The concatenated out-of-fold (OOF) scores are the only predictions ever evaluated or backtested. Each
fold also records the validation IC of its models, which becomes the causal *prior* IC used for sizing
during that fold's test period.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from hermes.config import HermesConfig
from hermes.models.base import TrainData, cs_standardize, ic_lower_bound, mean_group_corr
from hermes.models.gbm import GBMModel
from hermes.models.linear import RidgeModel
from hermes.research.dataset import Dataset
from hermes.validation.splits import walk_forward

log = logging.getLogger(__name__)


@dataclass
class WalkForwardResult:
    score: pd.DataFrame  # ensemble OOF score (time x symbol)
    model_scores: dict[str, pd.DataFrame]
    prior_ic: pd.Series  # per bar, from the fold that produced the prediction
    folds: list[dict[str, object]] = field(default_factory=list)
    feature_importance: pd.Series | None = None
    market_score: pd.Series | None = None
    market_prior_ic: pd.Series | None = None

    @property
    def oof_start(self) -> pd.Timestamp:
        return self.score.dropna(how="all").index[0]


def first_valid_bar(ds: Dataset, min_members: int = 8) -> int:
    counts = ds.mask.sum(axis=1).to_numpy()
    ok = np.nonzero(counts >= min_members)[0]
    return int(ok[0]) if len(ok) else 0


def _weights(ds: Dataset, rows: np.ndarray, t_end: int, cfg: HermesConfig) -> np.ndarray | None:
    hl = cfg.validation.recency_halflife_days
    if hl <= 0:
        return None
    age_days = (t_end - ds.t_pos[rows]) / cfg.bars_per_day
    return (0.5 ** (age_days / hl)).astype(np.float32)


def _ensemble_weights(val_ics: dict[str, float], mode: str) -> dict[str, float]:
    names = list(val_ics)
    eq = {k: 1.0 / len(names) for k in names}
    if mode == "equal":
        return eq
    pos = {k: max(0.0, v) if np.isfinite(v) else 0.0 for k, v in val_ics.items()}
    tot = sum(pos.values())
    if tot <= 0:
        return eq
    return {k: 0.5 * eq[k] + 0.5 * pos[k] / tot for k in names}


def train_fold_models(
    ds: Dataset, train_bars: np.ndarray, cfg: HermesConfig
) -> tuple[dict[str, object], dict[str, float]]:
    """Fit every enabled model on ``train_bars`` with an inner, purged validation block for early stopping."""
    v = cfg.validation
    H = max(cfg.labels.horizons)
    t_end = int(train_bars.max()) + 1
    val_lo = t_end - v.val_bars
    core = train_bars[train_bars < val_lo - H]
    val = train_bars[train_bars >= val_lo]
    rows_core = ds.rows_for(core, v.train_stride)
    rows_val = ds.rows_for(val, v.train_stride)
    rows_core = rows_core[np.isfinite(ds.y[rows_core])]
    rows_val = rows_val[np.isfinite(ds.y[rows_val])]
    tr = TrainData(ds.X[rows_core], ds.y[rows_core], ds.t_pos[rows_core], _weights(ds, rows_core, t_end, cfg))
    va = TrainData(ds.X[rows_val], ds.y[rows_val], ds.t_pos[rows_val], _weights(ds, rows_val, t_end, cfg))
    models: dict[str, object] = {}
    ics: dict[str, float] = {}
    lcb: dict[str, float] = {}
    overlap = max(1, H // v.train_stride)
    if cfg.model.gbm.enabled:
        # Early stopping on one seed decides the number of trees; every seed is then fit on core + val.
        es_cfg = cfg.model.gbm.model_copy(update={"seeds": cfg.model.gbm.seeds[:1]})
        g = GBMModel(es_cfg, refit_full=False).fit(tr, va)
        ics["gbm"] = g.val_ic
        lcb["gbm"] = ic_lower_bound(g.predict(va.X), va.y, va.groups, overlap)
        # Refit on core + val with the early-stopped number of trees, scaled for the extra data.
        full = TrainData(
            np.vstack([tr.X, va.X]),
            np.concatenate([tr.y, va.y]),
            np.concatenate([tr.groups, va.groups]),
            None if tr.weight is None else np.concatenate([tr.weight, va.weight]),
        )  # type: ignore[list-item]
        scale = len(full) / max(len(tr), 1)
        gcfg = cfg.model.gbm.model_copy(update={"n_estimators": max(20, int(np.mean(g.best_iterations) * scale))})
        g_full = GBMModel(gcfg).fit(full)
        g_full.best_iterations, g_full.val_ic = g.best_iterations, g.val_ic
        models["gbm"] = g_full
    if cfg.model.linear.enabled:
        # The ridge is refit on core+val (no early stopping needed); its IC is measured on val first.
        r = RidgeModel(cfg.model.linear).fit(tr, va)
        ics["ridge"] = r.val_ic
        lcb["ridge"] = ic_lower_bound(r.predict(va.X), va.y, va.groups, overlap)
        full = TrainData(np.vstack([tr.X, va.X]), np.concatenate([tr.y, va.y]), np.concatenate([tr.groups, va.groups]))
        models["ridge"] = RidgeModel(cfg.model.linear).fit(full)
    for k in lcb:
        models[f"_lcb_{k}"] = lcb[k]  # type: ignore[assignment]
    return models, ics


def predict_rows(models: dict[str, object], ds: Dataset, rows: np.ndarray) -> dict[str, np.ndarray]:
    out = {}
    for name, m in models.items():
        p = m.predict(ds.X[rows])  # type: ignore[attr-defined]
        out[name] = cs_standardize(p, ds.t_pos[rows])
    return out


def _market_fold(
    ds: Dataset, train_bars: np.ndarray, test_bars: np.ndarray, cfg: HermesConfig
) -> tuple[np.ndarray, float]:
    """Ridge on market-state features -> normalised market forward return. Returns (test preds, val corr)."""
    H = max(cfg.labels.horizons)
    v = cfg.validation
    t_end = int(train_bars.max()) + 1
    core = train_bars[train_bars < t_end - v.val_bars - H]
    val = train_bars[train_bars >= t_end - v.val_bars]
    X, y = ds.market_X, ds.market_y
    ok_core = core[np.isfinite(y[core]) & np.all(np.isfinite(X[core]), axis=1)]
    ok_val = val[np.isfinite(y[val]) & np.all(np.isfinite(X[val]), axis=1)]
    if len(ok_core) < 500 or len(ok_val) < 50 or X.shape[1] == 0:
        return np.full(len(test_bars), np.nan), 0.0
    from hermes.config import LinearConfig

    m = RidgeModel(LinearConfig(alpha=3000.0))
    grp = np.zeros(len(ok_core), dtype=np.int64)
    m.fit(TrainData(X[ok_core], y[ok_core], grp))
    pv = m.predict(X[ok_val])
    corr = float(np.corrcoef(pv, y[ok_val])[0, 1]) if np.std(pv) > 0 else 0.0
    both = np.concatenate([ok_core, ok_val])
    m.fit(TrainData(X[both], y[both], np.zeros(len(both), dtype=np.int64)))
    return m.predict(np.nan_to_num(X[test_bars])), corr


def _deep_fold(
    ds: Dataset, pos: np.ndarray, train_bars: np.ndarray, test_bars: np.ndarray, cfg: HermesConfig, warm: object | None
) -> tuple[object, float, float, np.ndarray, np.ndarray]:
    """Train the cross-sectional attention network on the fold (warm-started) and score the test block."""
    from hermes.models.deep import DeepModel

    v = cfg.validation
    H = max(cfg.labels.horizons)
    t_end = int(train_bars.max()) + 1
    val_lo = t_end - v.val_bars
    core = train_bars[(train_bars < val_lo - H) & (train_bars % v.train_stride == 0)]
    val = train_bars[(train_bars >= val_lo) & (train_bars % v.train_stride == 0)]
    m = DeepModel(cfg.model.deep, max_members=cfg.data.universe.top_n)
    m.fit(ds.X, pos, ds.y, core, val, warm_start=warm)  # type: ignore[arg-type]
    vrows, vsc, vbars = m.predict(ds.X, pos, val)
    ok = np.isfinite(ds.y[vrows]) if len(vrows) else np.zeros(0, bool)
    lcb = ic_lower_bound(vsc[ok], ds.y[vrows][ok], vbars[ok], max(1, H // v.train_stride)) if ok.any() else 0.0
    rows, sc, _ = m.predict(ds.X, pos, test_bars)
    return m, float(m.val_ic), lcb, rows, sc


def walk_forward_train(ds: Dataset, cfg: HermesConfig) -> WalkForwardResult:
    v = cfg.validation
    H = max(cfg.labels.horizons)
    start = first_valid_bar(ds)
    folds = walk_forward(
        n_bars=ds.n_bars,
        test_bars=v.test_bars,
        min_train_bars=v.min_train_bars,
        horizon=H,
        embargo=v.embargo_bars,
        train_bars=None if v.expanding else v.train_bars,
        start=start,
    )
    index = ds.mask.index
    ens = np.full(len(ds.t_pos), np.nan)
    per_model: dict[str, np.ndarray] = {}
    prior = pd.Series(np.nan, index=index)
    mkt_score = pd.Series(np.nan, index=index)
    mkt_prior = pd.Series(np.nan, index=index)
    importance = None
    info: list[dict[str, object]] = []
    deep_prev = None
    use_deep = cfg.model.deep.enabled
    if use_deep:
        from hermes.models import deep as deep_mod

        use_deep = deep_mod.available()
        if use_deep:
            pos = deep_mod.build_pos(ds.t_pos, ds.s_pos, ds.n_bars, ds.mask.shape[1])
    for f in folds:
        t_start = time.time()
        models, ics = train_fold_models(ds, f.train, cfg)
        lcbs = {k[5:]: float(models.pop(k)) for k in list(models) if k.startswith("_lcb_")}  # type: ignore[arg-type]
        rows = ds.rows_for(f.test)
        preds = predict_rows(models, ds, rows)
        if use_deep:
            dm, dic, dlcb, drows, dsc = _deep_fold(ds, pos, f.train, f.test, cfg, deep_prev)
            deep_prev = dm
            ics["deep"], lcbs["deep"] = dic, dlcb
            p = np.full(len(ds.t_pos), np.nan)
            p[drows] = dsc
            preds["deep"] = cs_standardize(p[rows], ds.t_pos[rows])
        weights = _ensemble_weights(ics, cfg.model.ensemble)
        combo = sum(weights[k] * np.nan_to_num(preds[k]) for k in preds)
        ens[rows] = cs_standardize(combo, ds.t_pos[rows])
        for k, p in preds.items():
            per_model.setdefault(k, np.full(len(ds.t_pos), np.nan))[rows] = p
        fold_prior = max(
            0.0, sum(weights[k] * (lcbs.get(k, 0.0) if np.isfinite(lcbs.get(k, 0.0)) else 0.0) for k in ics)
        )
        prior.iloc[f.test] = fold_prior
        g = models.get("gbm")
        if g is not None and g.feature_importance is not None:  # type: ignore[attr-defined]
            imp = g.feature_importance / max(g.feature_importance.sum(), 1e-12)  # type: ignore[attr-defined]
            importance = imp if importance is None else importance + imp
        if cfg.model.market_model:
            mp, mc = _market_fold(ds, f.train, f.test, cfg)
            mkt_score.iloc[f.test] = mp
            mkt_prior.iloc[f.test] = max(0.0, mc)
        test_ic = mean_group_corr(np.nan_to_num(ens[rows]), np.nan_to_num(ds.y_raw[rows]), ds.t_pos[rows])
        rec = {
            "fold": f.name,
            "train_start": str(index[f.train.min()]),
            "test_start": str(index[f.test.min()]),
            "test_end": str(index[f.test.max()]),
            "val_ic": {k: round(float(v_), 4) for k, v_ in ics.items()},
            "prior_ic": round(float(fold_prior), 4),
            "weights": {k: round(float(v_), 3) for k, v_ in weights.items()},
            "test_ic": round(float(test_ic), 4),
            "seconds": round(time.time() - t_start, 1),
        }
        if g is not None:
            rec["gbm_trees"] = g.best_iterations  # type: ignore[attr-defined]
        info.append(rec)
        log.info(
            "fold %s test %s..%s val_ic=%s test_ic=%.4f (%.0fs)",
            f.name,
            rec["test_start"][:10],
            rec["test_end"][:10],
            rec["val_ic"],
            test_ic,
            rec["seconds"],
        )
    fi = pd.Series(importance, index=ds.feature_names).sort_values(ascending=False) if importance is not None else None
    return WalkForwardResult(
        score=ds.to_frame(ens),
        model_scores={k: ds.to_frame(p) for k, p in per_model.items()},
        prior_ic=prior,
        folds=info,
        feature_importance=fi,
        market_score=mkt_score if cfg.model.market_model else None,
        market_prior_ic=mkt_prior if cfg.model.market_model else None,
    )
