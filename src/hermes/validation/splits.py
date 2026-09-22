"""Time-series cross-validation without leakage.

Labels at time ``t`` look ``h`` bars ahead, so a training sample at ``t`` overlaps any test sample in
``[t - h, t + h]``. Both schemes below **purge** training rows whose label window intersects the test block
and add an **embargo** after each test block (serial correlation of features/labels), following
López de Prado (2018, ch. 7 & 12).

* :func:`walk_forward` -- the production-faithful scheme: train on the past only, predict the next block,
  roll. Its out-of-fold predictions are what the backtest trades.
* :func:`cpcv` -- combinatorial purged CV: ``C(n_groups, k)`` train/test splits whose test blocks
  recombine into ``C(n_groups-1, k-1)`` full backtest paths, giving a *distribution* of out-of-sample
  performance (and the input of the PBO statistic) instead of a single path.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np


@dataclass(frozen=True)
class Fold:
    train: np.ndarray  # bar positions used for training
    test: np.ndarray  # bar positions predicted
    name: str = ""


def _purge(train: np.ndarray, test_start: int, test_end: int, horizon: int, embargo: int) -> np.ndarray:
    """Drop training bars whose label window [t, t+horizon] touches [test_start - 0, test_end + embargo]."""
    lo = test_start - horizon
    hi = test_end + embargo
    return train[(train < lo) | (train > hi)]


def walk_forward(
    n_bars: int,
    test_bars: int,
    min_train_bars: int,
    horizon: int,
    embargo: int,
    train_bars: int | None = None,
    start: int = 0,
) -> list[Fold]:
    """Expanding (``train_bars=None``) or rolling walk-forward folds over positions ``[start, n_bars)``."""
    folds: list[Fold] = []
    t0 = start + min_train_bars
    k = 0
    while t0 < n_bars:
        t1 = min(n_bars, t0 + test_bars)
        lo = start if train_bars is None else max(start, t0 - train_bars)
        # Only labels fully observed before the test block may be used: t + horizon < t0.
        train = np.arange(lo, max(lo, t0 - horizon))
        if len(train) >= min(min_train_bars, n_bars) // 2:
            folds.append(Fold(train=train, test=np.arange(t0, t1), name=f"wf{k:03d}"))
            k += 1
        t0 = t1
    return folds


def cpcv(n_bars: int, n_groups: int, k_test: int, horizon: int, embargo: int) -> list[Fold]:
    edges = np.linspace(0, n_bars, n_groups + 1).astype(int)
    groups = [np.arange(edges[i], edges[i + 1]) for i in range(n_groups)]
    folds = []
    for combo in combinations(range(n_groups), k_test):
        test = np.concatenate([groups[g] for g in combo])
        train = np.arange(n_bars)
        for g in combo:
            train = _purge(train, groups[g][0], groups[g][-1], horizon, embargo)
        folds.append(Fold(train=train, test=test, name="cpcv" + "-".join(map(str, combo))))
    return folds


def cpcv_paths(n_groups: int, k_test: int) -> list[list[tuple[int, int]]]:
    """Assign each (split, group) test block to a backtest path.

    Each group appears in ``C(n-1, k-1)`` splits; the ``j``-th occurrence of group ``g`` goes to path ``j``.
    Returns, for each path, the list of ``(split_index, group)`` pairs that compose it.
    """
    combos = list(combinations(range(n_groups), k_test))
    n_paths = len(list(combinations(range(n_groups - 1), k_test - 1)))
    seen = [0] * n_groups
    paths: list[list[tuple[int, int]]] = [[] for _ in range(n_paths)]
    for s, combo in enumerate(combos):
        for g in combo:
            paths[seen[g]].append((s, g))
            seen[g] += 1
    return paths
