"""Risk Engine indépendant : limites, halts et absence d'effet d'un refus (§25, §53).

POURQUOI ces cas comptent. Le Risk Engine est le dernier rempart entre un modèle et de l'argent réel.
Deux façons de le rendre décoratif :

1. **Refuser sans rien empêcher.** Un refus qui laisse une réservation d'exposition active, ou qui
   consomme du budget, n'est pas un refus : il dégrade la décision SUIVANTE. Tous les tests de refus
   ici vérifient donc l'ABSENCE d'effet, pas seulement le code de retour.
2. **Refuser tout.** Un moteur qui rejette systématiquement passerait n'importe quel test de blocage.
   Chaque blocage est donc doublé de sa contre-épreuve : le cas admissible doit passer, et une
   RÉDUCTION doit rester possible là où seule une augmentation est interdite.

Le kill switch n'a qu'une direction : il monte. Un niveau qui redescendrait tout seul rendrait
inutile le fait de l'avoir monté.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from okxq.config.schema import RiskCfg
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import HaltError
from okxq.domain.events import OrderIntent, RiskAction, RiskEvent, Severity
from okxq.domain.instruments import InstrumentSpec, InstrumentState
from okxq.domain.money import Money, Side
from okxq.domain.orders import OrderKind, OrderState
from okxq.domain.reasons import ReasonCode
from okxq.risk.approvals import InMemoryReservationStore
from okxq.risk.budgets import LimitSet, OpenOrderState, PositionState
from okxq.risk.engine import MarketQuality, RiskContext, RiskEngine, reduced_intent
from okxq.risk.kill_switch import (
    HaltLevel,
    HealthSignals,
    InMemoryRiskStateStore,
    KillSwitch,
    OperatorRequest,
    max_level,
)

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SCOPE = "scope-test"
INST = "AAA-USDT-SWAP"
EQUITY = Money(Decimal("100000"), "USDT")
PRICE = Decimal("1000")  # 1 contrat = 1 unité de base = 1 000 USDT = 0,01 d'equity


def spec(state: InstrumentState = InstrumentState.LIVE) -> InstrumentSpec:
    return InstrumentSpec(
        inst_id=INST,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        observed_at=T0,
        settle_ccy="USDT",
        base_ccy="AAA",
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=Decimal("1"),
        tick_size=Decimal("0.1"),
        lot_size=Decimal("1"),
        min_size=Decimal("1"),
        state=state,
        provenance="fixture:test_risk_engine",
        max_leverage=Decimal("10"),
    )


def limits(**overrides: object) -> LimitSet:
    values: dict[str, object] = {
        "max_gross_equity_multiple": Decimal("2"),
        "max_abs_net_equity_multiple": Decimal("1"),
        "max_asset_equity_multiple": Decimal("0.5"),
        "max_cluster_gross_equity_multiple": Decimal("1"),
        "max_directional_equity_multiple": Decimal("1.5"),
        "max_abs_btc_beta_exposure": Decimal("1"),
        "max_abs_eth_beta_exposure": Decimal("1"),
        "max_margin_utilization": Decimal("0.5"),
        "margin_buffer_fraction": Decimal("0.2"),
        "liquidation_distance_min_fraction": Decimal("0.15"),
        "daily_loss_halt_fraction": Decimal("0.05"),
        "drawdown_review_fraction": Decimal("0.1"),
        "max_order_equity_multiple": Decimal("0.5"),
        "max_order_participation_fraction": Decimal("0.1"),
        "max_depth_consumption_fraction": Decimal("0.2"),
        "max_turnover_equity_fraction_per_decision": Decimal("1"),
        "max_orders_per_minute": 30,
        "approval_ttl_ms": 2_000,
        "max_order_intent_age_ms": 3_000,
        "max_quote_age_ms": 2_000,
        "max_private_state_age_ms": 5_000,
        "max_clock_offset_ms": 250,
        "max_relative_spread": Decimal("0.001"),
        "resume_stability_seconds": 120,
        "require_reconciliation_before_entries": True,
    }
    values.update(overrides)
    return LimitSet(**values)  # type: ignore[arg-type]


def quality(**overrides: object) -> MarketQuality:
    values: dict[str, object] = {
        "quote_age_ms": 200,
        "book_valid": True,
        "relative_spread": Decimal("0.0002"),
        "volatility_ratio": Decimal("1"),
        "max_contracts_by_participation": None,
    }
    values.update(overrides)
    return MarketQuality(**values)  # type: ignore[arg-type]


HELD_SHORT = {
    INST: PositionState(
        inst_id=INST,
        signed_contracts=Decimal("-20"),
        mark_price=PRICE,
        liquidation_price=Decimal("700"),
        leverage=Decimal("4"),
    )
}


def context(**overrides: Any) -> RiskContext:
    """Contexte SAIN par défaut : chaque test ne dégrade qu'une seule chose à la fois."""
    values: dict[str, Any] = {
        "now": T0,
        "account_scope": SCOPE,
        "equity": EQUITY,
        "equity_version": "eq-1",
        "position_version": "pos-1",
        "positions": dict(HELD_SHORT),
        "open_orders": [],
        "specs": {INST: spec()},
        "reference_prices": {INST: PRICE},
        "market_quality": {INST: quality()},
        "equity_reconciled": True,
        "private_state_age_ms": 500,
        "reconciliation_ok": True,
        "reconciliation_age_ms": 1_000,
        "is_leader": True,
        "orders_last_minute": 0,
        "halt_level": HaltLevel.NONE,
    }
    values.update(overrides)
    return RiskContext(**values)


