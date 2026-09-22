"""Ridge regression on standardised features -- the honest baseline and an ensemble diversifier.

If trees cannot beat this model out of sample, the extra complexity is fitting noise.
"""

from __future__ import annotations

import numpy as np

from hermes.config import LinearConfig
from hermes.models.base import TrainData, mean_group_corr


class RidgeModel:
    name = "ridge"

    def __init__(self, cfg: LinearConfig):
        self.cfg = cfg
        self.mu: np.ndarray | None = None
        self.sd: np.ndarray | None = None
        self.coef: np.ndarray | None = None
        self.val_ic = float("nan")

    def _prep(self, X: np.ndarray) -> np.ndarray:
        assert self.mu is not None and self.sd is not None
        Z = (X.astype(np.float64) - self.mu) / self.sd
        Z = np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0)
        return np.clip(Z, -5, 5)

    def fit(self, data: TrainData, val: TrainData | None = None) -> RidgeModel:
        X = data.X.astype(np.float64)
        self.mu = np.nanmean(X, axis=0)
        sd = np.nanstd(X, axis=0)
        self.mu = np.nan_to_num(self.mu)
        self.sd = np.where(np.isfinite(sd) & (sd > 1e-12), sd, 1.0)
        Z = self._prep(data.X)
        y = data.y.astype(np.float64)
        w = np.ones(len(y)) if data.weight is None else data.weight.astype(np.float64)
        Zw = Z * w[:, None]
        A = Z.T @ Zw + self.cfg.alpha * len(y) / 1000.0 * np.eye(Z.shape[1])
        b = Zw.T @ (y - np.average(y, weights=w))
        self.coef = np.linalg.solve(A, b)
        if val is not None and len(val):
            self.val_ic = mean_group_corr(self.predict(val.X), val.y, val.groups)
        return self

    def predict(self, X: np.ndarray, groups: np.ndarray | None = None) -> np.ndarray:
        assert self.coef is not None
        return self._prep(X) @ self.coef
