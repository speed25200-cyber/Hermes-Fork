"""Optimiseur de portefeuille convexe (§51) — ``PortfolioBuilder`` sur CVXPY + CLARABEL.

Formulation EXACTE résolue (poids ``w`` en multiples d'equity, ``δ = w − w0``) :

    max  mu·w − ½·λ·wᵀΣw − Σ_i (c_buy_i·pos(δ_i) + c_sell_i·pos(−δ_i)) − Σ_i pen_i·|w_i|
         − funding(w) − exit_cost(w)

    avec funding(w)   = Σ_i pos(f_i·w_i)      (on paie le funding attendu, on ne compte JAMAIS celui
                                              que l'on recevrait : choix conservateur documenté)
         exit_cost(w) = Σ_i e_i·|w_i|         (coût futur de sortie de la position tenue)

    s.c. Σ|w| ≤ gross ; |Σw| ≤ net ; |w_i| ≤ asset_i ; |β_btc·w| ≤ b_btc ; |β_eth·w| ≤ b_eth ;
         Σ_{i∈C}|w_i| ≤ cluster (∀C) ; Σ|δ| ≤ turnover ; Σ m_i·|w_i| ≤ margin_capacity (enveloppe
         convexe conservatrice) ; |δ_i| ≤ min(liquidity_i, max_leg_offset).

``mu`` est BRUT de coûts ; les coûts d'entrée ne portent que sur la VARIATION. Après résolution, le
candidat est revérifié INDÉPENDAMMENT (``okxq.portfolio.constraints``), marge exacte comprise si un
vérificateur est fourni. Une solution ``optimal_inaccurate`` n'est jamais approuvée automatiquement.

Fallback (T41) : timeout, infaisabilité, erreur numérique, NaN ou vérification échouée → on ne choisit
JAMAIS les plus gros scores ; on conserve l'allocation antérieure si elle reste admissible, sinon on
applique la réduction déterministe ``scale_to_admissible``. Un target porte une expiration ; un target
expiré est rejeté par ``assert_target_fresh``.

Risque en cours d'exécution (T42) : le décalage maximal par jambe ``max_leg_offset`` borne |δ_i| ; le
rapport d'exécution partielle liste les jambes dont l'exécution isolée dépasserait une limite
d'exposition, pour que la génération d'ordres séquence les réductions avant les augmentations et que
le Risk Engine réserve chaque jambe séparément.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import cvxpy as cp
import numpy as np

from okxq.domain.clocks import Clock, ensure_utc
from okxq.domain.errors import SolverError
from okxq.domain.events import PortfolioInputs, PortfolioTarget
from okxq.domain.ids import new_id
from okxq.domain.money import dec_from_float
from okxq.domain.reasons import ReasonCode
from okxq.portfolio.constraints import (
    ConstraintReport,
    MarginChecker,
    scale_to_admissible,
    verify_weights,
)
from okxq.portfolio.covariance import is_symmetric_psd

# CVXPY n'expose pas de types pour ses atomes (sum, abs, pos, quad_form...) : accès dynamique localisé ici.
_cvx: Any = cp

WEIGHT_PLACES = 10
SOLVER_VERIFY_TOL = 1e-6  # tolérance de revérification d'une solution numérique (≥ tol_feas du solveur)

STATUS_OPTIMAL = "optimal"
STATUS_INACCURATE = "optimal_inaccurate"
STATUS_FALLBACK_KEEP = "fallback_keep_previous"
STATUS_FALLBACK_REDUCE = "fallback_reduce"


@dataclass(frozen=True, slots=True)
class SolverSettings:
    """Tolérances FIXES de CLARABEL (documentées, jamais relâchées pour faire passer un cas)."""

    tol_gap_abs: float = 1e-8
    tol_gap_rel: float = 1e-8
    tol_feas: float = 1e-8
    max_iter: int = 500
    time_limit_s: float = 2.0


@dataclass(frozen=True, slots=True)
class SolveOutcome:
    status: str
    weights: np.ndarray | None
    objective: float | None
    solve_ms: int
    error: str | None = None


SolveFn = Callable[[PortfolioInputs, SolverSettings], SolveOutcome]


@dataclass(frozen=True, slots=True)
class PartialExecutionReport:
    """Exposition intermédiaire si UNE seule jambe s'exécute depuis ``w0`` (T42)."""

    ok: bool
    max_leg_offset: float | None
    worst_gross: float
    worst_abs_net: float
    worst_abs_beta_btc: float
    worst_abs_beta_eth: float
    legs_at_risk: list[str] = field(default_factory=list)


