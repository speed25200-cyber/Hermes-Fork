"""T59 et T61 : une protection non confirmée, et une stratégie morte sous un heartbeat vivant.

Ces deux cas partagent la même erreur de raisonnement, et c'est pourquoi ils sont testés ensemble :
croire qu'une chose est vraie parce qu'on l'a DEMANDÉE.

- T59 : un stop envoyé n'est pas un stop actif. Tant que l'exchange ne l'a pas confirmé, la position
  est NON PROTÉGÉE, et l'afficher comme protégée est le pire des deux mondes — on prend du risque en
  croyant être couvert.
- T61 : un heartbeat qui bat parce qu'un fil d'exécution tourne ne dit rien de la stratégie. Un
  heartbeat inconditionnel laisserait des prises de risque orphelines sous surveillance apparente.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from okxq.domain.clocks import SimulatedClock
from okxq.domain.events import SubmissionOutcome, SubmissionResult
from okxq.domain.money import Side
from okxq.domain.reasons import ReasonCode
from okxq.exchange.base import PositionStatus, ProtectionRequest, ProtectionStatus
from okxq.execution.protections import ProtectionManager, UnprotectedPolicy
from okxq.risk.kill_switch import HaltLevel
from okxq.risk.watchdog import ObservedState, Watchdog

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
SCOPE = "compte-de-test"


def position(contracts: str = "3") -> PositionStatus:
    return PositionStatus(
        inst_id=INST,
        signed_contracts=Decimal(contracts),
        average_price=Decimal("65000"),
        mark_price=Decimal("65100"),
        liquidation_price=Decimal("58000"),
        margin=Decimal("400"),
        leverage=Decimal("5"),
        as_of=T0,
    )


class _Adapter:
    """Adaptateur d'échange factice : c'est LUI qui décide si le stop est confirmé."""

    is_real_exchange = False

    def __init__(self, *, state: str | None = "live", raise_on_place: bool = False) -> None:
        self._state = state
        self._raise = raise_on_place
        self.placed: list[ProtectionRequest] = []
        self.cancelled: list[str] = []

    async def place_protection(self, request: ProtectionRequest) -> ProtectionStatus:
        self.placed.append(request)
        if self._raise:
            raise TimeoutError("exchange injoignable pendant la pose du stop")
        return ProtectionStatus(
            client_algo_id=request.client_algo_id,
            exchange_algo_id="algo-exchange-1",
            state=self._state or "unknown",
            trigger_price=request.trigger_price,
            contracts=request.contracts,
            as_of=T0,
        )

    async def cancel_protection(self, scope: str, client_algo_id: str, inst_id: str) -> None:
        self.cancelled.append(client_algo_id)


def _manager(adapter: Any, **kwargs: Any) -> ProtectionManager:
    return ProtectionManager(adapter=adapter, clock=SimulatedClock(T0), account_scope=SCOPE, **kwargs)


# --- T59 : stop prévu mais non confirmé ---------------------------------------------------------------


def test_T59_a_confirmed_stop_protects_the_position() -> None:
    """Contre-épreuve indispensable : sans elle, un test de non-protection passerait aussi sur un
    gestionnaire qui ne protège jamais rien."""
    adapter = _Adapter(state="live")
    outcome = asyncio.run(_manager(adapter).protect_position(position(), stop_price=Decimal("62000")))
    assert outcome.protected is True
    assert outcome.action_taken == "stop_confirmed"
    assert outcome.protected_contracts == Decimal("3")
    # Le stop ne dépasse JAMAIS la quantité ouverte : sinon il inverserait la position.
    assert adapter.placed[0].contracts == Decimal("3")
    assert adapter.placed[0].reduce_only is True


@pytest.mark.parametrize("etat", ["rejected", "canceled", "unknown"])
def test_T59_a_stop_not_accepted_leaves_the_position_marked_unprotected(etat: str) -> None:
    """Un stop rejeté, annulé ou d'état inconnu ne protège rien. « Inconnu » compte comme non protégé :
    supposer le contraire, c'est prendre du risque en croyant être couvert."""
    manager = _manager(_Adapter(state=etat))
    outcome = asyncio.run(manager.protect_position(position(), stop_price=Decimal("62000")))
    assert outcome.protected is False
    assert manager.unprotected[INST].protected is False
    assert INST not in manager.active, "un stop non confirmé ne doit pas figurer parmi les actifs"


def test_T59_an_exchange_error_while_placing_the_stop_is_not_a_protection() -> None:
    """Un appel qui échoue ne dit rien de l'état côté exchange : la position reste non protégée."""
    manager = _manager(_Adapter(raise_on_place=True))
    outcome = asyncio.run(manager.protect_position(position(), stop_price=Decimal("62000")))
    assert outcome.protected is False
    assert "exchange_error" in outcome.reason
    assert INST in manager.unprotected


def test_T59_the_reduce_policy_requires_a_way_to_actually_reduce() -> None:
    """Annoncer une politique de réduction sans moyen de réduire serait une promesse vide."""
    with pytest.raises(ValueError, match="reduce_callback"):
        _manager(_Adapter(), on_unprotected=UnprotectedPolicy.REDUCE)


