"""Covariance of contract returns: one-factor structure blended with a shrunk EWMA sample.

``S = (1 - d) * (beta beta' s_m^2 + diag(ivol^2)) + d * EWMA`` per bar, scaled to the holding horizon.
The factor part is well conditioned for any N; the EWMA part adds the residual co-movement (sectors,
narratives) that a single factor misses. ``d`` plays the role of a Ledoit-Wolf shrinkage intensity toward
the single-index target (Ledoit & Wolf 2003).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class EwmaCovariance:
    """Incremental EWMA second-moment matrix over a fixed symbol set (NaN returns treated as 0)."""

    def __init__(self, n: int, halflife: int):
        self.lam = 0.5 ** (1.0 / halflife)
        self.S = np.zeros((n, n))
        self.w = np.zeros((n, n))  # accumulated weight per pair, for bias correction
        self.count = 0

    def update(self, r: np.ndarray) -> None:
        ok = np.isfinite(r).astype(float)
        x = np.nan_to_num(r)
        self.S = self.lam * self.S + (1 - self.lam) * np.outer(x, x)
        self.w = self.lam * self.w + (1 - self.lam) * np.outer(ok, ok)
        self.count += 1

    def matrix(self, idx: np.ndarray) -> np.ndarray:
        S = self.S[np.ix_(idx, idx)]
        W = self.w[np.ix_(idx, idx)]
        return np.where(W > 1e-6, S / np.maximum(W, 1e-6), 0.0)


def factor_covariance(beta: np.ndarray, mkt_var: float, ivol: np.ndarray) -> np.ndarray:
    return np.outer(beta, beta) * mkt_var + np.diag(ivol**2)


def blended_covariance(
    beta: np.ndarray, mkt_var: float, ivol: np.ndarray, sample: np.ndarray | None, shrink: float = 0.3
) -> np.ndarray:
    F = factor_covariance(beta, mkt_var, ivol)
    if sample is None or shrink <= 0:
        return F
    S = (1 - shrink) * F + shrink * sample
    # Guard against indefiniteness from the pairwise-available sample.
    vals, vecs = np.linalg.eigh((S + S.T) / 2)
    vals = np.maximum(vals, 1e-12)
    return (vecs * vals) @ vecs.T


def market_variance(mkt: pd.Series, halflife: int) -> pd.Series:
    return (mkt**2).ewm(halflife=halflife, min_periods=24, adjust=False).mean()