def build_problem(
    inputs: PortfolioInputs, *, max_leg_offset: float | None = None
) -> tuple[cp.Problem, cp.Variable]:
    """Construit le problème §51 tel quel (utile pour inspection et tests de formulation)."""
    n = len(inputs.instruments)
    mu = np.asarray(inputs.mu, dtype=float)
    sigma = np.asarray(inputs.sigma, dtype=float)
    if not is_symmetric_psd(sigma, tol=1e-9):
        raise SolverError("sigma doit être symétrique semi-définie positive (voir okxq.portfolio.covariance)")
    w0 = np.asarray(inputs.w0, dtype=float)
    c_buy = np.asarray(inputs.cost_buy, dtype=float)
    c_sell = np.asarray(inputs.cost_sell, dtype=float)
    pen = np.asarray(inputs.uncertainty_penalty, dtype=float)
    fund = np.asarray(inputs.expected_funding_cost, dtype=float)
    exit_c = np.asarray(inputs.future_exit_cost, dtype=float)
    asset = np.asarray(inputs.asset_limit, dtype=float)
    cap = np.asarray(inputs.liquidity_capacity, dtype=float)
    if max_leg_offset is not None:
        cap = np.minimum(cap, max_leg_offset)
    beta_btc = np.asarray(inputs.beta_btc, dtype=float)
    beta_eth = np.asarray(inputs.beta_eth, dtype=float)
    margin_unit = np.asarray(inputs.margin_requirement_per_unit, dtype=float)
    for name, arr in (
        ("cost_buy", c_buy),
        ("cost_sell", c_sell),
        ("uncertainty_penalty", pen),
        ("future_exit_cost", exit_c),
    ):
        if np.any(arr < 0):
            raise SolverError(f"{name} contient une valeur négative : la formulation ne serait plus convexe")

    w: Any = _cvx.Variable(n, name="w")
    delta = w - w0
    expected = mu @ w
    variance = 0.5 * inputs.risk_aversion * _cvx.quad_form(w, _cvx.psd_wrap(sigma))
    entry_cost = c_buy @ _cvx.pos(delta) + c_sell @ _cvx.pos(-delta)
    penalty = pen @ _cvx.abs(w)
    funding = _cvx.sum(_cvx.pos(_cvx.multiply(fund, w)))
    exit_cost = exit_c @ _cvx.abs(w)
    objective = _cvx.Maximize(expected - variance - entry_cost - penalty - funding - exit_cost)

    constraints: list[Any] = [
        _cvx.sum(_cvx.abs(w)) <= inputs.gross_limit,
        _cvx.abs(_cvx.sum(w)) <= inputs.net_limit,
        _cvx.abs(w) <= asset,
        _cvx.abs(beta_btc @ w) <= inputs.btc_beta_limit,
        _cvx.abs(beta_eth @ w) <= inputs.eth_beta_limit,
        _cvx.sum(_cvx.abs(delta)) <= inputs.turnover_limit,
        margin_unit @ _cvx.abs(w) <= inputs.margin_capacity,
        _cvx.abs(delta) <= cap,
    ]
    index = {inst: i for i, inst in enumerate(inputs.instruments)}
    for members in inputs.clusters.values():
        idx = [index[m] for m in members]
        if idx:
            constraints.append(_cvx.sum(_cvx.abs(w[idx])) <= inputs.cluster_limit)
    problem: cp.Problem = _cvx.Problem(objective, constraints)
    return problem, w


def cvxpy_solve(
    inputs: PortfolioInputs, settings: SolverSettings, *, max_leg_offset: float | None = None
) -> SolveOutcome:
    """Résolution CLARABEL. Toute exception devient un statut ``error`` (→ fallback), jamais un crash."""
    started = time.perf_counter_ns()
    try:
        problem, w = build_problem(inputs, max_leg_offset=max_leg_offset)
        problem.solve(  # type: ignore[no-untyped-call]
            solver=_cvx.CLARABEL,
            tol_gap_abs=settings.tol_gap_abs,
            tol_gap_rel=settings.tol_gap_rel,
            tol_feas=settings.tol_feas,
            max_iter=settings.max_iter,
            time_limit=settings.time_limit_s,
        )
    except SolverError:
        raise
    except Exception as exc:  # cvxpy.SolverError, erreurs numériques, etc.
        ms = (time.perf_counter_ns() - started) // 1_000_000
        return SolveOutcome(status="error", weights=None, objective=None, solve_ms=int(ms), error=str(exc))
    ms = (time.perf_counter_ns() - started) // 1_000_000
    value = w.value
    weights = None if value is None else np.asarray(value, dtype=float).reshape(-1)
    objective = None if problem.value is None else float(problem.value)
    return SolveOutcome(status=str(problem.status), weights=weights, objective=objective, solve_ms=int(ms))


