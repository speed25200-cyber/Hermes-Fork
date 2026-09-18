"""Optimiseur de portefeuille : T41 (secours du solveur) et T42 (risque transitoire) — §51.

POURQUOI ces cas comptent. Un solveur convexe échoue de façons ordinaires : timeout, infaisabilité,
erreur numérique, solution « optimal_inaccurate », NaN. La tentation, dans ces moments-là, est de
« faire quelque chose quand même » — prendre les plus gros scores, relâcher une tolérance, garder la
dernière solution sans la revérifier. Ce sont exactement les stratégies de secours NON validées que
T41 interdit : elles envoient au marché un portefeuille que rien n'a vérifié, au pire moment.

T42 traite l'autre bord du même problème : un panier neutre n'est neutre qu'une fois TOUTES ses jambes
exécutées. Entre la première et la dernière, l'exposition est directionnelle. Ce risque transitoire
doit être mesuré et plafonné, pas supposé instantané.

La ``solve_fn`` de ``CvxpyPortfolioBuilder`` est injectable précisément pour que ces échecs soient
testables sans provoquer un vrai timeout : les tests sont donc hermétiques et déterministes. Un test
utilise malgré tout le solveur réel, sinon rien ne garantirait que le chemin nominal fonctionne encore
et tous les autres tests passeraient sur un code qui tombe TOUJOURS en secours.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import SolverError
from okxq.domain.events import PortfolioInputs
from okxq.domain.reasons import ReasonCode
from okxq.portfolio.constraints import verify_weights
from okxq.portfolio.optimizer import (
    STATUS_FALLBACK_KEEP,
    STATUS_FALLBACK_REDUCE,
    STATUS_INACCURATE,
    STATUS_OPTIMAL,
    CvxpyPortfolioBuilder,
    SolveOutcome,
    SolverSettings,
    assert_target_fresh,
    cvxpy_solve,
    is_auto_approvable,
    partial_execution_report,
)

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INSTRUMENTS = ["AAA-USDT-SWAP", "BBB-USDT-SWAP"]


def inputs(
    *,
    w0: Sequence[float] = (0.0, 0.0),
    mu: Sequence[float] = (0.10, -0.10),
    gross_limit: float = 2.0,
    net_limit: float = 2.0,
    asset_limit: float = 1.0,
    turnover_limit: float = 4.0,
    liquidity: float = 1.0,
    margin_capacity: float = 1.0,
    margin_per_unit: float = 0.1,
) -> PortfolioInputs:
    n = len(INSTRUMENTS)
    return PortfolioInputs(
        instruments=list(INSTRUMENTS),
        mu=list(mu),
        sigma=[[0.04, 0.0], [0.0, 0.04]],
        w0=list(w0),
        cost_buy=[0.0005] * n,
        cost_sell=[0.0005] * n,
        uncertainty_penalty=[0.0] * n,
        expected_funding_cost=[0.0] * n,
        future_exit_cost=[0.0] * n,
        asset_limit=[asset_limit] * n,
        liquidity_capacity=[liquidity] * n,
        beta_btc=[1.0, 0.8],
        beta_eth=[0.5, 0.4],
        clusters={"alt": list(INSTRUMENTS)},
        horizon_s=3600,
        risk_aversion=1.0,
        gross_limit=gross_limit,
        net_limit=net_limit,
        btc_beta_limit=3.0,
        eth_beta_limit=3.0,
        cluster_limit=2.0,
        turnover_limit=turnover_limit,
        margin_capacity=margin_capacity,
        margin_requirement_per_unit=[margin_per_unit] * n,
        equity_version="eq-1",
        snapshot_id="snap-1",
        constraints_version="cons-1",
    )


def outcome(
    status: str,
    weights: Sequence[float] | None = None,
    *,
    objective: float | None = None,
    solve_ms: int = 7,
    error: str | None = None,
) -> SolveOutcome:
    return SolveOutcome(
        status=status,
        weights=None if weights is None else np.asarray(list(weights), dtype=float),
        objective=objective,
        solve_ms=solve_ms,
        error=error,
    )


def builder(fixed: SolveOutcome | None = None, **kwargs: object) -> CvxpyPortfolioBuilder:
    solve_fn = None if fixed is None else (lambda _inp, _st: fixed)
    return CvxpyPortfolioBuilder(
        clock=SimulatedClock(T0),
        target_ttl_s=60,
        solve_fn=solve_fn,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def as_floats(target_weights: dict[str, object]) -> list[float]:
    return [float(target_weights[inst]) for inst in INSTRUMENTS]  # type: ignore[arg-type]


# --- T41 : aucune stratégie de secours non validée ----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "candidate"),
    [
        ("solver_error", None),  # exception du solveur, timeout dur
        ("infeasible", None),  # problème infaisable
        ("unbounded", None),
        ("user_limit", None),  # limite de temps ou d'itérations atteinte
    ],
    ids=["error", "infeasible", "unbounded", "time_limit"],
)
def test_T41_a_failed_solve_keeps_the_previous_allocation_never_the_biggest_scores(
    status: str, candidate: Sequence[float] | None
) -> None:
    """T41 : quelle que soit la façon dont le solveur échoue, on ne CHOISIT pas.

    ``mu`` pousse fortement vers (+1, −1) : c'est exactement la « stratégie de secours » tentante et
    non validée (prendre les plus gros scores). Le secours admissible est de CONSERVER l'allocation
    antérieure, qui a déjà été vérifiée.
    """
    previous = (0.2, -0.2)
    target = builder(outcome(status, candidate)).optimize(
        inputs(w0=previous, mu=(10.0, -10.0), gross_limit=1.0)
    )

    assert target.solver_status == STATUS_FALLBACK_KEEP
    assert ReasonCode.SOLVER_FALLBACK.value in target.reason_codes
    assert ReasonCode.PREVIOUS_ALLOCATION_KEPT.value in target.reason_codes
    assert as_floats(target.signed_weights) == pytest.approx(list(previous))
    # Le secours n'est PAS le vecteur des plus gros scores.
    assert as_floats(target.signed_weights) != pytest.approx([1.0, -1.0])
    # Une allocation conservée reste vérifiée, donc approuvable seule.
    assert ReasonCode.RISK_LIMIT.value not in target.reason_codes
    assert is_auto_approvable(target)
    # Aucune valeur d'objectif n'est inventée pour un solveur qui n'a rien résolu.
    assert target.objective_value is None


def test_T41_an_inaccurate_solution_is_reported_and_never_adopted() -> None:
    """T41 : ``optimal_inaccurate`` est une solution que le solveur lui-même ne garantit pas.

    Les poids proposés ici sont plausibles et même admissibles : c'est ce qui rend le défaut
    dangereux. Les adopter reviendrait à approuver une solution dont les tolérances n'ont pas été
    tenues. Le test vérifie donc qu'ils sont IGNORÉS, pas seulement signalés.
    """
    plausible = (0.9, -0.9)
    target = builder(outcome(STATUS_INACCURATE, plausible, objective=1.23)).optimize(inputs(w0=(0.2, -0.2)))

    assert ReasonCode.SOLVER_INACCURATE.value in target.reason_codes
    assert ReasonCode.SOLVER_FALLBACK.value in target.reason_codes
    assert target.solver_status == STATUS_FALLBACK_KEEP
    assert as_floats(target.signed_weights) == pytest.approx([0.2, -0.2])
    assert as_floats(target.signed_weights) != pytest.approx(list(plausible))
    assert target.objective_value is None, "l'objectif d'une solution non retenue n'est pas rapporté"


def test_T41_non_finite_weights_never_reach_the_target() -> None:
    """T41 : un NaN dans les poids est une absence de résultat, pas un poids nul.

    Un seul NaN qui franchirait cette barrière contaminerait tout l'aval : contrats, notionnels,
    contrôles de risque. Le contrat ``PortfolioTarget`` n'accepte que des décimales finies.
    """
    target = builder(outcome(STATUS_OPTIMAL, (float("nan"), 0.1))).optimize(inputs(w0=(0.2, -0.2)))

    assert ReasonCode.SOLVER_FALLBACK.value in target.reason_codes
    assert target.solver_status == STATUS_FALLBACK_KEEP
    assert all(value.is_finite() for value in target.signed_weights.values())
    assert as_floats(target.signed_weights) == pytest.approx([0.2, -0.2])


def test_T41_a_solution_that_fails_independent_verification_is_not_auto_approvable() -> None:
    """T41 : la revérification est INDÉPENDANTE du solveur (NumPy, pas CVXPY).

    Un solveur qui rend « optimal » sur un problème mal construit reste un solveur qui a réussi. Seul
    un second calcul, fait autrement, peut le démentir — et un secours qui traîne une violation
    résiduelle ne doit PAS pouvoir partir sans validation humaine.
    """
    violating = (1.5, 0.0)  # brut 1,5 > gross_limit 1,0 et actif 1,5 > asset_limit 1,0
    target = builder(outcome(STATUS_OPTIMAL, violating, objective=99.0)).optimize(
        inputs(w0=(0.9, 0.9), gross_limit=1.0, turnover_limit=0.05, liquidity=0.02)
    )

    assert ReasonCode.SOLVER_FALLBACK.value in target.reason_codes
    assert ReasonCode.RISK_LIMIT.value in target.reason_codes
    assert as_floats(target.signed_weights) != pytest.approx(list(violating))
    assert not is_auto_approvable(target), "un secours avec violation résiduelle n'est jamais automatique"


def test_T41_an_inadmissible_previous_allocation_is_reduced_deterministically() -> None:
    """T41 : si l'allocation antérieure n'est PLUS admissible (limites resserrées, equity tombée), la
    conserver serait aussi faux que prendre les plus gros scores. Le secours est alors une réduction
    déterministe vers zéro : mêmes signes, magnitudes strictement plus petites, jamais une entrée.
    """
    previous = (0.8, -0.8)
    target = builder(outcome("infeasible")).optimize(
        inputs(w0=previous, gross_limit=1.0, turnover_limit=1.0, liquidity=1.0)
    )

    assert target.solver_status == STATUS_FALLBACK_REDUCE
    assert ReasonCode.DETERMINISTIC_REDUCTION.value in target.reason_codes
    weights = as_floats(target.signed_weights)
    assert np.sign(weights).tolist() == [1.0, -1.0], "la réduction ne retourne aucune position"
    assert all(abs(w) < 0.8 for w in weights), "une réduction diminue, elle n'augmente jamais"
    assert sum(abs(w) for w in weights) <= 1.0 + 1e-6
    # La réduction atteint l'admissibilité dès cette décision : aucun risque résiduel à signaler.
    assert ReasonCode.RISK_LIMIT.value not in target.reason_codes
    assert is_auto_approvable(target)


def test_T41_a_reduction_blocked_by_change_limits_is_reported_not_pretended() -> None:
    """Contre-épreuve du cas ci-dessus : quand turnover et liquidité empêchent d'atteindre
    l'admissibilité en UNE décision, la réduction est PARTIELLE. Elle doit alors être signalée
    (``RISK_LIMIT``) et perdre son caractère automatique — prétendre le contraire ferait croire le
    portefeuille rentré dans ses limites alors qu'il n'y est pas.
    """
    target = builder(outcome("infeasible")).optimize(
        inputs(w0=(0.8, -0.8), gross_limit=1.0, turnover_limit=0.05, liquidity=0.02)
    )

    assert target.solver_status == STATUS_FALLBACK_REDUCE
    assert ReasonCode.DETERMINISTIC_REDUCTION.value in target.reason_codes
    assert ReasonCode.RISK_LIMIT.value in target.reason_codes
    assert not is_auto_approvable(target)
    assert sum(abs(w) for w in as_floats(target.signed_weights)) > 1.0, "la réduction reste partielle"


def test_T41_a_verified_optimal_solution_is_adopted_as_is() -> None:
    """Contre-épreuve indispensable de tout ce fichier : une solution optimale et vérifiée est
    ADOPTÉE. Sans ce test, un optimiseur qui tomberait systématiquement en secours passerait tous les
    tests précédents.
    """
    good = (0.30, -0.25)
    target = builder(outcome(STATUS_OPTIMAL, good, objective=0.0421)).optimize(inputs(w0=(0.2, -0.2)))

    assert target.solver_status == STATUS_OPTIMAL
    assert as_floats(target.signed_weights) == pytest.approx(list(good))
    assert ReasonCode.SOLVER_FALLBACK.value not in target.reason_codes
    assert ReasonCode.RISK_LIMIT.value not in target.reason_codes
    assert target.objective_value == pytest.approx(0.0421)
    assert is_auto_approvable(target)
    assert target.solve_ms == 7


def test_T41_the_real_solver_returns_a_verified_admissible_target() -> None:
    """Le chemin NOMINAL (CVXPY + CLARABEL, aucune injection) doit produire un target admissible.

    Ce test existe pour que les tests d'injection ci-dessus ne puissent pas passer sur un optimiseur
    cassé qui ne résoudrait plus jamais rien.
    """
    problem_inputs = inputs(w0=(0.0, 0.0), mu=(0.20, -0.15), gross_limit=1.0, net_limit=0.5)
    solved = cvxpy_solve(problem_inputs, SolverSettings())
    assert solved.status == STATUS_OPTIMAL, solved.error
    assert solved.weights is not None

    target = CvxpyPortfolioBuilder(clock=SimulatedClock(T0), target_ttl_s=60).optimize(problem_inputs)
    assert target.solver_status == STATUS_OPTIMAL
    assert ReasonCode.SOLVER_FALLBACK.value not in target.reason_codes
    weights = as_floats(target.signed_weights)
    assert verify_weights(problem_inputs, weights, tol=1e-6).ok
    # Le signe suit le signal : long sur mu positif, short sur mu négatif.
    assert weights[0] > 0 > weights[1]


def test_T41_an_expired_target_is_refused_and_a_fresh_one_is_not() -> None:
    """Un target de portefeuille porte une expiration : au-delà, il décrit un marché qui n'existe
    plus. ``assert_target_fresh`` est le seul endroit qui le dit — et il doit le dire avec un code
    stable, sinon l'appelant ne peut pas distinguer « expiré » de « cassé ».
    """
    target = builder(outcome(STATUS_OPTIMAL, (0.1, -0.1))).optimize(inputs())

    assert_target_fresh(target, T0 + timedelta(seconds=59))  # frais : ne lève pas
    with pytest.raises(SolverError) as err:
        assert_target_fresh(target, T0 + timedelta(seconds=60))
    assert err.value.code == ReasonCode.TARGET_EXPIRED.value


# --- T42 : une seule jambe d'un panier s'exécute ------------------------------------------------------


def test_T42_a_single_leg_of_a_neutral_basket_is_reported_as_transient_risk() -> None:
    """T42 : le panier (+0,5 / −0,5) est neutre à l'arrivée et directionnel en chemin.

    Si seule la jambe AAA s'exécute, le net intermédiaire vaut 0,5 pour une limite nette de 0,05 —
    dix fois la limite, sur une position qui n'a jamais été approuvée sous cette forme. Le rapport
    doit nommer les jambes concernées et le target doit porter ``PARTIAL_EXECUTION_RISK``.
    """
    basket = inputs(net_limit=0.05, gross_limit=2.0)
    weights = np.array([0.5, -0.5])

    report = partial_execution_report(basket, weights, max_leg_offset=None)
    assert not report.ok
    assert sorted(report.legs_at_risk) == sorted(INSTRUMENTS)
    assert report.worst_abs_net == pytest.approx(0.5)
    assert report.worst_gross == pytest.approx(0.5)
    # Aucun plafond n'a été demandé : la mesure est ABSENTE, donc None — jamais 0.
    assert report.max_leg_offset is None

    target = builder(outcome(STATUS_OPTIMAL, (0.5, -0.5))).optimize(basket)
    assert ReasonCode.PARTIAL_EXECUTION_RISK.value in target.reason_codes
    assert target.residuals["partial_execution_worst_abs_net"] == pytest.approx(0.5)


def test_T42_a_basket_whose_single_leg_stays_inside_the_limits_is_not_flagged() -> None:
    """Contre-épreuve : avec une limite nette de 0,6, la même exécution partielle reste admissible.
    Un moteur qui signalerait tous les paniers rendrait le signal inutile — et on finirait par
    l'ignorer, ce qui est le vrai coût d'une alerte qui crie tout le temps.
    """
    basket = inputs(net_limit=0.6)
    report = partial_execution_report(basket, np.array([0.5, -0.5]), max_leg_offset=None)

    assert report.ok
    assert report.legs_at_risk == []

    target = builder(outcome(STATUS_OPTIMAL, (0.5, -0.5))).optimize(basket)
    assert ReasonCode.PARTIAL_EXECUTION_RISK.value not in target.reason_codes


def test_T42_legs_that_do_not_move_are_not_counted_as_transient_risk() -> None:
    """Une jambe dont δ = 0 ne s'exécute pas : elle ne peut créer aucune exposition intermédiaire.
    La compter serait un faux positif — et le pire cas rapporté serait alors le portefeuille tenu,
    pas une exécution partielle.
    """
    held = inputs(w0=(0.4, -0.4), net_limit=0.05)
    report = partial_execution_report(held, np.array([0.4, -0.4]), max_leg_offset=None)

    assert report.ok
    assert report.legs_at_risk == []
    # Le pire cas reste l'état tenu : 0,8 de brut, 0 de net. Rien n'est inventé au-delà.
    assert report.worst_gross == pytest.approx(0.8)
    assert report.worst_abs_net == pytest.approx(0.0)


def test_T42_max_leg_offset_caps_each_leg_and_blocks_a_solution_that_exceeds_it() -> None:
    """T42 : ``max_leg_offset`` PLAFONNE le déplacement d'une jambe par décision. Une solution qui le
    dépasse ne peut pas être adoptée, même « optimale » : c'est le plafond qui borne le risque
    transitoire, et le relâcher au cas par cas le supprimerait.
    """
    capped = builder(outcome(STATUS_OPTIMAL, (0.5, -0.5)), max_leg_offset=0.1)
    target = capped.optimize(inputs(w0=(0.0, 0.0), net_limit=2.0))

    assert ReasonCode.SOLVER_FALLBACK.value in target.reason_codes
    assert target.solver_status == STATUS_FALLBACK_KEEP
    assert as_floats(target.signed_weights) == pytest.approx([0.0, 0.0])

    # Contre-épreuve : un déplacement au plafond (0,1 par jambe) est adopté tel quel.
    within = builder(outcome(STATUS_OPTIMAL, (0.1, -0.1)), max_leg_offset=0.1)
    ok_target = within.optimize(inputs(w0=(0.0, 0.0), net_limit=0.2))
    assert ok_target.solver_status == STATUS_OPTIMAL
    assert as_floats(ok_target.signed_weights) == pytest.approx([0.1, -0.1])
    assert ReasonCode.PARTIAL_EXECUTION_RISK.value not in ok_target.reason_codes


def test_T42_the_real_problem_formulation_respects_max_leg_offset() -> None:
    """Le plafond doit vivre DANS le problème résolu, pas seulement dans le contrôle a posteriori :
    sinon chaque décision produirait une solution rejetée, et le portefeuille n'avancerait jamais.
    """
    problem_inputs = inputs(w0=(0.0, 0.0), mu=(1.0, -1.0), gross_limit=2.0, net_limit=2.0)
    solved = cvxpy_solve(problem_inputs, SolverSettings(), max_leg_offset=0.05)

    assert solved.status == STATUS_OPTIMAL, solved.error
    assert solved.weights is not None
    assert np.all(np.abs(solved.weights) <= 0.05 + 1e-6)
    # Sans plafond, le même signal pousse bien au-delà : le plafond est donc actif, pas décoratif.
    unbounded = cvxpy_solve(problem_inputs, SolverSettings())
    assert unbounded.weights is not None
    assert np.max(np.abs(unbounded.weights)) > 0.05


def test_T42_a_non_positive_leg_offset_is_refused_at_construction() -> None:
    """Un plafond nul ou négatif interdirait tout ordre tout en ayant l'air configuré. Mieux vaut
    refuser à la construction que découvrir en production que rien ne part plus.
    """
    with pytest.raises(SolverError):
        builder(outcome(STATUS_OPTIMAL, (0.1, -0.1)), max_leg_offset=0.0)
    with pytest.raises(SolverError):
        CvxpyPortfolioBuilder(clock=SimulatedClock(T0), target_ttl_s=0)
