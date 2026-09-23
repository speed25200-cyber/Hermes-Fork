"""Train/serve skew and drift: population stability index (PSI) of each feature, live versus training.

The bundle stores, for every feature, decile edges and the share of rows per bin (plus a missing-value bin)
measured on the most recent training months. Live, the same bins are filled with the member rows of the
last day and compared: ``PSI = sum (a - e) ln(a / e)``. Common reading: < 0.1 stable, 0.1-0.25 shifting,
> 0.25 a different distribution -- a regime change, or a data/feature bug that the model would otherwise
trade on silently. It is a warning for the operator, never a trading input.
"""

from __future__ import annotations

import numpy as np

FLOOR = 1e-4


def _shares(x: np.ndarray, edges: np.ndarray) -> np.ndarray:
    fin = np.isfinite(x)
    counts = np.bincount(np.searchsorted(edges, x[fin], side="right"), minlength=len(edges) + 1).astype(float)
    out = np.append(counts, float((~fin).sum()))
    return out / max(len(x), 1)


def feature_profile(
    X: np.ndarray, names: list[str], n_bins: int = 10, max_rows: int = 200_000, seed: int = 0
) -> dict[str, dict[str, list[float]]]:
    """Decile edges and bin shares per feature (rows subsampled to ``max_rows``)."""
    rng = np.random.default_rng(seed)
    rows = np.arange(len(X)) if len(X) <= max_rows else np.sort(rng.choice(len(X), max_rows, replace=False))
    out: dict[str, dict[str, list[float]]] = {}
    qs = np.linspace(0, 1, n_bins + 1)[1:-1]
    for j, name in enumerate(names):
        x = np.asarray(X[rows, j], dtype=np.float64)
        fin = x[np.isfinite(x)]
        edges = np.unique(np.quantile(fin, qs)) if len(fin) else np.array([])
        out[name] = {"edges": [float(e) for e in edges], "expected": [float(s) for s in _shares(x, edges)]}
    return out


def psi(profile: dict[str, dict[str, list[float]]], X: np.ndarray, names: list[str]) -> dict[str, float]:
    """PSI of each profiled feature on the rows of ``X`` (columns in ``names`` order)."""
    out: dict[str, float] = {}
    if len(X) == 0:
        return out
    for j, name in enumerate(names):
        p = profile.get(name)
        if p is None:
            continue
        e = np.maximum(np.asarray(p["expected"], dtype=float), FLOOR)
        a = np.maximum(_shares(np.asarray(X[:, j], dtype=np.float64), np.asarray(p["edges"], dtype=float)), FLOOR)
        out[name] = float(np.sum((a - e) * np.log(a / e)))
    return out