def partial_execution_report(
    inputs: PortfolioInputs, weights: np.ndarray, *, max_leg_offset: float | None
) -> PartialExecutionReport:
    n = len(inputs.instruments)
    w0 = np.asarray(inputs.w0, dtype=float)
    beta_btc = np.asarray(inputs.beta_btc, dtype=float)
    beta_eth = np.asarray(inputs.beta_eth, dtype=float)
    delta = weights - w0
    worst_gross = float(np.sum(np.abs(w0)))
    worst_net = abs(float(np.sum(w0)))
    worst_bb = abs(float(beta_btc @ w0))
    worst_be = abs(float(beta_eth @ w0))
    at_risk: list[str] = []
    for i in range(n):
        if delta[i] == 0:
            continue
        inter = w0.copy()
        inter[i] = weights[i]
        g = float(np.sum(np.abs(inter)))
        nt = abs(float(np.sum(inter)))
        bb = abs(float(beta_btc @ inter))
        be = abs(float(beta_eth @ inter))
        worst_gross, worst_net = max(worst_gross, g), max(worst_net, nt)
        worst_bb, worst_be = max(worst_bb, bb), max(worst_be, be)
        breaches = (
            g > inputs.gross_limit + SOLVER_VERIFY_TOL
            or nt > inputs.net_limit + SOLVER_VERIFY_TOL
            or bb > inputs.btc_beta_limit + SOLVER_VERIFY_TOL
            or be > inputs.eth_beta_limit + SOLVER_VERIFY_TOL
        )
        if breaches:
            at_risk.append(inputs.instruments[i])
    ok = not at_risk
    if max_leg_offset is not None and np.any(np.abs(delta) > max_leg_offset + SOLVER_VERIFY_TOL):
        ok = False
        at_risk = sorted(
            set(at_risk) | {inputs.instruments[i] for i in range(n) if abs(delta[i]) > max_leg_offset}
        )
    return PartialExecutionReport(
        ok=ok,
        max_leg_offset=max_leg_offset,
        worst_gross=worst_gross,
        worst_abs_net=worst_net,
        worst_abs_beta_btc=worst_bb,
        worst_abs_beta_eth=worst_be,
        legs_at_risk=at_risk,
    )


def _finite_residuals(report: ConstraintReport) -> dict[str, float]:
    return {k: float(v) for k, v in report.residuals.items() if math.isfinite(v)}