def intent(
    *,
    side: Side = Side.SELL,
    contracts: str = "10",
    reduce_only: bool = False,
    created_at: datetime = T0,
    ttl_ms: int = 2_000,
) -> OrderIntent:
    return OrderIntent(
        intent_id=f"int-{side.value}-{contracts}-{reduce_only}",
        decision_id="dec-1",
        target_id="tgt-1",
        account_scope=SCOPE,
        inst_id=INST,
        side=side,
        contracts=Decimal(contracts),
        price_limit=PRICE,
        order_type=OrderKind.POST_ONLY,
        reduce_only=reduce_only,
        ttl_ms=ttl_ms,
        reason="test",
        client_order_id=f"cid-{side.value}-{contracts}-{reduce_only}",
        created_at=created_at,
        expires_at=created_at + timedelta(milliseconds=ttl_ms),
    )


def increase() -> OrderIntent:
    """Augmentation d'exposition : vendre 10 contrats de plus sur un short déjà tenu."""
    return intent(side=Side.SELL, contracts="10", reduce_only=False)


def reduction() -> OrderIntent:
    """Réduction : racheter 10 contrats du short tenu, en reduce_only."""
    return intent(side=Side.BUY, contracts="10", reduce_only=True)


def engine(
    limit_set: LimitSet | None = None, *, sink: list[RiskEvent] | None = None
) -> tuple[RiskEngine, InMemoryReservationStore]:
    store = InMemoryReservationStore()
    return (
        RiskEngine(
            limits=limit_set or limits(),
            clock=SimulatedClock(T0),
            reservations=store,
            event_sink=None if sink is None else sink.append,
        ),
        store,
    )


# --- décision nominale : la contre-épreuve de tout le fichier -----------------------------------------


def test_an_admissible_increase_is_allowed_and_reserves_exactly_what_it_allows() -> None:
    """Sans cette contre-épreuve, tous les tests de refus ci-dessous passeraient aussi sur un moteur
    qui refuse TOUT. On vérifie en plus que la décision est liée au hash EXACT du payload normalisé,
    à la version des limites et à celle des positions : c'est ce lien qui rend une approbation
    non réutilisable dans un autre contexte.
    """
    lim = limits()
    eng, store = engine(lim)
    order_intent = increase()

    decision = eng.evaluate_sync(order_intent, context())

    assert decision.action is RiskAction.ALLOW
    assert decision.reason_codes == [ReasonCode.OK.value]
    assert decision.intent_hash == order_intent.payload_hash()
    assert decision.allowed_payload_hash == order_intent.payload_hash()
    assert decision.allowed_contracts == Decimal("10")
    assert decision.limits_version == lim.limits_version
    assert decision.position_version == "pos-1"
    assert decision.expires_at == T0 + timedelta(milliseconds=lim.approval_ttl_ms)

    # La réservation est PESSIMISTE, signée, et porte exactement la taille autorisée.
    active = store.active(SCOPE)
    assert len(active) == 1
    assert active[0].signed_contracts == Decimal("-10")
    assert active[0].notional_usdt == Decimal("10000")
    assert active[0].pessimistic
    assert decision.reservations == {INST: active[0].reservation_id}


def test_a_refusal_leaves_no_reservation_and_does_not_penalize_the_next_decision() -> None:
    """Un refus doit être SANS EFFET. S'il laissait une réservation active, il consommerait du budget
    pour un ordre qui n'existe pas : la décision suivante, elle, serait réduite à tort. C'est une
    perte d'argent silencieuse causée par un refus « réussi ».
    """
    eng, store = engine(limits(max_asset_equity_multiple=Decimal("0.005")))

    refused = eng.evaluate_sync(increase(), context())
    assert refused.action is RiskAction.REJECT
    assert ReasonCode.RISK_LIMIT.value in refused.reason_codes
    assert refused.allowed_payload_hash is None
    assert refused.allowed_contracts is None
    assert refused.reservations == {}
    assert store.active(SCOPE) == [], "un refus ne réserve rien"

    # Et le budget est intact : le même compte, avec des limites normales, autorise la taille pleine.
    eng_ok, store_ok = engine()
    allowed = eng_ok.evaluate_sync(increase(), context())
    assert allowed.action is RiskAction.ALLOW
    assert allowed.allowed_contracts == Decimal("10")
    assert len(store_ok.active(SCOPE)) == 1


