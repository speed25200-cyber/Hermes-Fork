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


def _njit(fn):  # type: ignore[no-untyped-def]
    """Compile with numba when available (≈50x faster per solve); plain Python otherwise."""
    try:
        from numba import njit

        return njit(cache=True, fastmath=False)(fn)
    except Exception:  # pragma: no cover - numba missing or unsupported platform
        return fn


@_njit
def _fista(alpha, Q, w0, thresh, cap, step, max_iter, tol):  # type: ignore[no-untyped-def]
    """FISTA for 0.5 w'Qw - alpha'w + sum thresh/step |w - w0| subject to |w| <= cap."""
    n = alpha.shape[0]
    w = np.empty(n)
    for i in range(n):
        w[i] = min(max(w0[i], -cap[i]), cap[i])
    y = w.copy()
    w_new = np.empty(n)
    tk = 1.0
    it = 0
    for it in range(1, max_iter + 1):  # noqa: B007 (returned: iteration count)
        g = Q @ y - alpha
        diff = 0.0
        for i in range(n):
            v = y[i] - step * g[i] - w0[i]
            a = abs(v) - thresh[i]
            x = w0[i] + (np.sign(v) * a if a > 0.0 else 0.0)
            x = min(max(x, -cap[i]), cap[i])
            w_new[i] = x
            d = abs(x - w[i])
            if d > diff:
                diff = d
        t_new = 0.5 * (1.0 + np.sqrt(1.0 + 4.0 * tk * tk))
        mom = (tk - 1.0) / t_new
        for i in range(n):
            y[i] = w_new[i] + mom * (w_new[i] - w[i])
            w[i] = w_new[i]
        tk = t_new
        if diff < tol:
            break
    return w, it


@_njit
def _exposure_shift(w, b, cap, target, e):  # type: ignore[no-untyped-def]
    """Bisection for the shift s along max(b, 0) such that b' clip(w - s b+) = target."""
    n = w.shape[0]
    bp = np.maximum(b, 0.0)
    denom = 0.0
    for i in range(n):
        denom += bp[i] * bp[i]
    lo = 0.0
    hi = (e - target) / max(denom, 1e-12)
    for _ in range(60):
        acc = 0.0
        for i in range(n):
            acc += b[i] * min(max(w[i] - hi * bp[i], -cap[i]), cap[i])
        if (acc - target) * (e - target) <= 0.0:
            break
        hi *= 2.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        acc = 0.0
        for i in range(n):
            acc += b[i] * min(max(w[i] - mid * bp[i], -cap[i]), cap[i])
        if (acc - target) * (e - target) > 0.0:
            lo = mid
        else:
            hi = mid
    out = np.empty(n)
    for i in range(n):
        out[i] = min(max(w[i] - hi * bp[i], -cap[i]), cap[i])
    return out


def project_exposure(w: np.ndarray, b: np.ndarray, cap: np.ndarray, bound: float) -> np.ndarray:
    """Shift ``w`` along ``b`` (then clip to the box) until ``|b'w| <= bound``.

    ``g(s) = b' clip(w - s b)`` is non-increasing in ``s`` for non-negative loadings, so the shift is found
    by bisection; the result satisfies the bound and the box exactly (up to tolerance).
    """
    e = float(b @ w)
    if abs(e) <= bound:
        return w
    target = float(np.clip(e, -bound, bound))
    return _exposure_shift(
        np.ascontiguousarray(w, dtype=np.float64),
        np.ascontiguousarray(b, dtype=np.float64),
        np.ascontiguousarray(cap, dtype=np.float64),
        target,
        e,
    )


def _largest_eigenvalue(Q: np.ndarray, iters: int = 30) -> float:
    """Power iteration on the (symmetric PSD) Hessian: a few matvecs instead of an SVD per solve."""
    v = np.full(Q.shape[0], 1.0 / np.sqrt(Q.shape[0]))
    lam = 0.0
    for _ in range(iters):
        w = Q @ v
        nw = float(np.linalg.norm(w))
        if nw == 0.0:
            return 0.0
        v = w / nw
        if abs(nw - lam) <= 1e-6 * nw:
            lam = nw
            break
        lam = nw
    # Never below the largest diagonal element (a valid lower bound of the top eigenvalue).
    return max(lam, float(np.max(np.diag(Q))))


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
    max_iter: int = 200,
    tol: float = 1e-6,
    penalty_q: np.ndarray | None = None,
) -> OptimizerResult:
    """``penalty_q`` is added to the quadratic term only (e.g. style-factor risk priced for neutrality); the
    volatility cap and reported ex-ante volatility use ``cov`` alone."""
    n = len(alpha)
    if n == 0:
        return OptimizerResult(np.zeros(0), 0, 0.0, 1.0)
    b = np.zeros(n) if beta is None else beta
    Q = lam * cov
    if kappa > 0:
        Q = Q + kappa * np.outer(b, b)
    if penalty_q is not None:
        Q = Q + lam * penalty_q
    L = _largest_eigenvalue(Q) * 1.05 + 1e-12
    step = 1.0 / L
    thresh = cost * step
    w, it = _fista(
        np.ascontiguousarray(alpha, dtype=np.float64),
        np.ascontiguousarray(Q, dtype=np.float64),
        np.ascontiguousarray(w0, dtype=np.float64),
        np.ascontiguousarray(thresh, dtype=np.float64),
        np.ascontiguousarray(cap, dtype=np.float64),
        float(step),
        int(max_iter),
        float(tol),
    )
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