def test_T59_under_the_reduce_policy_an_unconfirmed_stop_triggers_a_reduction() -> None:
    """Sous la politique REDUCE, la position non protégée doit être RÉDUITE, pas seulement signalée."""
    reduites: list[tuple[str, Side, Decimal]] = []

    async def reduire(inst_id: str, side: Side, contracts: Decimal, now: datetime) -> SubmissionResult:
        reduites.append((inst_id, side, contracts))
        return SubmissionResult(
            intent_id="int_reduction",
            client_order_id="cl_reduction",
            outcome=SubmissionOutcome.ACK,
            sent_at=now,
        )

    manager = _manager(
        _Adapter(state="rejected"),
        on_unprotected=UnprotectedPolicy.REDUCE,
        reduce_callback=reduire,
    )
    outcome = asyncio.run(manager.protect_position(position(), stop_price=Decimal("62000")))
    assert outcome.protected is False
    # Une position LONGUE se réduit en VENDANT : un sens inversé aggraverait l'exposition.
    assert reduites == [(INST, Side.SELL, Decimal("3"))]


def test_T59_a_flat_position_needs_no_protection() -> None:
    manager = _manager(_Adapter())
    outcome = asyncio.run(manager.protect_position(position("0"), stop_price=Decimal("62000")))
    assert outcome.protected is True
    assert outcome.action_taken == "none"
    assert INST not in manager.unprotected


# --- T61 : stratégie morte, heartbeat vivant ----------------------------------------------------------


def _watchdog(clock: SimulatedClock) -> Watchdog:
    return Watchdog(
        clock=clock,
        max_decision_age_s=180.0,
        max_reconciliation_age_s=300.0,
        max_private_stream_age_s=30.0,
        max_heartbeat_age_s=30.0,
    )


def test_T61_a_heartbeat_without_observed_state_is_refused() -> None:
    """Un heartbeat inconditionnel ne prouve qu'une chose : un fil d'exécution tourne. C'est
    exactement ce qu'un processus zombie fait aussi."""
    result = _watchdog(SimulatedClock(T0)).heartbeat("strategy", None)
    assert result.accepted is False
    assert result.reason is ReasonCode.HEARTBEAT_UNCONDITIONED


def test_T61_a_heartbeat_carrying_a_dead_strategy_is_refused() -> None:
    """Le heartbeat porte l'état observé ; un état qui dit « morte » ne peut pas être accepté."""
    clock = SimulatedClock(T0)
    result = _watchdog(clock).heartbeat(
        "strategy",
        ObservedState(observed_at=T0, strategy_alive=False, strategy_last_decision_at=T0),
    )
    assert result.accepted is False


def test_T61_a_stale_decision_makes_the_strategy_unhealthy_even_if_it_claims_to_be_alive() -> None:
    """C'est le cœur de T61 : le processus se déclare vivant, mais il n'a plus rien décidé.

    Se fier à la déclaration plutôt qu'à la dernière décision observée laisserait des prises de
    risque orphelines sous une surveillance purement apparente.
    """
    clock = SimulatedClock(T0)
    watchdog = _watchdog(clock)
    ok = watchdog.heartbeat(
        "strategy",
        ObservedState(observed_at=T0, strategy_alive=True, strategy_last_decision_at=T0),
    )
    assert ok.accepted is True, "contre-épreuve : une stratégie saine doit être acceptée"

    clock.advance(timedelta(seconds=600))
    now = clock.now_utc()
    stale = watchdog.heartbeat(
        "strategy",
        # Elle se dit vivante, et son heartbeat est frais ; seule la DERNIÈRE DÉCISION est vieille.
        ObservedState(observed_at=now, strategy_alive=True, strategy_last_decision_at=T0),
    )
    assert stale.accepted is False
    assert stale.reason is not ReasonCode.OK

    verdict = watchdog.evaluate()
    assert verdict.strategy_ok is False
    policy = verdict.policy
    assert policy.allow_new_entries is False, "aucune nouvelle entrée sans stratégie saine"
    assert policy.cancel_orphan_entries is True, "les entrées orphelines doivent être annulées"
    # La supervision des positions existantes NE s'arrête PAS : abandonner une position ouverte parce
    # que la stratégie est morte serait le pire choix possible.
    assert policy.keep_position_supervision is True
    assert policy.requested_halt is not HaltLevel.NONE


def test_T61_a_healthy_system_allows_entries() -> None:
    """Contre-épreuve globale : sans elle, tous les tests ci-dessus passeraient sur un watchdog qui
    refuse tout en permanence."""
    clock = SimulatedClock(T0)
    watchdog = _watchdog(clock)
    observed = ObservedState(
        observed_at=T0,
        strategy_alive=True,
        strategy_last_decision_at=T0,
        reconciliation_completed_at=T0,
        private_stream_last_at=T0,
        leadership_held=True,
    )
    for component in ("strategy", "reconciliation", "private_stream", "leadership"):
        assert watchdog.heartbeat(component, observed).accepted is True, component
    verdict = watchdog.evaluate()
    assert verdict.strategy_ok and verdict.reconciliation_ok
    assert verdict.private_stream_ok and verdict.leadership_ok
    assert verdict.policy.allow_new_entries is True
    assert verdict.policy.requested_halt is HaltLevel.NONE