def test_an_oversized_increase_is_reduced_to_the_largest_admissible_lot_multiple() -> None:
    """Une augmentation trop grande est RÉDUITE, pas refusée : refuser ferait perdre la totalité
    d'une opportunité pour un dépassement partiel. La taille retenue est un multiple du lot, et le
    hash autorisé est celui de l'intention RÉDUITE — jamais celui de l'intention demandée.
    """
    eng, store = engine(limits(max_order_equity_multiple=Decimal("0.05")))  # 5 contrats au plus
    order_intent = increase()

    decision = eng.evaluate_sync(order_intent, context())

    assert decision.action is RiskAction.REDUCE
    assert decision.allowed_contracts == Decimal("5")
    assert ReasonCode.ORDER_TOO_LARGE.value in decision.reason_codes
    assert decision.allowed_payload_hash != order_intent.payload_hash()
    assert reduced_intent(order_intent, decision).contracts == Decimal("5")
    # La réservation suit la taille AUTORISÉE, pas la taille demandée.
    assert store.active(SCOPE)[0].notional_usdt == Decimal("5000")


def test_a_participation_cap_reduces_the_order_and_an_unmeasured_volume_caps_nothing() -> None:
    """Le plafond de participation est calculé sur un volume OBSERVÉ. Quand ce volume n'a pas été
    mesuré, la valeur est ``None`` : on ne fabrique pas un plafond, et on n'en invente pas non plus
    l'absence — les deux comportements sont vérifiés ici.
    """
    eng, _ = engine()
    capped = eng.evaluate_sync(
        increase(), context(market_quality={INST: quality(max_contracts_by_participation=Decimal("3"))})
    )
    assert capped.action is RiskAction.REDUCE
    assert capped.allowed_contracts == Decimal("3")
    assert ReasonCode.PARTICIPATION_LIMIT.value in capped.reason_codes

    eng_free, _ = engine()
    uncapped = eng_free.evaluate_sync(
        increase(), context(market_quality={INST: quality(max_contracts_by_participation=None)})
    )
    assert uncapped.action is RiskAction.ALLOW
    assert uncapped.allowed_contracts == Decimal("10")


def test_margin_reduces_then_refuses_when_no_room_remains_but_never_blocks_a_reduction() -> None:
    """La marge est un plafond dur, mais seulement sur les AUGMENTATIONS : refuser une réduction
    faute de marge serait le pire enchaînement possible — on empêcherait de sortir précisément quand
    la marge manque.
    """
    # Capacité utilisable = 0,5 × (1 − 0,2) × 100 000 = 40 000. Marge par contrat = 1 000 / 4 = 250.
    eng, _ = engine()
    tight = eng.evaluate_sync(increase(), context(used_margin=Decimal("39500")))
    assert tight.action is RiskAction.REDUCE
    assert tight.allowed_contracts == Decimal("2")  # 500 de place / 250 par contrat
    assert ReasonCode.MARGIN_INSUFFICIENT.value in tight.reason_codes

    eng_full, store_full = engine()
    saturated = eng_full.evaluate_sync(increase(), context(used_margin=Decimal("40000")))
    assert saturated.action is RiskAction.REJECT
    assert ReasonCode.MARGIN_INSUFFICIENT.value in saturated.reason_codes
    assert store_full.active(SCOPE) == []

    # Contre-épreuve : la réduction passe dans le MÊME contexte saturé.
    eng_exit, _ = engine()
    exit_decision = eng_exit.evaluate_sync(reduction(), context(used_margin=Decimal("40000")))
    assert exit_decision.action is RiskAction.ALLOW


