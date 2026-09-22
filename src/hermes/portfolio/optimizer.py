"""Cost-aware mean-variance portfolio construction.

At each rebalance the optimiser solves

    max_w   alpha' w  -  (lam/2) w' S w  -  kappa/2 (b' w)^2  -  sum_i c_i |w_i - w0_i|
    s.t.    |w_i| <= cap_i

with ``alpha`` the expected return over the holding horizon, ``S`` the covariance over that horizon,
``b`` the betas (a soft neutrality penalty, then an exact projection onto ``|b'w| <= net_max``), ``c`` the
linear cost rates and ``w0`` the current weights. The L1 turnover term creates an endogenous **no-trade
region**: a position is only changed when the marginal alpha exceeds its cost (Gârleanu & Pedersen 2013;
the NBIM no-trade-band rule). Solved by FISTA with a closed-form proximal step (soft-threshold around
``w0`` then clip to the box), warm-started from ``w0``.

Risk aversion ``lam`` is set from a *prior* IC, not from the current forecasts: if the model's recent
realised IC halves, positions halve -- vol-targeting never levers up noise.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class OptimizerResult:
    weights: np.ndarray
    iterations: int
    ex_ante_vol: float  # over the holding horizon
    scaled_by: float


def _prox(v: np.ndarray, w0: np.ndarray, thresh: np.ndarray, cap: np.ndarray) -> np.ndarray:
    d = v - w0
    w = w0 + np.sign(d) * np.maximum(np.abs(d) - thresh, 0.0)
    return np.clip(w, -cap, cap)


def project_exposure(w: np.ndarray, b: np.ndarray, cap: np.ndarray, bound: float) -> np.ndarray:
    """Shift ``w`` along ``b`` (then clip to the box) until ``|b'w| <= bound``.

    ``g(s) = b' clip(w - s b)`` is non-increasing in ``s`` for non-negative loadings, so the shift is found
    by bisection; the result satisfies the bound and the box exactly (up to tolerance).
    """
    e = float(b @ w)
    if abs(e) <= bound:
        return w
    target = float(np.clip(e, -bound, bound))
    bp = np.maximum(b, 0.0)

    def g(sh: float) -> float:
        return float(b @ np.clip(w - sh * bp, -cap, cap))

    lo, hi = 0.0, (e - target) / max(float(bp @ bp), 1e-12)
    for _ in range(60):  # expand until the bracket contains the root
        if (g(hi) - target) * (e - target) <= 0:
            break
        hi *= 2.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if (g(mid) - target) * (e - target) > 0:
            lo = mid
        else:
            hi = mid
    return np.clip(w - hi * bp, -cap, cap)


def solve(
    alpha: np.ndarray,
    cov: np.ndarray,
    w0: np.ndarray,
    cost: np.ndarray,
    cap: np.ndarray,
    lam: float,
    beta: np.ndarray | None = None,
    net_max: float = np.inf,
    kappa: float = 0.0,
    vol_cap: float = np.inf,
    gross_max: float = np.inf,
    max_iter: int = 300,
    tol: float = 1e-7,
) -> OptimizerResult:
    n = len(alpha)
    if n == 0:
        return OptimizerResult(np.zeros(0), 0, 0.0, 1.0)
    b = np.zeros(n) if beta is None else beta
    Q = lam * cov
    if kappa > 0:
        Q = Q + kappa * np.outer(b, b)
    L = float(np.linalg.norm(Q, 2)) + 1e-12
    step = 1.0 / L
    thresh = cost * step
    w = np.clip(w0.copy(), -cap, cap)
    y = w.copy()
    tk = 1.0
    it = 0
    for it in range(1, max_iter + 1):  # noqa: B007 (reported in the result)
        grad = Q @ y - alpha
        w_new = _prox(y - step * grad, w0, thresh, cap)
        t_new = 0.5 * (1 + np.sqrt(1 + 4 * tk * tk))
        y = w_new + ((tk - 1) / t_new) * (w_new - w)
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            break
        w, tk = w_new, t_new
    if np.isfinite(net_max) and np.any(b):
        w = project_exposure(w, b, cap, net_max)
    vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    scale = 1.0
    if vol > vol_cap > 0:
        scale = vol_cap / vol
    gross = float(np.abs(w).sum())
    if gross * scale > gross_max:
        scale = gross_max / gross
    if scale < 1.0:
        w = w * scale
        vol *= scale
    return OptimizerResult(w, it, vol, scale)


def risk_aversion(ic_ref: float, n_active: int, vol_target_horizon: float) -> float:
    """``lam`` such that a model performing at ``ic_ref`` on ``n_active`` names hits the vol target.

    With alpha_i = IC * z_i * s_i (s_i the residual vol over the horizon) and a diagonal covariance, the
    frictionless optimum ``w = alpha / (lam s^2)`` has volatility ``IC * sqrt(N) / lam``.
    """
    return max(ic_ref, 1e-4) * np.sqrt(max(n_active, 1)) / max(vol_target_horizon, 1e-9)