class CvxpyPortfolioBuilder:
    """``PortfolioBuilder`` (domain.protocols) : résout §51, vérifie, et retombe sûrement (T41)."""

    def __init__(
        self,
        *,
        clock: Clock,
        target_ttl_s: int,
        settings: SolverSettings | None = None,
        max_leg_offset: float | None = None,
        margin_checker: MarginChecker | None = None,
        solve_fn: SolveFn | None = None,
    ) -> None:
        if target_ttl_s <= 0:
            raise SolverError("target_ttl_s doit être strictement positif")
        if max_leg_offset is not None and max_leg_offset <= 0:
            raise SolverError("max_leg_offset doit être strictement positif")
        self._clock = clock
        self._ttl = timedelta(seconds=target_ttl_s)
        self._settings = settings or SolverSettings()
        self._max_leg_offset = max_leg_offset
        self._margin_checker = margin_checker
        self._solve: SolveFn = solve_fn or (
            lambda inp, st: cvxpy_solve(inp, st, max_leg_offset=self._max_leg_offset)
        )

    # --- API publique ---------------------------------------------------------------------------------

    def optimize(self, inputs: PortfolioInputs) -> PortfolioTarget:
        outcome = self._solve(inputs, self._settings)
        reasons: list[str] = []
        weights: np.ndarray | None = None
        status = outcome.status
        objective = outcome.objective

        if outcome.status == STATUS_OPTIMAL and outcome.weights is not None:
            candidate = outcome.weights
            if candidate.shape != (len(inputs.instruments),) or not np.all(np.isfinite(candidate)):
                reasons.append(ReasonCode.SOLVER_FALLBACK.value)
                status = "nan"
            else:
                report = verify_weights(
                    inputs,
                    candidate,
                    tol=SOLVER_VERIFY_TOL,
                    margin_checker=self._margin_checker,
                    leg_offset_limit=self._max_leg_offset,
                )
                if report.ok:
                    weights = candidate
                else:
                    reasons.extend([ReasonCode.SOLVER_FALLBACK.value, ReasonCode.RISK_LIMIT.value])
                    status = "verification_failed:" + ",".join(report.violations)
        elif outcome.status == STATUS_INACCURATE:
            reasons.extend([ReasonCode.SOLVER_INACCURATE.value, ReasonCode.SOLVER_FALLBACK.value])
        else:
            reasons.append(ReasonCode.SOLVER_FALLBACK.value)

        if weights is None:
            weights, status, fallback_reasons = self._fallback(inputs)
            reasons.extend(fallback_reasons)
            objective = None

        final_report = verify_weights(
            inputs,
            weights,
            tol=SOLVER_VERIFY_TOL,
            margin_checker=self._margin_checker,
            leg_offset_limit=self._max_leg_offset,
            include_change_limits=status != STATUS_FALLBACK_KEEP,
        )
        partial = partial_execution_report(inputs, weights, max_leg_offset=self._max_leg_offset)
        if not partial.ok:
            reasons.append(ReasonCode.PARTIAL_EXECUTION_RISK.value)
        if not final_report.ok:
            reasons.append(ReasonCode.RISK_LIMIT.value)
        now = self._clock.now_utc()
        signed = {
            inst: dec_from_float(float(w), WEIGHT_PLACES, field=f"w[{inst}]")
            for inst, w in zip(inputs.instruments, weights, strict=True)
        }
        residuals = _finite_residuals(final_report)
        residuals["partial_execution_worst_abs_net"] = partial.worst_abs_net
        residuals["partial_execution_worst_gross"] = partial.worst_gross
        return PortfolioTarget(
            target_id=new_id("tgt"),
            snapshot_id=inputs.snapshot_id,
            equity_version=inputs.equity_version,
            signed_weights=signed,
            constraints_version=inputs.constraints_version,
            solver_status=status,
            objective_value=objective if objective is not None and math.isfinite(objective) else None,
            active_constraints=list(final_report.active),
            residuals=residuals,
            solve_ms=max(0, outcome.solve_ms),
            created_at=now,
            expires_at=now + self._ttl,
            reason_codes=_dedupe(reasons),
        )

    # --- fallback -------------------------------------------------------------------------------------

    def _fallback(self, inputs: PortfolioInputs) -> tuple[np.ndarray, str, list[str]]:
        """Jamais les plus gros scores : conserver w0 si admissible, sinon réduction déterministe."""
        w0 = np.asarray(inputs.w0, dtype=float)
        if not np.all(np.isfinite(w0)):
            return np.zeros(len(w0)), STATUS_FALLBACK_REDUCE, [ReasonCode.DETERMINISTIC_REDUCTION.value]
        keep = verify_weights(
            inputs,
            w0,
            tol=SOLVER_VERIFY_TOL,
            margin_checker=self._margin_checker,
            include_change_limits=False,
        )
        if keep.ok:
            return w0, STATUS_FALLBACK_KEEP, [ReasonCode.PREVIOUS_ALLOCATION_KEPT.value]
        s, fully = scale_to_admissible(inputs, tol=SOLVER_VERIFY_TOL)
        reasons = [ReasonCode.DETERMINISTIC_REDUCTION.value]
        if not fully:
            reasons.append(ReasonCode.RISK_LIMIT.value)
        return s * w0, STATUS_FALLBACK_REDUCE, reasons


def _dedupe(codes: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def is_auto_approvable(target: PortfolioTarget) -> bool:
    """Seul un statut ``optimal`` vérifié ou un fallback sans violation résiduelle est approuvable seul."""
    if ReasonCode.RISK_LIMIT.value in target.reason_codes:
        return False
    return target.solver_status in (STATUS_OPTIMAL, STATUS_FALLBACK_KEEP, STATUS_FALLBACK_REDUCE)


def assert_target_fresh(target: PortfolioTarget, now: datetime) -> None:
    if ensure_utc(now) >= target.expires_at:
        raise SolverError(
            "target de portefeuille expiré : nouvelle optimisation requise",
            code=ReasonCode.TARGET_EXPIRED.value,
            target_id=target.target_id,
            expires_at=target.expires_at.isoformat(),
        )