def test_pessimistic_exposure_includes_pending_and_unknown_orders_before_deciding() -> None:
    """T43 vu du moteur : les ordres en attente, ``UNKNOWN`` compris, consomment le budget AVANT que
    l'intention ne soit évaluée. Les ignorer autoriserait une exposition cumulée que personne n'a
    validée — c'est le scénario classique de la position doublée après un ACK perdu.
    """
    pending = OpenOrderState(
        client_order_id="cid-unknown",
        inst_id=INST,
        side=Side.SELL,
        remaining_contracts=Decimal("38"),
        observed_state=OrderState.UNKNOWN,
    )
    eng, _ = engine()  # limite d'actif 0,5 → 50 contrats ; 20 tenus + 38 en attente = 58
    decision = eng.evaluate_sync(increase(), context(open_orders=[pending]))

    assert decision.action is RiskAction.REJECT
    assert ReasonCode.RISK_LIMIT.value in decision.reason_codes

    # Contre-épreuve : le même ordre à un état FINAL observé ne consomme plus rien.
    done = OpenOrderState(
        client_order_id="cid-canceled",
        inst_id=INST,
        side=Side.SELL,
        remaining_contracts=Decimal("38"),
        observed_state=OrderState.CANCELED,
    )
    eng_ok, _ = engine()
    assert eng_ok.evaluate_sync(increase(), context(open_orders=[done])).action is RiskAction.ALLOW


def test_a_cluster_or_beta_limit_reduces_the_order_just_like_the_asset_limit() -> None:
    """Les limites de cluster et de bêta doivent mordre SÉPARÉMENT. Un moteur qui ne vérifierait que
    la limite par actif laisserait passer une concentration sectorielle ou un facteur BTC entier.
    """
    # Cluster serré : 20 contrats tenus = 0,2 ; la limite de cluster à 0,25 ne laisse que 5 contrats.
    eng_cluster, _ = engine(limits(max_cluster_gross_equity_multiple=Decimal("0.25")))
    clustered = eng_cluster.evaluate_sync(increase(), context(clusters={"alt": [INST]}))
    assert clustered.action is RiskAction.REDUCE
    assert clustered.allowed_contracts == Decimal("5")

    # Bêta BTC de 2 : l'exposition bêta d'un short de 0,2 vaut 0,4 ; la limite 0,5 laisse 5 contrats.
    eng_beta, _ = engine(limits(max_abs_btc_beta_exposure=Decimal("0.5")))
    beta_capped = eng_beta.evaluate_sync(increase(), context(betas_btc={INST: Decimal("2")}))
    assert beta_capped.action is RiskAction.REDUCE
    assert beta_capped.allowed_contracts == Decimal("5")

    # Contre-épreuve : sans cluster ni bêta déclarés, aucune de ces limites n'est inventée.
    eng_plain, _ = engine(
        limits(max_cluster_gross_equity_multiple=Decimal("0.25"), max_abs_btc_beta_exposure=Decimal("0.5"))
    )
    assert eng_plain.evaluate_sync(increase(), context()).action is RiskAction.ALLOW


# --- blocages qui n'empêchent QUE les augmentations ---------------------------------------------------

ENTRY_ONLY_CASES: list[tuple[str, dict[str, Any], ReasonCode]] = [
    ("cotation_perimee", {"market_quality": {INST: quality(quote_age_ms=10_000)}}, ReasonCode.DATA_STALE),
    ("cotation_non_mesuree", {"market_quality": {INST: quality(quote_age_ms=None)}}, ReasonCode.DATA_STALE),
    ("etat_prive_non_mesure", {"private_state_age_ms": None}, ReasonCode.DATA_STALE),
    ("carnet_invalide", {"market_quality": {INST: quality(book_valid=False)}}, ReasonCode.BOOK_INVALID),
    ("flux_public_coupe", {"public_stream_connected": False}, ReasonCode.CONNECTION_LOST),
    ("flux_prive_coupe", {"private_stream_connected": False}, ReasonCode.CONNECTION_LOST),
    (
        "spread_trop_large",
        {"market_quality": {INST: quality(relative_spread=Decimal("0.01"))}},
        ReasonCode.SPREAD_TOO_WIDE,
    ),
    (
        "spread_non_mesure",
        {"market_quality": {INST: quality(relative_spread=None)}},
        ReasonCode.SPREAD_TOO_WIDE,
    ),
    (
        "volatilite_anormale",
        {"market_quality": {INST: quality(volatility_ratio=Decimal("5"))}},
        ReasonCode.VOLATILITY_ABNORMAL,
    ),
    ("modele_malade", {"model_healthy": False}, ReasonCode.MODEL_UNHEALTHY),
    ("reconciliation_ko", {"reconciliation_ok": False}, ReasonCode.RECONCILIATION_PENDING),
    ("reconciliation_non_datee", {"reconciliation_age_ms": None}, ReasonCode.RECONCILIATION_PENDING),
    ("equity_non_reconciliee", {"equity_reconciled": False}, ReasonCode.EQUITY_NOT_RECONCILED),
    (
        "sortie_univers",
        {"reduce_only_instruments": frozenset({INST})},
        ReasonCode.UNIVERSE_EXIT_REDUCE_ONLY,
    ),
    ("perte_journaliere", {"daily_loss_fraction": Decimal("0.2")}, ReasonCode.DAILY_LOSS_LIMIT),
    ("drawdown", {"drawdown_fraction": Decimal("0.5")}, ReasonCode.DRAWDOWN_REVIEW),
    (
        "distance_de_liquidation",
        {"positions": {INST: PositionState(inst_id=INST, signed_contracts=Decimal("-20"))}},
        ReasonCode.LIQUIDATION_DISTANCE,
    ),
]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [(o, e) for _, o, e in ENTRY_ONLY_CASES],
    ids=[name for name, _, _ in ENTRY_ONLY_CASES],
)
def test_a_degraded_context_blocks_the_increase_and_still_permits_the_reduction(
    overrides: Mapping[str, Any], expected: ReasonCode
) -> None:
    """Chaque dégradation observée doit interdire d'AUGMENTER l'exposition et laisser SORTIR.

    C'est la règle qui distingue un frein d'un blocage : quand la donnée est périmée, le carnet
    invalide ou la marge tendue, refuser aussi les réductions enfermerait le compte dans son risque
    au moment où l'on veut précisément le réduire. Les deux moitiés sont testées ensemble, sinon on
    ne saurait pas laquelle est vérifiée.
    """
    eng, store = engine()
    blocked = eng.evaluate_sync(increase(), context(**overrides))
    assert blocked.action is RiskAction.REJECT
    assert expected.value in blocked.reason_codes
    assert store.active(SCOPE) == [], "un refus ne laisse aucune réservation"

    eng_exit, store_exit = engine()
    allowed = eng_exit.evaluate_sync(reduction(), context(**overrides))
    assert allowed.action is RiskAction.ALLOW, f"{expected.value} ne doit pas bloquer une réduction"
    assert len(store_exit.active(SCOPE)) == 1


