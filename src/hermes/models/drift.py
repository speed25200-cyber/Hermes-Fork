"""Train/serve skew and drift: population stability index (PSI) of each feature, live versus training.

The bundle stores, for every feature, decile edges and the share of rows per bin (plus a missing-value bin)
measured on the most recent training months, the training range, and a calibrated alert threshold. Live, the
same bins are filled with the rows of a window and compared: ``PSI = sum (a - e) ln(a / e)``. The usual reading
(> 0.25: a different distribution) assumes independent rows; a slow or cyclical feature read on a short window
exceeds it with nothing wrong, so each feature's threshold is the PSI that windows of the same design reach on
the training months before the profile. It is a warning for the operator, never a trading input.
"""

from __future__ import annotations

import numpy as np

FLOOR = 1e-4
DRIFT_PSI_FLOOR = 0.25  # the usual 'different distribution' reading, the least a calibrated threshold can be
DRIFT_MARKET_DAYS = 7  # live window of market-level features: a whole weekly cycle


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
        if len(fin):
            out[name]["range"] = [float(fin.min()), float(fin.max())]
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


def null_quantiles(
    profile: dict[str, dict[str, list[float]]],
    X: np.ndarray,
    t_pos: np.ndarray,
    names: list[str],
    market: set[str],
    window_bars: int,
    market_window_bars: int,
    min_span_bars: int,
    stride: int = 1,
    q: float = 0.99,
) -> dict[str, float]:
    """Per-feature PSI that windows of the live design reach with no skew: the ``q`` quantile over every window
    (one start each ``stride`` bars) of the calibration rows (``X``, sorted by bar ``t_pos``) against ``profile``.

    Contract-level features are read on every member row of ``window_bars`` bars, market-level ones
    (``market``: one value a bar shared by all members) on one row a bar over ``market_window_bars`` bars, as the
    live engine does. Serial correlation, a weekly cycle and the few independent draws of a short window all
    raise the PSI of an unchanged feature; this threshold prices them in. A class of features is calibrated only
    when the rows span ``min_span_bars`` and four of its windows; it is a per-window reference measured on one
    past quarter, not a false-alarm rate.
    """
    if len(t_pos) == 0:
        return {}
    first = np.r_[0, np.nonzero(np.diff(t_pos))[0] + 1]  # first row of each bar
    out: dict[str, list[float]] = {}
    span_bars = int(t_pos[-1]) - int(t_pos[0]) + 1
    for is_market, span in ((False, window_bars), (True, market_window_bars)):
        cols = [j for j, k in enumerate(names) if k in profile and (k in market) == is_market]
        if not cols or span_bars < max(min_span_bars, 4 * span):
            continue
        sub = [names[j] for j in cols]
        for b0 in range(int(t_pos[0]), int(t_pos[-1]) - span + 2, max(1, stride)):
            lo, hi = np.searchsorted(t_pos, b0, "left"), np.searchsorted(t_pos, b0 + span, "left")
            rows = first[(first >= lo) & (first < hi)] if is_market else np.arange(lo, hi)
            if len(rows) < 2:
                continue
            for k, v in psi(profile, np.asarray(X[rows][:, cols], dtype=np.float32), sub).items():
                out.setdefault(k, []).append(v)
    return {k: float(np.quantile(v, q)) for k, v in out.items() if v}


def unseen_share(profile: dict[str, dict[str, list[float]]], X: np.ndarray, names: list[str]) -> dict[str, float]:
    """Share of rows per feature holding values training never produced: in bins with under 0.1 % of the
    training rows (missing where it had none, below a tied minimum), or beyond the training range by more than
    its width (a unit or scale bug; a new extreme of a real regime stays inside that margin). Unlike the PSI
    level, this does not depend on the window, so it flags a broken live feature even without a threshold."""
    out: dict[str, float] = {}
    if len(X) == 0:
        return out
    for j, name in enumerate(names):
        p = profile.get(name)
        if p is None:
            continue
        x = np.asarray(X[:, j], dtype=np.float64)
        edges = np.asarray(p["edges"], dtype=float)
        fin = np.isfinite(x)
        bins = np.where(fin, np.searchsorted(edges, np.where(fin, x, 0.0), side="right"), len(edges) + 1)
        bad = np.asarray(p["expected"], dtype=float)[bins] < 1e-3
        if p.get("range"):
            lo, hi = p["range"]
            w = max(hi - lo, 1e-12)
            bad |= fin & ((x < lo - w) | (x > hi + w))
        out[name] = float(bad.mean())
    return out


def drift_report(
    profile: dict[str, dict[str, list[float]]],
    blocks: list[tuple[np.ndarray, list[str]]],
    calibrated: bool = True,
) -> tuple[dict[str, float], list[str]]:
    """Risk fields and operator notes from windows of live rows (``(X, names)`` blocks).

    A feature drifts when its PSI exceeds its calibrated threshold (``null_q99``, floored at 0.25). Without
    thresholds (or with ``calibrated`` false: windows unlike those they were measured on) there is no drift
    verdict, only the window-free check for never-seen values."""
    values: dict[str, float] = {}
    unseen: dict[str, float] = {}
    for X, names in blocks:
        if len(X) and names:
            values.update(psi(profile, X, names))
            unseen.update(unseen_share(profile, X, names))
    if not values:
        return {}, []
    limits = {
        k: max(DRIFT_PSI_FLOOR, float(profile[k]["null_q99"][0]))
        for k in values
        if calibrated and profile[k].get("null_q99")
    }
    drifted = sorted(((values[k] / v, k) for k, v in limits.items() if values[k] > v), reverse=True)
    broken = sorted(((v, k) for k, v in unseen.items() if v > 0.5), reverse=True)
    notes = []
    if limits and len(drifted) > 0.1 * len(limits):
        worst = ", ".join(k for _, k in drifted[:3])
        notes.append(
            f"dérive des variables face à l'entraînement : {len(drifted)} au-delà de leur seuil calibré ({worst})"
        )
    if broken:
        worst = ", ".join(k for _, k in broken[:3])
        notes.append(
            f"valeurs jamais vues à l'entraînement (manquantes ou très hors plage) : {len(broken)} variables ({worst}),"
            " défaut de données probable"
        )
    risk = {
        "psi_max": round(max(values.values()), 3),
        "psi_ratio_max": round(max((values[k] / v for k, v in limits.items()), default=0.0), 3),
        "psi_drifted": float(len(drifted)),
        "psi_unseen": float(len(broken)),
        "psi_calibrated": float(bool(limits)),
        "psi_alert": float(bool(notes)),
    }
    return risk, notes
