"""Arrondi poids → contrats → intentions : T40 (§24, §45).

POURQUOI ces cas comptent : l'arrondi est la DERNIÈRE étape avant l'envoi. Un optimiseur peut rendre
un portefeuille parfaitement neutre et parfaitement dans la marge ; c'est l'arrondi qui décide des
contrats réellement envoyés. Si l'arrondi casse la neutralité (net) ou la marge sans être repris, on
envoie un risque que personne n'a approuvé — et l'approbation du Risk Engine porterait sur un
portefeuille qui n'existe pas.

Chaque refus est doublé de sa contre-épreuve : un moteur qui rejetterait TOUT passerait les tests de
rejet mais échouerait les tests « cas admissible ». Et un rejet est vérifié par l'ABSENCE d'effet :
aucune jambe, aucune intention, donc rien d'envoyable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import SolverError
from okxq.domain.events import PortfolioInputs, PortfolioTarget
from okxq.domain.instruments import InstrumentSpec, InstrumentState
from okxq.domain.money import Money, Side
from okxq.domain.orders import OrderKind
from okxq.domain.reasons import ReasonCode
from okxq.portfolio.rounding import (
    IntentPolicy,
    RoundingContext,
    round_target,
    round_target_contracts,
    weight_residual_norm,
)

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
EQUITY = Money(Decimal("100000"), "USDT")

# Deux instruments dont la grille de lot ne divise PAS le poids cible : c'est la condition même pour
# que l'arrondi déplace la neutralité. AAA : 1 lot = 0,01 d'equity ; BBB : 1 lot = 0,007.
PRICES: dict[str, Decimal] = {"AAA-USDT-SWAP": Decimal("1000"), "BBB-USDT-SWAP": Decimal("700")}


def spec(inst_id: str, *, lot: str = "1", minimum: str = "1") -> InstrumentSpec:
    return InstrumentSpec(
        inst_id=inst_id,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        observed_at=T0,
        settle_ccy="USDT",
        base_ccy=inst_id.split("-")[0],
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=Decimal("1"),
        tick_size=Decimal("0.1"),
        lot_size=Decimal(lot),
        min_size=Decimal(minimum),
        state=InstrumentState.LIVE,
        provenance="fixture:test_portfolio_rounding",
        max_leverage=Decimal("10"),
    )


def inputs(
    instruments: Sequence[str],
    *,
    w0: Sequence[float] | None = None,
    net_limit: float = 0.01,
    gross_limit: float = 2.0,
    asset_limit: float = 1.0,
    cluster_limit: float = 2.0,
    turnover_limit: float = 4.0,
    margin_capacity: float = 1.0,
    margin_per_unit: float = 0.1,
    liquidity: float = 1.0,
) -> PortfolioInputs:
    n = len(instruments)
    return PortfolioInputs(
        instruments=list(instruments),
        mu=[0.0] * n,
        sigma=[[0.0] * n for _ in range(n)],
        w0=list(w0) if w0 is not None else [0.0] * n,
        cost_buy=[0.0] * n,
        cost_sell=[0.0] * n,
        uncertainty_penalty=[0.0] * n,
        expected_funding_cost=[0.0] * n,
        future_exit_cost=[0.0] * n,
        asset_limit=[asset_limit] * n,
        liquidity_capacity=[liquidity] * n,
        beta_btc=[0.0] * n,
        beta_eth=[0.0] * n,
        clusters={"alt": list(instruments)},
        horizon_s=3600,
        risk_aversion=1.0,
        gross_limit=gross_limit,
        net_limit=net_limit,
        btc_beta_limit=1.0,
        eth_beta_limit=1.0,
        cluster_limit=cluster_limit,
        turnover_limit=turnover_limit,
        margin_capacity=margin_capacity,
        margin_requirement_per_unit=[margin_per_unit] * n,
        equity_version="eq-1",
        snapshot_id="snap-1",
        constraints_version="cons-1",
    )


def context(
    portfolio_inputs: PortfolioInputs,
    *,
    current: dict[str, Decimal] | None = None,
    specs: dict[str, InstrumentSpec] | None = None,
    margin_checker: object = None,
) -> RoundingContext:
    return RoundingContext(
        equity=EQUITY,
        specs=specs or {inst: spec(inst) for inst in portfolio_inputs.instruments},
        reference_prices={inst: PRICES[inst] for inst in portfolio_inputs.instruments},
        current_contracts=current or {},
        inputs=portfolio_inputs,
        margin_checker=margin_checker,  # type: ignore[arg-type]
    )


def policy() -> IntentPolicy:
    return IntentPolicy(account_scope="scope-test", decision_id="dec-1", ttl_ms=5_000)


def target(weights: dict[str, Decimal], *, ttl_s: int = 60) -> PortfolioTarget:
    return PortfolioTarget(
        target_id="tgt-1",
        snapshot_id="snap-1",
        equity_version="eq-1",
        signed_weights=dict(weights),
        constraints_version="cons-1",
        solver_status="optimal",
        created_at=T0,
        expires_at=T0 + timedelta(seconds=ttl_s),
    )


NEUTRAL = {"AAA-USDT-SWAP": Decimal("0.5"), "BBB-USDT-SWAP": Decimal("-0.5")}


# --- T40 : l'arrondi casse la neutralité -------------------------------------------------------------


def test_T40_rounding_that_breaks_neutrality_is_repaired_under_constraints() -> None:
    """T40 : la cible est neutre (+0,5 / −0,5), la grille de lot ne l'est pas.

    BBB ne peut pas atteindre −0,5 : 0,5 × 100 000 / 700 = 71,43 contrats, arrondis vers zéro à 71,
    soit −0,497. Le portefeuille arrondi porte donc un net résiduel de +0,003 alors que la limite
    nette vaut 0,001. C'est le défaut que T40 décrit : personne n'a approuvé ce +0,003 directionnel.
    Le candidat doit être CORRIGÉ sous contraintes, pas envoyé tel quel.
    """
    ctx = context(inputs(list(NEUTRAL), net_limit=0.001))
    plan = round_target(target(NEUTRAL), ctx, policy(), clock=SimulatedClock(T0))

    assert plan.status == "repaired"
    assert ReasonCode.ROUNDING_REPAIRED.value in plan.reason_codes
    # La correction est effective : le rapport final ne porte plus aucune violation.
    assert plan.report.ok
    assert plan.report.violations == []
    net = sum(plan.rounded_weights.values())
    assert abs(net) <= Decimal("0.001")
    # Correction DÉTERMINISTE et bornée : on rapproche des lots de la position actuelle, pas plus.
    assert 0 < plan.repair_steps <= 5
    assert plan.rounded_contracts == {"AAA-USDT-SWAP": Decimal("49"), "BBB-USDT-SWAP": Decimal("-70")}
    # Le plan corrigé reste actionnable : la correction ne supprime pas la décision, elle la borne.
    assert [leg.kind for leg in plan.legs] == ["increase", "increase"]
    assert len(plan.intents) == 2
    assert plan.accepted


def test_T40_an_admissible_rounded_candidate_passes_untouched() -> None:
    """Contre-épreuve du cas ci-dessus : avec une limite nette de 0,01, le résidu de 0,003 est
    admissible. Un moteur qui « corrigerait » ou rejetterait ici serait tout aussi faux : il
    détruirait de la décision valide à chaque arrondi. Sans ce test, le test de correction passerait
    aussi sur un moteur qui corrige toujours.
    """
    ctx = context(inputs(list(NEUTRAL), net_limit=0.01))
    plan = round_target(target(NEUTRAL), ctx, policy(), clock=SimulatedClock(T0))

    assert plan.status == "ok"
    assert plan.repair_steps == 0
    assert plan.reason_codes == []
    assert plan.rounded_contracts == {"AAA-USDT-SWAP": Decimal("50"), "BBB-USDT-SWAP": Decimal("-71")}
    assert plan.report.ok
    assert len(plan.intents) == 2
    # L'écart à la cible est un DIAGNOSTIC, pas un défaut : il vaut le résidu de grille, pas zéro.
    assert weight_residual_norm(plan, target(NEUTRAL)) == pytest.approx(0.003, abs=1e-9)


# --- T40 : l'arrondi casse la marge ------------------------------------------------------------------


def test_T40_rounding_that_breaks_the_exact_margin_is_repaired_under_constraints() -> None:
    """T40, variante marge : le vérificateur EXACT de marge (modèle de l'exchange) refuse le candidat
    arrondi. L'enveloppe linéaire de l'optimiseur peut être satisfaite alors que la marge réelle ne
    l'est pas — c'est précisément pour cela qu'un vérificateur exact est injectable.
    """

    def margin_checker(weights: Sequence[float]) -> float:
        # Utilisation = 1,05 × exposition brute : le candidat à 0,997 de brut dépasse la capacité.
        return 1.05 * sum(abs(w) for w in weights)

    ctx = context(inputs(list(NEUTRAL), net_limit=1.0), margin_checker=margin_checker)
    plan = round_target(target(NEUTRAL), ctx, policy(), clock=SimulatedClock(T0))

    assert plan.status == "repaired"
    assert ReasonCode.ROUNDING_REPAIRED.value in plan.reason_codes
    assert plan.report.ok
    assert plan.report.residuals["margin_exact"] <= 0
    # La correction rapproche des positions actuelles (ici zéro) : elle RÉDUIT, elle n'augmente jamais.
    assert abs(plan.rounded_contracts["AAA-USDT-SWAP"]) < Decimal("50")
    assert plan.intents, "un candidat corrigé reste envoyable"


def test_T40_an_irreparable_candidate_is_rejected_and_produces_nothing() -> None:
    """T40 : quand aucune correction sous contraintes ne converge, le candidat est REJETÉ.

    Ici le vérificateur de marge refuse tout, y compris le portefeuille plat. La correction
    déterministe ramène chaque jambe sur sa position actuelle, puis n'a plus rien à déplacer. Le seul
    comportement sûr est le rejet — et un rejet se vérifie par l'ABSENCE d'effet : aucune jambe,
    aucune intention, donc aucun ordre possible en aval.
    """
    ctx = context(inputs(list(NEUTRAL)), margin_checker=lambda _w: 2.0)
    plan = round_target(target(NEUTRAL), ctx, policy(), clock=SimulatedClock(T0))

    assert plan.status == "rejected"
    assert not plan.accepted
    assert plan.reason_codes[0] == ReasonCode.ROUNDING_REJECTED.value
    assert "margin_exact" in plan.reason_codes, "le rejet nomme la contrainte fautive"
    # Absence d'effet : rien d'exécutable ne sort d'un rejet.
    assert plan.legs == []
    assert plan.intents == []
    assert plan.sequencing == {}


def test_T40_a_non_finite_margin_verifier_rejects_rather_than_guesses() -> None:
    """Une marge non finie est une MESURE MANQUANTE, pas une marge confortable. Traiter NaN comme
    « pas de violation » serait la façon la plus discrète d'envoyer un portefeuille non couvert.
    """
    ctx = context(inputs(list(NEUTRAL)), margin_checker=lambda _w: float("nan"))
    plan = round_target(target(NEUTRAL), ctx, policy(), clock=SimulatedClock(T0))

    assert plan.status == "rejected"
    assert not plan.report.finite
    assert plan.intents == []


def test_T40_an_admissible_exact_margin_lets_the_candidate_through() -> None:
    """Contre-épreuve des deux rejets ci-dessus : un vérificateur de marge satisfait ne bloque rien."""
    ctx = context(inputs(list(NEUTRAL), net_limit=0.01), margin_checker=lambda _w: 0.5)
    plan = round_target(target(NEUTRAL), ctx, policy(), clock=SimulatedClock(T0))

    assert plan.status == "ok"
    assert plan.report.residuals["margin_exact"] == pytest.approx(-0.5)
    assert len(plan.intents) == 2


# --- T40 : grille de lot, minimum et dust -------------------------------------------------------------


def test_T40_an_increase_below_the_minimum_becomes_no_order_never_the_minimum() -> None:
    """Une augmentation sous ``min_size`` devient ZÉRO. La porter à ``min_size`` serait augmenter un
    risque que la cible ne demandait pas — le sens exactement inverse d'un arrondi prudent.
    """
    s = spec("AAA-USDT-SWAP", lot="1", minimum="5")
    # Cible 0,03 = 3 contrats : au-dessus du lot, sous le minimum.
    value, dust = round_target_contracts(Decimal("0.03"), Decimal("0"), s, PRICES["AAA-USDT-SWAP"], EQUITY)
    assert value == Decimal("0")
    assert dust is None

    # Contre-épreuve : à 0,05 (5 contrats) l'ordre est exactement au minimum et passe.
    value_ok, _ = round_target_contracts(Decimal("0.05"), Decimal("0"), s, PRICES["AAA-USDT-SWAP"], EQUITY)
    assert value_ok == Decimal("5")


def test_T40_a_reduction_below_the_minimum_is_raised_but_never_flips_the_position() -> None:
    """Une réduction sous ``min_size`` est portée à ``min_size`` — on réduit un peu PLUS, jamais au-delà
    de la position. Ouvrir 1 contrat de l'autre côté pour « nettoyer » un résidu serait une prise de
    risque inversée déguisée en hygiène.
    """
    s = spec("AAA-USDT-SWAP", lot="1", minimum="5")
    # Position +5, cible +0,04 (4 contrats) : la réduction demandée vaut 1 contrat, sous le minimum.
    value, dust = round_target_contracts(Decimal("0.04"), Decimal("5"), s, PRICES["AAA-USDT-SWAP"], EQUITY)
    assert value == Decimal("0"), "la réduction est portée au minimum, donc elle solde la position"
    assert value >= Decimal("0"), "aucun retournement : le signe n'est jamais inversé"
    assert dust is None


def test_T40_a_position_below_the_minimum_is_reported_as_dust_not_traded() -> None:
    """Un résidu non réductible est du DUST : il est LAISSÉ en place et RAPPORTÉ. L'alternative
    (ouvrir une position opposée plus grosse) échangerait un résidu mesuré contre un risque inventé.
    """
    inst = "AAA-USDT-SWAP"
    specs = {inst: spec(inst, lot="1", minimum="5")}
    # ``w0`` reflète la position tenue (3 contrats = 0,03 d'equity) : sinon on testerait une limite
    # nette violée par l'état INITIAL, pas le traitement du dust.
    ctx = context(inputs([inst], w0=[0.03], net_limit=0.05), current={inst: Decimal("3")}, specs=specs)
    plan = round_target(target({inst: Decimal("0")}), ctx, policy(), clock=SimulatedClock(T0))

    assert plan.status == "ok"
    assert plan.dust == {inst: Decimal("3")}
    assert ReasonCode.DUST_RESIDUAL.value in plan.reason_codes
    assert plan.legs == [], "le dust ne produit aucune jambe"
    assert plan.intents == []

    # Contre-épreuve : une position réductible (10 ≥ minimum) produit bien une réduction reduce_only.
    ctx_ok = context(inputs([inst], w0=[0.1], net_limit=0.15), current={inst: Decimal("10")}, specs=specs)
    plan_ok = round_target(target({inst: Decimal("0")}), ctx_ok, policy(), clock=SimulatedClock(T0))
    assert plan_ok.dust == {}
    assert [(leg.kind, leg.side, leg.contracts) for leg in plan_ok.legs] == [
        ("reduce", Side.SELL, Decimal("10"))
    ]
    assert plan_ok.intents[0].reduce_only is True
    assert plan_ok.intents[0].order_type is OrderKind.IOC


def test_T40_a_long_to_short_flip_is_split_into_a_close_then_a_dependent_open() -> None:
    """T39/T40 : un passage long → short n'est pas un seul ordre. La clôture est reduce_only et
    l'ouverture DÉPEND d'elle : sans ce séquencement, les deux jambes peuvent s'exécuter ensemble et
    doubler transitoirement l'exposition.
    """
    inst = "AAA-USDT-SWAP"
    ctx = context(inputs([inst], w0=[0.2], net_limit=1.0), current={inst: Decimal("20")})
    plan = round_target(target({inst: Decimal("-0.1")}), ctx, policy(), clock=SimulatedClock(T0))

    kinds = [leg.kind for leg in plan.legs]
    assert kinds == ["close", "open"], "la clôture précède l'ouverture"
    close_leg, open_leg = plan.legs
    assert (close_leg.side, close_leg.contracts, close_leg.reduce_only) == (Side.SELL, Decimal("20"), True)
    assert (open_leg.side, open_leg.contracts, open_leg.reduce_only) == (Side.SELL, Decimal("10"), False)
    close_intent, open_intent = plan.intents
    assert plan.sequencing == {open_intent.intent_id: [close_intent.intent_id]}


# --- T40 : fraîcheur ---------------------------------------------------------------------------------


def test_T40_an_expired_target_is_rejected_before_any_rounding() -> None:
    """Un target expiré est refusé AVANT tout calcul : arrondir sur une equity et des prix périmés
    produirait des contrats justes pour un portefeuille qui n'existe plus.
    """
    ctx = context(inputs(list(NEUTRAL)))
    expired = target(NEUTRAL, ttl_s=10)
    late = SimulatedClock(T0 + timedelta(seconds=10))

    with pytest.raises(SolverError) as err:
        round_target(expired, ctx, policy(), clock=late)
    assert err.value.code == ReasonCode.TARGET_EXPIRED.value

    # Contre-épreuve : une seconde avant l'expiration, le même target est traité normalement.
    plan = round_target(expired, ctx, policy(), clock=SimulatedClock(T0 + timedelta(seconds=9)))
    assert plan.accepted