# --- blocages DURS : ils empêchent aussi les réductions -----------------------------------------------

HARD_CASES: list[tuple[str, dict[str, Any], ReasonCode]] = [
    ("non_leader", {"is_leader": False}, ReasonCode.NOT_LEADER),
    ("derive_horloge", {"clock_offset_ms": 5_000}, ReasonCode.CLOCK_DRIFT),
    ("horloge_non_fiable", {"clock_reliable": False}, ReasonCode.CLOCK_DRIFT),
    ("cadence_saturee", {"orders_last_minute": 30}, ReasonCode.ORDER_RATE_LIMIT),
    ("instrument_sans_metadonnees", {"specs": {}}, ReasonCode.INSTRUMENT_NOT_TRADABLE),
    (
        "instrument_suspendu",
        {"specs": {INST: spec(InstrumentState.SUSPEND)}},
        ReasonCode.INSTRUMENT_NOT_TRADABLE,
    ),
    ("instrument_sans_prix", {"reference_prices": {}}, ReasonCode.INSTRUMENT_NOT_TRADABLE),
]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [(o, e) for _, o, e in HARD_CASES],
    ids=[name for name, _, _ in HARD_CASES],
)
def test_a_hard_control_refuses_even_a_reduction_and_reserves_nothing(
    overrides: Mapping[str, Any], expected: ReasonCode
) -> None:
    """Certains contrôles ne peuvent pas être contournés par une réduction : sans leadership, sans
    horloge fiable, sans métadonnées d'instrument ou au-delà de la cadence autorisée, on ne sait même
    pas ce qu'on enverrait. Envoyer « juste une réduction » dans cet état, c'est envoyer à l'aveugle.
    """
    for order_intent in (increase(), reduction()):
        eng, store = engine()
        decision = eng.evaluate_sync(order_intent, context(**overrides))
        assert decision.action is RiskAction.REJECT
        assert expected.value in decision.reason_codes
        assert decision.allowed_payload_hash is None
        assert store.active(SCOPE) == []


def test_an_expired_intent_is_refused_even_for_a_reduction() -> None:
    """Une intention expirée décrit un marché qui n'existe plus : ses prix et sa taille ont été
    calculés ailleurs. L'envoyer serait exécuter une décision périmée — le TTL n'existe que pour ça.
    """
    late = context(now=T0 + timedelta(seconds=10))
    for order_intent in (increase(), reduction()):
        eng, store = engine()
        decision = eng.evaluate_sync(order_intent, late)
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.INTENT_EXPIRED.value in decision.reason_codes
        assert store.active(SCOPE) == []

    # Contre-épreuve : la même intention, dans son TTL, est acceptée.
    eng_ok, _ = engine()
    assert eng_ok.evaluate_sync(increase(), context(now=T0 + timedelta(seconds=1))).action is RiskAction.ALLOW


