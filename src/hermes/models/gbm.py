"""Gradient-boosted trees (LightGBM) -- the workhorse for tabular financial prediction.

Choices that matter on noisy, non-stationary data:

* very large ``min_data_in_leaf`` and strong L2: a leaf must be supported by thousands of samples;
* early stopping on the **cross-sectional IC** of a purged validation block (not on MSE), then a refit on
  the full window with the selected number of trees scaled by the extra data -- or, with
  ``early_stopping_rounds: 0``, a number of trees fixed in advance (a noisy validation block then decides
  nothing);
* Huber loss on Gauss-ranked targets: outliers cannot dominate;
* several seeds averaged (bagging over the stochastic parts of the fit).
"""

from __future__ import annotations

import lightgbm as lgb
import numpy as np

from hermes.config import GBMConfig
from hermes.models.base import TrainData, group_bounds, mean_group_corr


class GBMModel:
    name = "gbm"

    def __init__(self, cfg: GBMConfig, refit_full: bool = True):
        self.cfg = cfg
        self.refit_full = refit_full
        self.boosters: list[lgb.Booster] = []
        self.best_iterations: list[int] = []
        self.val_ic: float = float("nan")
        self.feature_importance: np.ndarray | None = None

    def _params(self, seed: int) -> dict[str, object]:
        c = self.cfg
        return {
            "objective": c.objective,
            "alpha": 1.5,  # huber transition, in target (z-score) units
            "learning_rate": c.learning_rate,
            "num_leaves": c.num_leaves,
            "min_data_in_leaf": c.min_data_in_leaf,
            "feature_fraction": c.feature_fraction,
            "bagging_fraction": c.bagging_fraction,
            "bagging_freq": c.bagging_freq,
            "lambda_l2": c.lambda_l2,
            "max_bin": c.max_bin,
            "verbosity": -1,
            "seed": seed,
            "deterministic": True,
            "force_row_wise": True,
            "num_threads": c.n_jobs,
        }

    def fit(self, data: TrainData, val: TrainData | None = None) -> GBMModel:
        self.boosters, self.best_iterations = [], []
        importance = None
        for seed in self.cfg.seeds:
            params = self._params(seed)
            n_iter = self.cfg.n_estimators
            if val is not None and len(val) > 0 and self.cfg.early_stopping_rounds > 0:
                dtrain = lgb.Dataset(data.X, data.y, weight=data.weight, free_raw_data=False)
                dval = lgb.Dataset(val.X, val.y, reference=dtrain)
                vb = val.groups

                def feval(preds: np.ndarray, _ds: lgb.Dataset, _y=val.y, _g=vb) -> tuple[str, float, bool]:
                    return "cs_ic", mean_group_corr(preds, _y, _g), True

                booster = lgb.train(
                    params,
                    dtrain,
                    num_boost_round=self.cfg.n_estimators,
                    valid_sets=[dval],
                    feval=feval,
                    callbacks=[
                        lgb.early_stopping(self.cfg.early_stopping_rounds, first_metric_only=True, verbose=False)
                    ],
                )
                n_iter = max(20, booster.best_iteration or self.cfg.n_estimators)
                if self.refit_full:
                    X = np.vstack([data.X, val.X])
                    y = np.concatenate([data.y, val.y])
                    w = None
                    if data.weight is not None and val.weight is not None:
                        w = np.concatenate([data.weight, val.weight])
                    scale = len(y) / max(len(data.y), 1)
                    booster = lgb.train(params, lgb.Dataset(X, y, weight=w), num_boost_round=int(n_iter * scale))
            else:
                booster = lgb.train(params, lgb.Dataset(data.X, data.y, weight=data.weight), num_boost_round=n_iter)
            self.boosters.append(booster)
            self.best_iterations.append(n_iter)
            imp = booster.feature_importance(importance_type="gain")
            importance = imp if importance is None else importance + imp
        self.feature_importance = importance
        if val is not None and len(val) > 0:
            self.val_ic = mean_group_corr(self.predict(val.X), val.y, val.groups)
        return self

    def predict(self, X: np.ndarray, groups: np.ndarray | None = None) -> np.ndarray:
        preds = [b.predict(X, num_threads=self.cfg.n_jobs) for b in self.boosters]
        return np.mean(preds, axis=0)


__all__ = ["GBMModel", "group_bounds"]
