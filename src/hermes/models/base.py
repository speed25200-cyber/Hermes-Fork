"""Model interface and shared helpers.

Tabular models see a long matrix ``X`` (rows = (time, symbol) samples) with an integer ``groups`` vector
giving each row's timestamp; the cross-sectional structure matters for training targets and evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
from scipy.special import ndtri


class Model(Protocol):
    name: str

    def fit(self, data: TrainData, val: TrainData | None = None) -> Model: ...

    def predict(self, X: np.ndarray, groups: np.ndarray | None = None) -> np.ndarray: ...


@dataclass
class TrainData:
    X: np.ndarray  # float32 (n x f)
    y: np.ndarray  # float32 (n,)
    groups: np.ndarray  # int (n,) timestamp position, sorted ascending
    weight: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self.y)


def group_bounds(groups: np.ndarray) -> np.ndarray:
    """Start offsets of each contiguous group (groups must be sorted), with the end appended."""
    if len(groups) == 0:
        return np.array([0])
    change = np.nonzero(np.diff(groups))[0] + 1
    return np.concatenate([[0], change, [len(groups)]])


def cs_gauss_rank(values: np.ndarray, groups: np.ndarray) -> np.ndarray:
    """Per-timestamp rank -> normal scores; NaN preserved. Robust target transform."""
    out = np.full(len(values), np.nan, dtype=np.float64)
    b = group_bounds(groups)
    for i in range(len(b) - 1):
        seg = values[b[i] : b[i + 1]]
        ok = np.isfinite(seg)
        k = int(ok.sum())
        if k < 3:
            continue
        r = np.empty(k)
        r[np.argsort(seg[ok], kind="mergesort")] = np.arange(1, k + 1)
        res = np.full(len(seg), np.nan)
        res[ok] = ndtri((r - 0.5) / k)
        out[b[i] : b[i + 1]] = res
    return out


def cs_standardize(values: np.ndarray, groups: np.ndarray) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=np.float64)
    b = group_bounds(groups)
    for i in range(len(b) - 1):
        seg = values[b[i] : b[i + 1]].astype(np.float64)
        ok = np.isfinite(seg)
        if ok.sum() < 3:
            continue
        m, s = seg[ok].mean(), seg[ok].std()
        out[b[i] : b[i + 1]] = (seg - m) / s if s > 0 else 0.0
    return out


def group_corrs(pred: np.ndarray, y: np.ndarray, groups: np.ndarray) -> np.ndarray:
    """Per-timestamp Pearson correlations (the IC the portfolio actually monetises)."""
    b = group_bounds(groups)
    out = []
    for i in range(len(b) - 1):
        p = pred[b[i] : b[i + 1]]
        t = y[b[i] : b[i + 1]]
        if len(p) < 3:
            continue
        p = p - p.mean()
        t = t - t.mean()
        d = np.sqrt((p @ p) * (t @ t))
        if d > 0:
            out.append((p @ t) / d)
    return np.asarray(out, dtype=np.float64)


def mean_group_corr(pred: np.ndarray, y: np.ndarray, groups: np.ndarray) -> float:
    c = group_corrs(pred, y, groups)
    return float(c.mean()) if len(c) else 0.0


def ic_lower_bound(pred: np.ndarray, y: np.ndarray, groups: np.ndarray, overlap: int, n_se: float = 1.5) -> float:
    """Mean IC minus ``n_se`` standard errors, the SE inflated for overlapping labels.

    Early stopping selects the iteration that maximises validation IC, which biases that IC upward; the
    lower bound is the prior the sizing layer may rely on before live evidence accumulates.
    """
    c = group_corrs(pred, y, groups)
    if len(c) < 10:
        return 0.0
    se = c.std(ddof=1) / np.sqrt(max(len(c) / max(overlap, 1), 1.0))
    return float(c.mean() - n_se * se)