def test_a_reduce_only_order_without_an_opposite_position_is_refused() -> None:
    """Un ``reduce_only`` sans position opposée n'est pas une réduction : c'est une ouverture qui
    emprunte le régime de faveur des réductions. L'accepter contournerait tous les contrôles
    d'entrée d'un seul drapeau.
    """
    eng, store = engine()
    decision = eng.evaluate_sync(reduction(), context(positions={}))

    assert decision.action is RiskAction.REJECT
    assert ReasonCode.RISK_LIMIT.value in decision.reason_codes
    assert store.active(SCOPE) == []


def test_the_engine_emits_an_auditable_event_for_every_non_informational_finding() -> None:
    """Un refus non journalisé est un refus invérifiable : personne ne pourra dire, après coup,
    pourquoi l'ordre n'est pas parti.
    """
    events: list[RiskEvent] = []
    eng, _ = engine(sink=events)
    eng.evaluate_sync(increase(), context(model_healthy=False, model_health_reason="calibration"))

    codes = {e.reason_code for e in events}
    assert ReasonCode.MODEL_UNHEALTHY.value in codes
    emitted = next(e for e in events if e.reason_code == ReasonCode.MODEL_UNHEALTHY.value)
    assert emitted.requested_action == "REJECT"
    assert emitted.affected_scope == f"{SCOPE}:{INST}"
    assert emitted.evidence["detail"] == "calibration"
    assert emitted.severity in (Severity.WARN, Severity.CRITICAL)


# --- halts : les réductions restent permises, l'urgence demande un flatten ----------------------------


@pytest.mark.parametrize("level", [HaltLevel.SOFT_HALT, HaltLevel.HARD_HALT])
def test_a_halt_blocks_increases_and_explicitly_authorizes_reductions(level: HaltLevel) -> None:
    """Un halt suspend les PRISES de risque, pas les sorties. Le code
    ``REDUCTION_ALLOWED_UNDER_HALT`` rend cette autorisation explicite dans la décision : sans lui,
    on ne saurait pas distinguer « autorisé parce que tout va bien » de « autorisé parce que ça
    réduit ».
    """
    eng, store = engine()
    blocked = eng.evaluate_sync(increase(), context(halt_level=level))
    assert blocked.action is RiskAction.REJECT
    assert ReasonCode.HALTED.value in blocked.reason_codes
    assert level.reason_code.value in blocked.reason_codes
    assert store.active(SCOPE) == []

    eng_exit, store_exit = engine()
    allowed = eng_exit.evaluate_sync(reduction(), context(halt_level=level))
    assert allowed.action is RiskAction.ALLOW
    assert ReasonCode.REDUCTION_ALLOWED_UNDER_HALT.value in allowed.reason_codes
    assert len(store_exit.active(SCOPE)) == 1


def test_an_emergency_flatten_answers_flatten_to_an_increase_and_still_lets_it_exit() -> None:
    """En ``EMERGENCY_FLATTEN``, répondre REJECT à une augmentation serait incomplet : la réponse
    attendue est une DEMANDE DE SORTIE. Et la sortie elle-même doit rester possible, sinon l'état
    d'urgence empêcherait ce qu'il exige.
    """
    eng, store = engine()
    decision = eng.evaluate_sync(increase(), context(halt_level=HaltLevel.EMERGENCY_FLATTEN))

    assert decision.action is RiskAction.FLATTEN
    assert decision.allowed_payload_hash is None
    assert ReasonCode.EMERGENCY_FLATTEN.value in decision.reason_codes
    assert store.active(SCOPE) == []

    eng_exit, _ = engine()
    allowed = eng_exit.evaluate_sync(reduction(), context(halt_level=HaltLevel.EMERGENCY_FLATTEN))
    assert allowed.action is RiskAction.ALLOW


# --- kill switch : escalade seule ---------------------------------------------------------------------


def kill_switch(cfg: RiskCfg | None = None) -> tuple[KillSwitch, SimulatedClock]:
    clock = SimulatedClock(T0)
    switch = KillSwitch(
        account_scope=SCOPE,
        store=InMemoryRiskStateStore(),
        cfg=cfg or RiskCfg(),
        clock=clock,
        limits_version=limits().limits_version,
    )
    return switch, clock


