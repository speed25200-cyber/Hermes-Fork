"""Réutilisation d'un identifiant client déjà terminal : interdite par NOTRE journal (T36, §52).

Pourquoi ce cas compte. OKX impose sa propre unicité de ``clOrdId``, mais sur une fenêtre qui lui
appartient et que nous ne contrôlons pas : passé un certain âge, l'échange peut très bien accepter à
nouveau un identifiant que nous avons déjà utilisé. Si nous nous reposions sur ce refus-là, un
identifiant recyclé finirait par désigner DEUX intentions différentes dans notre historique. Tout ce
qui s'appuie sur lui — déduplication des événements du flux privé, réconciliation après un ACK perdu,
rapprochement des fills — attribuerait alors les exécutions de l'une à l'autre. §52 tranche : « le
client ID est généré une fois, persisté avant envoi et jamais réutilisé pour une nouvelle intention.
La limite d'unicité propre à OKX ne constitue pas notre journal historique d'idempotence. »

Le refus doit donc venir de nous, AVANT le réseau, et il doit valoir quel que soit l'état de l'ordre
précédent : terminal (l'échange l'a peut-être oublié) comme vivant. Les tests le vérifient avec un
adaptateur qui accepterait TOUT — c'est le seul moyen de prouver que le refus n'emprunte rien à
l'échange — et en comptant les envois réellement effectués.

Hermétisme : base SQLite en mémoire, horloge simulée, adaptateur d'exchange en mémoire. Aucun réseau.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from okxq.config.modes import Mode
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import IdempotencyError
from okxq.domain.events import (
    ApprovedOrder,
    OrderEvent,
    OrderEventKind,
    OrderIntent,
    RiskAction,
    RiskDecision,
    SubmissionOutcome,
)
from okxq.domain.money import Side
from okxq.domain.orders import OrderKind, OrderState, is_terminal
from okxq.exchange.base import ExchangeEvent, OrderRequest, PlaceOutcome, PlaceResponse
from okxq.execution.gateway import DurableExecutionGateway, GatewaySettings, RecheckResult
from okxq.execution.leadership import LeaseManager
from okxq.persistence.db import memory_engine
from okxq.persistence.repositories import UnitOfWorkFactory

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SCOPE = "test-T36"
INST = "BTC-USDT-SWAP"
REUSED = "cl_T36_reutilise"


class PermissiveAdapter:
    """Adaptateur qui ACCEPTE tout, y compris un identifiant déjà vu.

    C'est délibéré : il joue le rôle d'un échange dont la fenêtre d'unicité a expiré. Si un test de
    refus passe avec cet adaptateur, c'est que le refus vient bien de notre journal.
    """

    is_real_exchange = False
    name = "adaptateur-permissif"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def place_order(self, request: OrderRequest) -> PlaceResponse:
        self.sent.append(request.client_order_id)
        return PlaceResponse(
            outcome=PlaceOutcome.ACK,
            client_order_id=request.client_order_id,
            exchange_order_id=f"ex_{len(self.sent)}",
            code="0",
            sent_at=T0,
            ack_at=T0,
        )


class AlwaysApproves:
    """Re-vérification au moment de l'envoi : hors sujet ici, donc toujours favorable."""

    async def recheck(self, approved: ApprovedOrder, *, now: datetime) -> RecheckResult:
        return RecheckResult(ok=True)


