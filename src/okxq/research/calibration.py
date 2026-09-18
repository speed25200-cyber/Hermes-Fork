"""Calibration des probabilités et des intervalles (§39).

- ``brier_score`` ; ``log_loss`` protégé (probabilités bornées dans ``[eps, 1-eps]``) ;
- ``expected_calibration_error`` (ECE) sur ``n_bins`` quantiles ou bornes fixes ;
- ``reliability_curve`` : par bin, moyenne prédite, fréquence observée, effectif et intervalle de Wilson ;
- ``PlattCalibrator`` / ``IsotonicCalibrator`` : transformateurs à provenance (``FittedTransformer``) ;
  ils s'ajustent hors test et ``assert_not_fitted_on`` protège (T17) ;
- ``interval_coverage`` / ``interval_width`` pour les quantiles de prévision ; ``pinball_loss``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from okxq.research.splits import FittedTransformer, register_transformer_kind

EPS = 1e-6


def _check(p: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    if p.shape != y.shape or p.ndim != 1:
        raise ValueError("p et y doivent être des vecteurs alignés")
    if p.size == 0:
        raise ValueError("vecteurs vides")
    if np.any(p < 0) or np.any(p > 1) or not np.all(np.isfinite(p)):
        raise ValueError("probabilités hors [0, 1]")
    if not np.all((y == 0) | (y == 1)):
        raise ValueError("y doit être binaire 0/1")
    return p, y


def brier_score(p: np.ndarray, y: np.ndarray) -> float:
    p, y = _check(p, y)
    return float(np.mean((p - y) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray, *, eps: float = EPS) -> float:
    p, y = _check(p, y)
    q = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    phat = successes / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def reliability_curve(
    p: np.ndarray, y: np.ndarray, *, n_bins: int = 10, strategy: str = "quantile"
) -> list[dict[str, Any]]:
    p, y = _check(p, y)
    if strategy == "quantile":
        edges = np.unique(np.quantile(p, np.linspace(0, 1, n_bins + 1)))
    else:
        edges = np.linspace(0, 1, n_bins + 1)
    if edges.size < 2:
        edges = np.array([0.0, 1.0])
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, edges.size - 2)
    out: list[dict[str, Any]] = []
    for b in range(edges.size - 1):
        sel = idx == b
        n = int(np.sum(sel))
        if n == 0:
            continue
        succ = int(np.sum(y[sel]))
        lo, hi = wilson_interval(succ, n)
        out.append(
            {
                "bin": b,
                "lower_edge": float(edges[b]),
                "upper_edge": float(edges[b + 1]),
                "count": n,
                "mean_predicted": float(np.mean(p[sel])),
                "observed_frequency": succ / n,
                "ci_low": lo,
                "ci_high": hi,
            }
        )
    return out


def expected_calibration_error(p: np.ndarray, y: np.ndarray, *, n_bins: int = 10) -> float:
    curve = reliability_curve(p, y, n_bins=n_bins)
    total = sum(b["count"] for b in curve)
    return float(sum(b["count"] / total * abs(b["mean_predicted"] - b["observed_frequency"]) for b in curve))


def calibration_report(p: np.ndarray, y: np.ndarray, *, n_bins: int = 10) -> dict[str, Any]:
    return {
        "n": int(np.asarray(p).size),
        "brier": brier_score(p, y),
        "log_loss": log_loss(p, y),
        "ece": expected_calibration_error(p, y, n_bins=n_bins),
        "base_rate": float(np.mean(np.asarray(y, dtype=float))),
        "reliability": reliability_curve(p, y, n_bins=n_bins),
    }


class PlattCalibrator(FittedTransformer):
    """Sigmoïde ``σ(a·logit(p) + b)`` ajustée par Newton sur la log-vraisemblance (colonne 0 = p, colonne 1 = y)."""

    kind = "platt_calibrator"

    def __init__(self, max_iter: int = 100) -> None:
        super().__init__()
        self.max_iter = max_iter
        self.a: float = 1.0
        self.b: float = 0.0

    def _fit(self, x: np.ndarray) -> None:
        p, y = _check(x[:, 0], x[:, 1])
        z = np.log(np.clip(p, EPS, 1 - EPS) / (1 - np.clip(p, EPS, 1 - EPS)))
        a, b = 1.0, 0.0
        for _ in range(self.max_iter):
            s = 1.0 / (1.0 + np.exp(-(a * z + b)))
            w = s * (1 - s) + 1e-9
            g = np.array([np.sum((s - y) * z), np.sum(s - y)]) + 1e-3 * np.array([a - 1.0, b])
            h = np.array([[np.sum(w * z * z), np.sum(w * z)], [np.sum(w * z), np.sum(w)]]) + 1e-3 * np.eye(2)
            step = np.linalg.solve(h, g)
            a, b = a - float(step[0]), b - float(step[1])
            if float(np.max(np.abs(step))) < 1e-8:
                break
        self.a, self.b = a, b

    def _transform(self, x: np.ndarray) -> np.ndarray:
        p = np.clip(np.asarray(x[:, 0], dtype=float), EPS, 1 - EPS)
        z = np.log(p / (1 - p))
        return 1.0 / (1.0 + np.exp(-(self.a * z + self.b)))

    def _state(self) -> dict[str, Any]:
        return {"a": self.a, "b": self.b, "max_iter": self.max_iter}

    def _load_state(self, state: dict[str, Any]) -> None:
        self.a, self.b, self.max_iter = float(state["a"]), float(state["b"]), int(state["max_iter"])


class IsotonicCalibrator(FittedTransformer):
    """Régression isotone (scikit-learn), bornée dans [0, 1] ; sérialisée par ses points de cassure."""

    kind = "isotonic_calibrator"

    def __init__(self) -> None:
        super().__init__()
        self.x_thresholds: np.ndarray = np.zeros(0)
        self.y_thresholds: np.ndarray = np.zeros(0)

    def _fit(self, x: np.ndarray) -> None:
        from sklearn.isotonic import IsotonicRegression

        p, y = _check(x[:, 0], x[:, 1])
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso.fit(p, y)
        self.x_thresholds = np.asarray(iso.X_thresholds_, dtype=float)
        self.y_thresholds = np.asarray(iso.y_thresholds_, dtype=float)

    def _transform(self, x: np.ndarray) -> np.ndarray:
        p = np.asarray(x[:, 0], dtype=float)
        return np.interp(p, self.x_thresholds, self.y_thresholds)

    def _state(self) -> dict[str, Any]:
        return {"x": self.x_thresholds.tolist(), "y": self.y_thresholds.tolist()}

    def _load_state(self, state: dict[str, Any]) -> None:
        self.x_thresholds = np.array(state["x"], dtype=float)
        self.y_thresholds = np.array(state["y"], dtype=float)


register_transformer_kind(PlattCalibrator)
register_transformer_kind(IsotonicCalibrator)


def interval_coverage(y: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> float:
    y, lower, upper = (np.asarray(v, dtype=float) for v in (y, lower, upper))
    if y.shape != lower.shape or y.shape != upper.shape or y.size == 0:
        raise ValueError("vecteurs désalignés ou vides")
    return float(np.mean((y >= lower) & (y <= upper)))


def interval_width(lower: np.ndarray, upper: np.ndarray) -> float:
    return float(np.mean(np.asarray(upper, dtype=float) - np.asarray(lower, dtype=float)))


def pinball_loss(y: np.ndarray, q_pred: np.ndarray, tau: float) -> float:
    if not 0 < tau < 1:
        raise ValueError("tau dans (0, 1)")
    d = np.asarray(y, dtype=float) - np.asarray(q_pred, dtype=float)
    return float(np.mean(np.maximum(tau * d, (tau - 1) * d)))