def test_the_kill_switch_only_ever_escalates_on_request() -> None:
    """Une demande de niveau INFÉRIEUR n'abaisse rien. Si elle le faisait, un composant dégradé qui
    continue d'émettre « tout va bien » suffirait à lever une protection posée par un autre — le kill
    switch deviendrait décoratif.
    """
    switch, _ = kill_switch()
    assert switch.level is HaltLevel.NONE

    switch.request(HaltLevel.HARD_HALT, "divergence de positions")
    assert switch.level is HaltLevel.HARD_HALT

    switch.request(HaltLevel.SOFT_HALT, "signal moins grave")
    assert switch.level is HaltLevel.HARD_HALT, "le niveau a été abaissé par une simple demande"
    switch.request(HaltLevel.NONE, "reprise demandée sans action opérateur")
    assert switch.level is HaltLevel.HARD_HALT

    # Escalade vers l'urgence : elle passe, car elle monte.
    switch.request(HaltLevel.EMERGENCY_FLATTEN, "perte critique")
    assert switch.level is HaltLevel.EMERGENCY_FLATTEN
    assert max_level(HaltLevel.SOFT_HALT, HaltLevel.EMERGENCY_FLATTEN) is HaltLevel.EMERGENCY_FLATTEN


def test_a_critical_halt_never_lifts_itself_however_healthy_the_signals_become() -> None:
    """``auto_resume_after_critical_halt`` est faux par construction. Un HARD_HALT qui se lèverait
    seul au retour des signaux sains masquerait la cause : on ne saurait jamais si le problème a été
    compris ou s'il attend de revenir.
    """
    switch, clock = kill_switch()
    verdict = switch.observe(HealthSignals(now=clock.now_utc(), exchange_state_unknown=True))
    assert verdict.level is HaltLevel.HARD_HALT
    assert [t.name for t in verdict.triggers] == ["exchange_state_unknown"]

    clock.advance(timedelta(seconds=1))
    switch.observe(HealthSignals(now=clock.now_utc()))
    clock.advance(timedelta(seconds=10_000))  # bien au-delà de resume_stability_seconds
    still = switch.observe(HealthSignals(now=clock.now_utc()))

    assert still.level is HaltLevel.HARD_HALT
    assert ReasonCode.OPERATOR_ACTION_REQUIRED.value in {e.evidence["trigger"] for e in still.events}


def test_a_soft_halt_lifts_itself_only_after_the_declared_stability_delay() -> None:
    """Contre-épreuve du test précédent : un SOFT_HALT DOIT pouvoir se lever seul, sinon la moindre
    cotation en retard arrêterait le système jusqu'à intervention humaine. Mais seulement après le
    délai déclaré, et le compteur repart à zéro au moindre signal (hystérésis).
    """
    switch, clock = kill_switch(RiskCfg(resume_stability_seconds=120))
    assert switch.observe(HealthSignals(now=clock.now_utc(), data_stale=True)).level is HaltLevel.SOFT_HALT

    clock.advance(timedelta(seconds=1))
    switch.observe(HealthSignals(now=clock.now_utc()))  # premier signal sain : départ du compteur
    clock.advance(timedelta(seconds=60))
    assert switch.observe(HealthSignals(now=clock.now_utc())).level is HaltLevel.SOFT_HALT

    # Rechute : le compteur repart de zéro, la reprise est repoussée.
    clock.advance(timedelta(seconds=1))
    switch.observe(HealthSignals(now=clock.now_utc(), data_stale=True))
    clock.advance(timedelta(seconds=1))
    switch.observe(HealthSignals(now=clock.now_utc()))
    clock.advance(timedelta(seconds=60))
    assert switch.observe(HealthSignals(now=clock.now_utc())).level is HaltLevel.SOFT_HALT

    clock.advance(timedelta(seconds=61))
    resumed = switch.observe(HealthSignals(now=clock.now_utc()))
    assert resumed.level is HaltLevel.NONE
    assert resumed.stable_for_s >= 120


def test_a_jev_outage_is_never_a_protection_trigger() -> None:
    """§50 : une panne du service sémantique n'est pas un incident de risque. L'utiliser comme
    déclencheur de halt rendrait le trading dépendant d'un composant explicitement facultatif.
    """
    switch, clock = kill_switch()
    verdict = switch.observe(HealthSignals(now=clock.now_utc(), jev_unavailable=True))

    assert verdict.triggers == []
    assert verdict.level is HaltLevel.NONE

    # Contre-épreuve : un vrai déclencheur, lui, est bien vu.
    assert switch.observe(HealthSignals(now=clock.now_utc(), nan_detected=True)).level is HaltLevel.HARD_HALT


