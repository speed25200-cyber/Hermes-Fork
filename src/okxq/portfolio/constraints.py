"""Vérification INDÉPENDANTE des contraintes de portefeuille (§51).

Ce module ne dépend pas de CVXPY : il recalcule chaque contrainte en NumPy sur un vecteur de poids
candidat. L'optimiseur l'utilise après résolution, l'arrondi l'utilise après passage en contrats, et
les tests de propriété l'utilisent comme oracle. Convention : ``residual = utilisé − limite`` ; une
valeur strictement positive au-delà de la tolérance est une violation ; une valeur dans la bande
``active_tol`` autour de zéro signale une contrainte active.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from okxq.domain.events import PortfolioInputs

# Tolérances FIXES (documentées) : une violation sous ``FEASIBILITY_TOL`` est du bruit numérique.
FEASIBILITY_TOL = 1e-7
ACTIVE_TOL = 1e-5

Weights = Sequence[float] | np.ndarray
MarginChecker = Callable[[Sequence[float]], float]
"""Vérification EXACTE de la marge : poids signés → utilisation de marge (fraction de la capacité, 1 = plein)."""


@dataclass(frozen=True, slots=True)
class ConstraintReport:
    ok: bool
    active: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    residuals: dict[str, float] = field(default_factory=dict)
    finite: bool = True

    def worst_violation(self) -> float:
        return max((self.residuals[name] for name in self.violations), default=0.0)


def gross_exposure(w: np.ndarray) -> float:
    return float(np.sum(np.abs(w)))


def net_exposure(w: np.ndarray) -> float:
    return float(np.sum(w))


def _as_array(values: Weights, n: int, name: str) -> np.ndarray:
    arr = np.asarray(list(values), dtype=float)
    if arr.shape != (n,):
        raise ValueError(f"{name} : attendu {n} valeurs, reçu {arr.shape}")
    return arr


def verify_weights(
    inputs: PortfolioInputs,
    weights: Weights,
    *,
    tol: float = FEASIBILITY_TOL,
    active_tol: float = ACTIVE_TOL,
    margin_checker: MarginChecker | None = None,
    leg_offset_limit: float | None = None,
    include_change_limits: bool = True,
) -> ConstraintReport:
    """Recalcule toutes les contraintes de §51 sur ``weights`` (aligné sur ``inputs.instruments``).

    ``margin_checker`` : vérification exacte de la marge fournie par l'appelant (modèle de marge de
    l'exchange) ; elle s'ajoute à l'enveloppe linéaire conservatrice, elle ne la remplace pas.
    ``leg_offset_limit`` : si donné, borne |δ_i| par jambe (T42) en plus de la capacité de liquidité.
    ``include_change_limits=False`` n'évalue que les limites d'EXPOSITION (pas turnover ni liquidité) :
    c'est ce que l'on vérifie sur une allocation existante que l'on souhaite conserver.
    """
    n = len(inputs.instruments)
    w = _as_array(weights, n, "weights")
    residuals: dict[str, float] = {}
    if not np.all(np.isfinite(w)):
        return ConstraintReport(ok=False, violations=["finite"], residuals={"finite": math.inf}, finite=False)

    w0 = _as_array(inputs.w0, n, "w0")
    delta = w - w0
    residuals["gross"] = gross_exposure(w) - inputs.gross_limit
    residuals["net"] = abs(net_exposure(w)) - inputs.net_limit
    asset_limit = _as_array(inputs.asset_limit, n, "asset_limit")
    for i, inst in enumerate(inputs.instruments):
        residuals[f"asset:{inst}"] = abs(float(w[i])) - float(asset_limit[i])
    beta_btc = _as_array(inputs.beta_btc, n, "beta_btc")
    beta_eth = _as_array(inputs.beta_eth, n, "beta_eth")
    residuals["beta_btc"] = abs(float(beta_btc @ w)) - inputs.btc_beta_limit
    residuals["beta_eth"] = abs(float(beta_eth @ w)) - inputs.eth_beta_limit
    index = {inst: i for i, inst in enumerate(inputs.instruments)}
    for name, members in inputs.clusters.items():
        used = sum(abs(float(w[index[m]])) for m in members)
        residuals[f"cluster:{name}"] = used - inputs.cluster_limit
    if include_change_limits:
        residuals["turnover"] = float(np.sum(np.abs(delta))) - inputs.turnover_limit
    margin_unit = _as_array(inputs.margin_requirement_per_unit, n, "margin_requirement_per_unit")
    residuals["margin"] = float(margin_unit @ np.abs(w)) - inputs.margin_capacity
    if margin_checker is not None:
        exact = float(margin_checker([float(x) for x in w]))
        if not math.isfinite(exact):
            return ConstraintReport(
                ok=False, violations=["margin_exact"], residuals={"margin_exact": math.inf}, finite=False
            )
        residuals["margin_exact"] = exact - 1.0
    if include_change_limits:
        capacity = _as_array(inputs.liquidity_capacity, n, "liquidity_capacity")
        for i, inst in enumerate(inputs.instruments):
            cap = float(capacity[i])
            if leg_offset_limit is not None:
                cap = min(cap, leg_offset_limit)
            residuals[f"liquidity:{inst}"] = abs(float(delta[i])) - cap

    violations = [name for name, r in residuals.items() if r > tol]
    active = [name for name, r in residuals.items() if abs(r) <= active_tol]
    return ConstraintReport(ok=not violations, active=active, violations=violations, residuals=residuals)


def scale_to_admissible(inputs: PortfolioInputs, *, tol: float = FEASIBILITY_TOL) -> tuple[float, bool]:
    """Réduction déterministe (T41) : plus grand facteur ``s ∈ [0, 1]`` tel que ``s·w0`` respecte les
    limites d'exposition (brute, nette, actif, bêtas, clusters, marge), puis serrage par les limites de
    variation (turnover, liquidité) qui bornent la réduction possible en UNE décision.

    Retourne ``(s, fully_admissible)`` : ``fully_admissible`` est faux si les limites de variation
    empêchent d'atteindre une allocation admissible dès cette décision (la réduction est alors partielle
    et le Risk Engine reste le dernier rempart).
    """
    n = len(inputs.instruments)
    w0 = _as_array(inputs.w0, n, "w0")
    if not np.all(np.isfinite(w0)):
        return 0.0, False
    if np.allclose(w0, 0.0):
        return 1.0, True

    def exposure_ok(s: float) -> bool:
        return verify_weights(inputs, s * w0, tol=tol, include_change_limits=False).ok

    # Bissection déterministe sur s (les contraintes d'exposition sont monotones en s pour s ≥ 0).
    s_exposure = 1.0
    if not exposure_ok(1.0):
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if exposure_ok(mid):
                lo = mid
            else:
                hi = mid
        s_exposure = lo
    # Limites de variation : |(s−1)·w0| ≤ turnover et par jambe ≤ capacité → s ≥ s_lo.
    gross0 = gross_exposure(w0)
    s_lo_turnover = max(0.0, 1.0 - inputs.turnover_limit / gross0) if gross0 > 0 else 0.0
    caps = _as_array(inputs.liquidity_capacity, n, "liquidity_capacity")
    s_lo_liq = 0.0
    for i in range(n):
        a = abs(float(w0[i]))
        if a > 0:
            s_lo_liq = max(s_lo_liq, 1.0 - float(caps[i]) / a)
    s_lo = min(1.0, max(s_lo_turnover, s_lo_liq))
    s = max(s_exposure, s_lo)
    return float(s), bool(s <= s_exposure + 1e-12)