def approved_order(client_order_id: str, intent_id: str, *, now: datetime) -> ApprovedOrder:
    """Un ``ApprovedOrder`` construit par les VRAIS contrats : l'approbation reste liée au hash (T49)."""
    intent = OrderIntent(
        intent_id=intent_id,
        decision_id="dec_T36",
        target_id="tgt_T36",
        account_scope=SCOPE,
        inst_id=INST,
        side=Side.BUY,
        contracts=Decimal("1"),
        price_limit=Decimal("65000"),
        order_type=OrderKind.LIMIT,
        reduce_only=False,
        ttl_ms=60_000,
        reason="T36",
        client_order_id=client_order_id,
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    decision = RiskDecision(
        decision_id=f"rd_{intent_id}",
        intent_id=intent_id,
        intent_hash=intent.payload_hash(),
        action=RiskAction.ALLOW,
        allowed_payload_hash=intent.payload_hash(),
        allowed_contracts=intent.contracts,
        limits_version="limits-T36",
        position_version="pos-T36",
        reservations={"notional_usdt": "650"},
        created_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    return ApprovedOrder(intent=intent, decision=decision, payload_hash=intent.payload_hash())


@pytest.fixture
def gateway() -> tuple[DurableExecutionGateway, PermissiveAdapter, UnitOfWorkFactory, SimulatedClock]:
    engine = memory_engine()
    uow_factory = UnitOfWorkFactory(engine)
    clock = SimulatedClock(T0)
    with uow_factory.transaction() as uow:
        # La décision est l'aïeule de l'intention en base : sans elle, l'insertion violerait une clé.
        uow.decisions.ensure(
            "dec_T36", mode=Mode.PAPER.value, snapshot_id="snap_T36", cutoff_at=T0, started_at=T0
        )
    leadership = LeaseManager(uow_factory, holder_id="w_T36", clock=clock, ttl_ms=30_000, heartbeat_ms=5_000)
    assert leadership.try_acquire() is True
    adapter = PermissiveAdapter()
    gw = DurableExecutionGateway(
        adapter=adapter,
        uow_factory=uow_factory,
        leadership=leadership,
        rechecker=AlwaysApproves(),
        clock=clock,
        settings=GatewaySettings(
            mode=Mode.PAPER,
            account_scope=SCOPE,
            approval_ttl=timedelta(seconds=60),
            max_intent_age=timedelta(seconds=60),
            # La réconciliation n'est pas le sujet de T36 : on ne veut pas qu'elle masque le refus.
            require_reconciliation_before_entries=False,
        ),
        worker_id="w_T36",
    )
    return gw, adapter, uow_factory, clock


async def drive_to_filled(gw: DurableExecutionGateway, client_order_id: str, *, at: datetime) -> None:
    """Amène l'ordre à un état TERMINAL par le chemin réel : événement du flux privé, pas une écriture."""
    event = ExchangeEvent(
        kind="order",
        receive_ts=at,
        order_event=OrderEvent(
            event_id=f"ev_{client_order_id}",
            client_order_id=client_order_id,
            exchange_order_id="ex_1",
            event_kind=OrderEventKind.FILL,
            observed_state=OrderState.FILLED,
            cumulative_filled=Decimal("1"),
            average_fill_price=Decimal("65000"),
            event_ts=at,
            receive_ts=at,
        ),
    )
    result = await gw.consume_event(event)
    assert result is not None
    assert is_terminal(result.projection.observed_state)


async def test_T36_un_client_order_id_deja_terminal_ne_peut_pas_etre_reutilise(gateway) -> None:
    """Ordre exécuté, donc TERMINAL : le même ``client_order_id`` pour une NOUVELLE intention est refusé.

    Le refus est typé, il nomme l'état terminal rencontré, et il intervient AVANT le réseau : le
    compteur d'envois de l'adaptateur ne bouge pas, alors que cet adaptateur aurait accepté. C'est
    exactement la promesse de §52 : notre journal, pas la fenêtre d'unicité de l'échange.
    """
    gw, adapter, _, clock = gateway
    first = await gw.submit(approved_order(REUSED, "int_T36_1", now=clock.now_utc()))
    assert first.outcome is SubmissionOutcome.ACK
    assert adapter.sent == [REUSED]

    await drive_to_filled(gw, REUSED, at=T0 + timedelta(seconds=1))

    with pytest.raises(IdempotencyError) as err:
        gw.stage(approved_order(REUSED, "int_T36_2", now=clock.now_utc()))
    assert err.value.context["client_order_id"] == REUSED
    assert err.value.context["observed_state"] == OrderState.FILLED.value
    assert is_terminal(OrderState(str(err.value.context["observed_state"])))
    assert "terminal" in str(err.value)
    # Aucun second envoi : le refus n'a rien demandé à l'échange.
    assert adapter.sent == [REUSED]


async def test_T36_un_client_order_id_encore_vivant_est_refuse_aussi(gateway) -> None:
    """Même refus si l'ordre précédent est ACKNOWLEDGED : un identifiant ne sert jamais deux intentions.

    L'état terminal n'est pas la condition du refus, seulement sa circonstance la plus trompeuse (celle
    où l'échange, lui, aurait oublié l'identifiant). Un ordre vivant doit être refusé tout autant,
    sinon deux intentions concurrentes partageraient la même clé de déduplication.
    """
    gw, adapter, _, clock = gateway
    first = await gw.submit(approved_order(REUSED, "int_T36_1", now=clock.now_utc()))
    assert first.outcome is SubmissionOutcome.ACK

    with pytest.raises(IdempotencyError) as err:
        gw.stage(approved_order(REUSED, "int_T36_2", now=clock.now_utc()))
    assert err.value.context["observed_state"] == OrderState.ACKNOWLEDGED.value
    assert is_terminal(OrderState(str(err.value.context["observed_state"]))) is False
    assert adapter.sent == [REUSED]


async def test_T36_un_identifiant_neuf_reste_accepte(gateway) -> None:
    """CONTRE-ÉPREUVE indispensable : sans elle, un gateway qui refuserait TOUT passerait les tests.

    Après le refus d'un identifiant recyclé, un identifiant neuf doit être mis en file, envoyé, et
    acquitté normalement.
    """
    gw, adapter, _, clock = gateway
    await gw.submit(approved_order(REUSED, "int_T36_1", now=clock.now_utc()))
    await drive_to_filled(gw, REUSED, at=T0 + timedelta(seconds=1))
    with pytest.raises(IdempotencyError):
        gw.stage(approved_order(REUSED, "int_T36_2", now=clock.now_utc()))

    fresh = await gw.submit(approved_order("cl_T36_neuf", "int_T36_3", now=clock.now_utc()))
    assert fresh.outcome is SubmissionOutcome.ACK
    assert fresh.exchange_order_id is not None
    assert adapter.sent == [REUSED, "cl_T36_neuf"]


async def test_T36_le_refus_est_deja_pose_par_le_journal_des_intentions(gateway) -> None:
    """Le refus vit dans la couche de persistance, pas dans une garde de surface du gateway.

    On tente l'insertion directement dans le dépôt d'intentions : c'est le point de passage obligé de
    toute nouvelle intention, y compris pour un futur appelant qui n'utiliserait pas ``stage``. Un
    contrôle placé plus haut serait contournable ; celui-là ne l'est pas.
    """
    gw, _, uow_factory, clock = gateway
    await gw.submit(approved_order(REUSED, "int_T36_1", now=clock.now_utc()))

    with pytest.raises(IdempotencyError):
        with uow_factory.transaction() as uow:
            uow.intents.insert(approved_order(REUSED, "int_T36_2", now=clock.now_utc()))
    # Contre-épreuve : le même dépôt accepte une intention dont l'identifiant est neuf.
    with uow_factory.transaction() as uow:
        row = uow.intents.insert(approved_order("cl_T36_autre", "int_T36_4", now=clock.now_utc()))
    assert row.client_order_id == "cl_T36_autre"


async def test_T36_un_lot_refuse_lidentifiant_recycle_sans_penaliser_les_autres(gateway) -> None:
    """Dans un lot, l'identifiant recyclé est le seul refusé : les autres ordres partent normalement.

    Un refus d'idempotence qui ferait tomber tout le lot transformerait une erreur de programmation
    locale en panne d'exécution générale ; l'inverse — supposer le lot entièrement accepté — perdrait
    le refus. Le traitement est donc item par item, et chaque item porte son propre verdict.
    """
    gw, adapter, _, clock = gateway
    await gw.submit(approved_order(REUSED, "int_T36_1", now=clock.now_utc()))
    await drive_to_filled(gw, REUSED, at=T0 + timedelta(seconds=1))

    results = await gw.submit_many(
        [
            approved_order(REUSED, "int_T36_2", now=clock.now_utc()),
            approved_order("cl_T36_lot", "int_T36_5", now=clock.now_utc()),
        ]
    )
    by_id = {r.client_order_id: r for r in results}
    assert by_id[REUSED].outcome is SubmissionOutcome.NOT_SENT
    assert by_id[REUSED].error_code == IdempotencyError.code
    assert by_id["cl_T36_lot"].outcome is SubmissionOutcome.ACK
    assert adapter.sent == [REUSED, "cl_T36_lot"]