def test_only_an_authorized_operator_lifts_a_halt_and_only_under_preconditions() -> None:
    """La reprise après halt critique est une ACTION, pas un effet de bord. Elle exige un rôle
    autorisé, l'absence de déclencheur actif, une réconciliation saine et le délai de stabilité.
    Chacune de ces conditions est testée séparément : une reprise qui passerait pour la mauvaise
    raison remettrait du capital au travail sur un système encore cassé.
    """
    switch, clock = kill_switch(RiskCfg(resume_stability_seconds=120))
    switch.observe(HealthSignals(now=clock.now_utc(), positions_divergent=True))
    assert switch.level is HaltLevel.HARD_HALT

    healthy = HealthSignals(now=T0 + timedelta(seconds=200), reconciliation_ok=True)
    intruder = OperatorRequest(
        request_id="req-1", actor="stagiaire", role="viewer", reason="ça a l'air ok", requested_at=T0
    )
    with pytest.raises(HaltError) as role_error:
        switch.operator_resume(intruder, healthy)
    assert role_error.value.code == ReasonCode.OPERATOR_ACTION_REQUIRED.value
    assert switch.level is HaltLevel.HARD_HALT, "un refus de rôle ne doit rien changer"

    operator = OperatorRequest(
        request_id="req-2",
        actor="alice",
        role="operator",
        reason="divergence corrigée et réconciliée",
        requested_at=T0,
    )
    # Déclencheur encore actif : refus, et l'état ne bouge pas.
    with pytest.raises(HaltError) as trigger_error:
        switch.operator_resume(
            operator, HealthSignals(now=T0 + timedelta(seconds=200), positions_divergent=True)
        )
    assert trigger_error.value.code == ReasonCode.RESUME_PRECONDITIONS_UNMET.value
    assert switch.level is HaltLevel.HARD_HALT

    # Délai de stabilité non écoulé : refus également.
    with pytest.raises(HaltError):
        switch.operator_resume(operator, HealthSignals(now=T0 + timedelta(seconds=10)))
    assert switch.level is HaltLevel.HARD_HALT

    # Contre-épreuve : toutes les préconditions réunies, la reprise aboutit.
    assert switch.resume_preconditions(healthy) == []
    resumed = switch.operator_resume(operator, healthy)
    assert resumed.halt_level is HaltLevel.NONE
    assert resumed.halt_reason is None


def test_an_unmeasured_loss_is_none_and_never_a_reassuring_zero() -> None:
    """Sans equity de départ observée, la perte du jour et le drawdown sont INCONNUS. Rendre 0
    signifierait « aucune perte », c'est-à-dire l'affirmation la plus fausse et la plus permissive.
    """
    switch, _ = kill_switch()
    assert switch.daily_loss_fraction() is None
    assert switch.daily_loss_fraction(Decimal("90000")) is None
    assert switch.drawdown_fraction(None, None) is None
    assert switch.drawdown_fraction(Decimal("90000"), None) is None

    # Contre-épreuve : une fois l'equity observée, les deux mesures existent et sont non nulles.
    clock = SimulatedClock(T0)
    switch.observe(HealthSignals(now=clock.now_utc(), equity=Decimal("100000")))
    assert switch.daily_loss_fraction(Decimal("95000")) == Decimal("0.05")
    assert switch.drawdown_fraction(Decimal("95000"), None) == Decimal("0.05")


def sequence_of(levels: Sequence[HaltLevel]) -> list[int]:
    return [level.rank for level in levels]


def test_halt_levels_are_totally_ordered_and_all_of_them_block_increases() -> None:
    """Le rang est ce qui rend l'escalade décidable. Et tout niveau autre que ``NONE`` bloque les
    augmentations : il n'existe pas de halt « informatif » qui laisserait prendre du risque.
    """
    assert sequence_of(
        [HaltLevel.NONE, HaltLevel.SOFT_HALT, HaltLevel.HARD_HALT, HaltLevel.EMERGENCY_FLATTEN]
    ) == [0, 1, 2, 3]
    assert not HaltLevel.NONE.blocks_increases
    assert all(
        level.blocks_increases
        for level in (HaltLevel.SOFT_HALT, HaltLevel.HARD_HALT, HaltLevel.EMERGENCY_FLATTEN)
    )
    assert HaltLevel.HARD_HALT.is_critical and not HaltLevel.SOFT_HALT.is_critical

    switch, _ = kill_switch()
    switch.request(HaltLevel.SOFT_HALT, "prudence")
    assert switch.allows(increases_exposure=True) == (False, ReasonCode.SOFT_HALT)
    assert switch.allows(increases_exposure=False) == (True, ReasonCode.REDUCTION_ALLOWED_UNDER_HALT)


def test_the_engine_refuses_to_decide_without_a_context() -> None:
    """Sans contexte, aucune décision n'est possible. Rendre ALLOW par défaut serait la pire valeur
    par défaut imaginable ; rendre REJECT silencieusement masquerait un défaut de câblage.
    """
    eng, store = engine()
    with pytest.raises(ValueError):
        eng.evaluate_sync(increase())
    assert store.active(SCOPE) == [], "une évaluation impossible ne réserve rien"
