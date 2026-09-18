"""Approbations, recontrôle à l'envoi et writer unique : T46, T47, T48 (§25, §52.3, §53).

POURQUOI ces cas comptent.

**T48 — une approbation est PÉRISSABLE.** Elle a été calculée sur une equity, des positions et des
limites datées. Entre l'approbation et l'envoi il s'écoule un temps pendant lequel tout peut changer.
Un ordre envoyé sur une approbation expirée est un ordre décidé par un système qui n'existe plus. Le
recontrôle au moment de l'envoi est le seul endroit où cela peut encore être arrêté, et le test doit
vérifier que l'ordre n'est pas ENVOYÉ — pas seulement qu'un booléen vaut ``False``.

**T46/T47 — un seul chemin de signature et d'envoi.** Deux gateways qui envoient le même panier
doublent l'exposition sans qu'aucune limite ne soit franchie côté modèle. Le bail en base et son
jeton de cloisonnement ne servent qu'à cela. §52.3 est explicite : « en cas d'impossibilité de
confirmer l'autorité d'envoi, suspendre toute augmentation de risque ». L'implémentation va plus loin
et suspend TOUT envoi, ce qui satisfait l'exigence a fortiori — les tests vérifient donc l'incapacité
d'augmenter le risque, et la permission de réduire est vérifiée là où elle s'applique vraiment (halt,
réconciliation en attente), plus bas dans ce fichier.

Toutes les ressources sont locales : SQLite en mémoire, horloge simulée, adaptateur d'exchange factice
qui ENREGISTRE ce qu'on lui demande d'envoyer. C'est cet enregistrement qui prouve l'absence d'effet.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from okxq.config.modes import Mode
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import ApprovalError, LeadershipError, OrderStateError
from okxq.domain.events import (
    ApprovedOrder,
    OrderIntent,
    RiskAction,
    RiskDecision,
    SubmissionOutcome,
)
from okxq.domain.ids import payload_hash
from okxq.domain.money import Side
from okxq.domain.orders import OrderKind, OrderState
from okxq.domain.reasons import ReasonCode
from okxq.exchange.base import OrderRequest, PlaceOutcome, PlaceResponse
from okxq.execution.gateway import (
    DurableExecutionGateway,
    GatewaySettings,
    RecheckResult,
)
from okxq.execution.leadership import DEFAULT_LEASE_NAME, LeadershipState, LeaseManager
from okxq.persistence.db import memory_engine
from okxq.persistence.repositories import UnitOfWorkFactory
from okxq.risk.approvals import (
    InMemoryReservationStore,
    Reservation,
    SendAuthorization,
    SendContext,
    authorize_send,
    bind_approval,
    recheck_at_send,
)
from okxq.risk.kill_switch import HaltLevel

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SCOPE = "scope-test"
INST = "AAA-USDT-SWAP"
APPROVAL_TTL = timedelta(milliseconds=2_000)
INTENT_TTL_MS = 3_000


# --- fabriques ----------------------------------------------------------------------------------------


def intent(
    *,
    contracts: str = "10",
    reduce_only: bool = False,
    client_order_id: str = "cid-1",
    created_at: datetime = T0,
) -> OrderIntent:
    return OrderIntent(
        intent_id=f"int-{client_order_id}",
        decision_id="dec-1",
        target_id="tgt-1",
        account_scope=SCOPE,
        inst_id=INST,
        side=Side.BUY,
        contracts=Decimal(contracts),
        price_limit=Decimal("1000.0"),
        order_type=OrderKind.POST_ONLY,
        reduce_only=reduce_only,
        ttl_ms=INTENT_TTL_MS,
        reason="test",
        client_order_id=client_order_id,
        created_at=created_at,
        expires_at=created_at + timedelta(milliseconds=INTENT_TTL_MS),
    )


def decision(
    order_intent: OrderIntent,
    *,
    action: RiskAction = RiskAction.ALLOW,
    allowed_contracts: Decimal | None = None,
    allowed_hash: str | None = None,
    limits_version: str = "limits-1",
    position_version: str = "pos-1",
    created_at: datetime = T0,
    reservations: Mapping[str, str] | None = None,
) -> RiskDecision:
    return RiskDecision(
        decision_id="dec-1",
        intent_id=order_intent.intent_id,
        intent_hash=order_intent.payload_hash(),
        action=action,
        allowed_payload_hash=allowed_hash or order_intent.payload_hash(),
        allowed_contracts=allowed_contracts or order_intent.contracts,
        limits_version=limits_version,
        position_version=position_version,
        reservations=dict(reservations or {}),
        created_at=created_at,
        expires_at=created_at + APPROVAL_TTL,
        reason_codes=[ReasonCode.OK.value],
    )


def approved(**kwargs: Any) -> ApprovedOrder:
    order_intent = intent(**kwargs)
    return bind_approval(order_intent, decision(order_intent))


def send_context(**overrides: Any) -> SendContext:
    values: dict[str, Any] = {
        "now": T0 + timedelta(milliseconds=500),
        "position_version": "pos-1",
        "limits_version": "limits-1",
        "halt_level": HaltLevel.NONE,
        "is_leader": True,
        "reconciliation_ok": True,
    }
    values.update(overrides)
    return SendContext(**values)


@dataclass
class RecordingAdapter:
    """Adaptateur factice : il ENREGISTRE les envois au lieu de les faire.

    C'est l'instrument de mesure de tout ce fichier : un refus n'est prouvé que si cette liste reste
    vide. Un test qui ne regarderait que le code de retour passerait sur un gateway qui envoie quand
    même et rapporte un refus.
    """

    requests: list[OrderRequest] = field(default_factory=list)
    outcome: PlaceOutcome = PlaceOutcome.ACK

    @property
    def name(self) -> str:
        return "recording"

    async def place_order(self, request: OrderRequest) -> PlaceResponse:
        self.requests.append(request)
        return PlaceResponse(
            outcome=self.outcome,
            client_order_id=request.client_order_id,
            exchange_order_id="ex-1" if self.outcome is PlaceOutcome.ACK else None,
        )


@dataclass
class StubRechecker:
    """``ApprovalRechecker`` injecté : le gateway doit RESPECTER sa réponse, pas la contourner."""

    ok: bool = True
    reason_codes: tuple[str, ...] = ()
    calls: int = 0

    async def recheck(self, approved_order: ApprovedOrder, *, now: datetime) -> RecheckResult:
        self.calls += 1
        return RecheckResult(ok=self.ok, reason_codes=self.reason_codes)


class FlakyUowFactory:
    """Enveloppe une vraie fabrique et simule une perte de base à la demande (T47)."""

    def __init__(self, inner: UnitOfWorkFactory) -> None:
        self._inner = inner
        self.broken = False

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        if self.broken:
            raise RuntimeError("base de données injoignable")
        with self._inner.transaction() as uow:
            yield uow


class UnreachableLeases:
    def is_current(self, *_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("base de données injoignable")


@dataclass
class UowWithUnreachableLeases:
    leases: UnreachableLeases = field(default_factory=UnreachableLeases)


@dataclass
class Rig:
    clock: SimulatedClock
    uow: UnitOfWorkFactory
    adapter: RecordingAdapter
    rechecker: StubRechecker
    lease: LeaseManager
    gateway: DurableExecutionGateway


def build_rig(
    *,
    clock: SimulatedClock | None = None,
    uow: UnitOfWorkFactory | None = None,
    holder_id: str = "gateway-a",
    rechecker: StubRechecker | None = None,
    lease: LeaseManager | None = None,
    lease_ttl_ms: int = 10_000,
) -> Rig:
    clock = clock or SimulatedClock(T0)
    uow = uow or UnitOfWorkFactory(memory_engine())
    adapter = RecordingAdapter()
    rechecker = rechecker or StubRechecker()
    lease = lease or LeaseManager(
        uow, holder_id=holder_id, clock=clock, ttl_ms=lease_ttl_ms, heartbeat_ms=lease_ttl_ms // 4
    )
    gateway = DurableExecutionGateway(
        adapter=adapter,  # type: ignore[arg-type]
        uow_factory=uow,
        leadership=lease,
        rechecker=rechecker,
        clock=clock,
        settings=GatewaySettings(
            mode=Mode.PAPER,
            account_scope=SCOPE,
            approval_ttl=APPROVAL_TTL,
            max_intent_age=timedelta(milliseconds=INTENT_TTL_MS),
            require_reconciliation_before_entries=False,
        ),
        worker_id=holder_id,
    )
    # La ligne de décision est la clé étrangère de toute intention : sans elle, la base refuse
    # l'insertion. On la crée donc ici comme le ferait la boucle de décision.
    with uow.transaction() as tx:
        tx.decisions.ensure("dec-1", mode=Mode.PAPER.value, snapshot_id="snap-1", cutoff_at=T0, started_at=T0)
    return Rig(clock, uow, adapter, rechecker, lease, gateway)


def reservations_of(rig: Rig, intent_id: str) -> list[tuple[str, str]]:
    with rig.uow.transaction() as uow:
        return [
            (r.reservation_id, r.status) for r in uow.reservations.active(SCOPE) if r.intent_id == intent_id
        ]


# --- T48 : intention ou approbation expirée → l'ordre n'est PAS envoyé ---------------------------------


async def test_T48_an_expired_approval_never_reaches_the_exchange() -> None:
    """T48 : l'approbation est préparée à T0 puis l'envoi a lieu après son expiration.

    Le scénario est banal : un gateway ralenti, une file d'attente, un redémarrage. Ce qui ne doit pas
    être banal, c'est l'ordre qui part quand même. La preuve du refus est l'adaptateur VIDE.
    """
    rig = build_rig()
    assert rig.lease.try_acquire()
    staged = rig.gateway.stage(approved())

    rig.clock.advance(APPROVAL_TTL + timedelta(milliseconds=1))
    result = await rig.gateway.dispatch_order(staged.order_id)

    assert result.outcome is SubmissionOutcome.NOT_SENT
    assert ReasonCode.APPROVAL_EXPIRED.value in (result.message or "")
    assert rig.adapter.requests == [], "un ordre expiré a été envoyé"
    # Absence d'effet : l'ordre est refusé localement et la réservation ne bloque plus de budget.
    with rig.uow.transaction() as uow:
        row = uow.orders.get(staged.order_id)
        assert row is not None
        assert row.observed_state == OrderState.REJECTED.value
        assert row.attempt_count == 0, "aucune tentative réseau n'a été comptée"
    assert reservations_of(rig, staged.intent_id) == []


async def test_T48_a_fresh_approval_does_reach_the_exchange_with_its_fencing_token() -> None:
    """Contre-épreuve indispensable de T48 : dans son TTL, le même ordre PART. Sans ce test, tous les
    refus ci-dessus passeraient sur un gateway qui n'envoie plus rien du tout. On vérifie au passage
    que le jeton de cloisonnement accompagne l'envoi : c'est lui qui rend le chemin unique (T46).
    """
    rig = build_rig()
    assert rig.lease.try_acquire()
    token = rig.lease.fencing_token

    result = await rig.gateway.submit(approved())

    assert result.outcome is SubmissionOutcome.ACK
    assert len(rig.adapter.requests) == 1
    sent = rig.adapter.requests[0]
    assert sent.client_order_id == "cid-1"
    assert sent.contracts == Decimal("10")
    assert sent.fencing_token == token
    assert rig.rechecker.calls == 1, "le recontrôle injecté doit être consulté à chaque envoi"
    # Le payload envoyé est EXACTEMENT celui qui a été approuvé.
    approved_order = approved()
    assert (
        payload_hash(
            {
                "account_scope": sent.account_scope,
                "client_order_id": sent.client_order_id,
                "inst_id": sent.inst_id,
                "side": sent.side,
                "contracts": format(sent.contracts, "f"),
                "price_limit": None if sent.price_limit is None else format(sent.price_limit, "f"),
                "order_type": sent.order_type,
                "reduce_only": sent.reduce_only,
            }
        )
        == approved_order.payload_hash
    )


async def test_T48_the_gateway_obeys_a_refusing_approval_rechecker() -> None:
    """T48 : le ``ApprovalRechecker`` vit dans le module de risque ; le gateway ne fait que l'appeler
    et LUI OBÉIR. S'il pouvait passer outre — même « juste pour un ordre reduce-only » — le
    recontrôle ne serait qu'un journal.
    """
    refusing = StubRechecker(ok=False, reason_codes=(ReasonCode.POSITION_VERSION_CHANGED.value,))
    rig = build_rig(rechecker=refusing)
    assert rig.lease.try_acquire()

    result = await rig.gateway.submit(approved())

    assert result.outcome is SubmissionOutcome.NOT_SENT
    assert result.error_code == ReasonCode.POSITION_VERSION_CHANGED.value
    assert rig.adapter.requests == []
    assert refusing.calls == 1


def test_T48_recheck_at_send_refuses_an_expired_approval_or_intention() -> None:
    """T48 au niveau du recontrôle lui-même : l'approbation et l'intention ont des horloges
    DISTINCTES. Confondre les deux laisserait passer une intention périmée portée par une
    approbation encore fraîche — un ordre calculé sur des prix qui n'existent plus.
    """
    order = approved()

    fresh = recheck_at_send(order, send_context())
    assert fresh.ok
    assert fresh.reason_codes == []

    expired_approval = recheck_at_send(order, send_context(now=T0 + APPROVAL_TTL))
    assert not expired_approval.ok
    assert ReasonCode.APPROVAL_EXPIRED.value in expired_approval.reason_codes

    expired_intent = recheck_at_send(order, send_context(now=T0 + timedelta(milliseconds=INTENT_TTL_MS)))
    assert not expired_intent.ok
    assert ReasonCode.INTENT_EXPIRED.value in expired_intent.reason_codes


def test_T48_no_send_authorization_can_exist_without_a_successful_recheck() -> None:
    """Propriété d'impossibilité : le seul objet que le gateway accepte est une ``SendAuthorization``,
    et elle ne se construit QUE par ``authorize_send``. Un refus ne se contourne donc pas en
    fabriquant l'objet à la main — l'impossibilité est structurelle, pas une convention.
    """
    order = approved()

    granted = authorize_send(order, send_context())
    assert isinstance(granted, SendAuthorization)
    assert granted.payload_hash == order.payload_hash
    assert granted.authorized_at == T0 + timedelta(milliseconds=500)

    with pytest.raises(ApprovalError) as refused:
        authorize_send(order, send_context(now=T0 + APPROVAL_TTL))
    assert refused.value.code == ReasonCode.APPROVAL_EXPIRED.value

    with pytest.raises(ApprovalError):
        SendAuthorization(
            approved=order,
            payload=dict(order.intent.normalized_payload()),
            payload_hash="peu importe",
            authorized_at=T0,
        )


# --- approbation liée au hash du payload NORMALISÉ ----------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "tampered"),
    [
        ("contracts", "11"),
        ("price_limit", "1001.0"),
        ("side", "sell"),
        ("reduce_only", True),
        ("inst_id", "BBB-USDT-SWAP"),
        ("client_order_id", "cid-2"),
        ("order_type", "market"),
    ],
)
def test_a_single_modified_field_invalidates_the_approval(field_name: str, tampered: object) -> None:
    """Une approbation porte sur le hash du payload NORMALISÉ, champ par champ.

    Chaque champ est testé séparément : une vérification qui ne couvrirait que la taille laisserait
    modifier le prix, le sens ou l'instrument après approbation. C'est exactement la faille qui
    transforme une approbation en blanc-seing.
    """
    order = approved()
    sent = dict(order.intent.normalized_payload())
    sent[field_name] = tampered

    result = recheck_at_send(order, send_context(), payload=sent)
    assert not result.ok
    assert ReasonCode.PAYLOAD_HASH_MISMATCH.value in result.reason_codes

    # Contre-épreuve : le payload intact passe.
    assert recheck_at_send(order, send_context(), payload=order.intent.normalized_payload()).ok


def test_the_normalized_payload_hash_ignores_key_order_only() -> None:
    """Le hash doit être stable sur l'ORDRE des clés (sinon deux sérialisations équivalentes
    donneraient deux approbations différentes) et sensible à tout le reste.
    """
    payload = dict(approved().intent.normalized_payload())
    shuffled = dict(reversed(list(payload.items())))
    assert payload_hash(shuffled) == payload_hash(payload)
    assert payload_hash({**payload, "contracts": "10.0"}) != payload_hash(payload)


def test_a_reduce_decision_only_authorizes_the_reduced_size() -> None:
    """Une décision REDUCE autorise UNE taille : la taille réduite. Envoyer la taille demandée
    reviendrait à ignorer la réduction tout en se réclamant de l'approbation qui l'a imposée.
    """
    requested = intent(contracts="10")
    allowed = requested.model_copy(update={"contracts": Decimal("4")})
    reduce_decision = decision(
        requested,
        action=RiskAction.REDUCE,
        allowed_contracts=Decimal("4"),
        allowed_hash=allowed.payload_hash(),
    )
    order = ApprovedOrder(intent=allowed, decision=reduce_decision, payload_hash=allowed.payload_hash())

    assert recheck_at_send(order, send_context()).ok

    oversized = dict(requested.normalized_payload())
    result = recheck_at_send(order, send_context(), payload=oversized)
    assert not result.ok
    assert ReasonCode.PAYLOAD_HASH_MISMATCH.value in result.reason_codes


def test_a_changed_context_demands_a_new_risk_evaluation() -> None:
    """Les versions de positions et de limites sont dans l'approbation pour une raison : une
    approbation calculée sur d'autres positions ou d'autres limites n'est pas une approbation. Le
    recontrôle doit exiger une NOUVELLE évaluation, pas seulement refuser — sinon l'ordre serait
    abandonné alors qu'il est peut-être encore justifié.
    """
    order = approved()

    moved = recheck_at_send(order, send_context(position_version="pos-2"))
    assert not moved.ok
    assert ReasonCode.POSITION_VERSION_CHANGED.value in moved.reason_codes
    assert moved.requires_reevaluation

    retuned = recheck_at_send(order, send_context(limits_version="limits-2"))
    assert not retuned.ok
    assert ReasonCode.LIMITS_VERSION_CHANGED.value in retuned.reason_codes
    assert retuned.requires_reevaluation


def test_a_halt_or_a_pending_reconciliation_stops_an_entry_and_lets_a_reduction_out() -> None:
    """C'est ici que « les réductions restent permises » se vérifie vraiment : un halt ou une
    réconciliation en attente bloquent les ENTRÉES et laissent sortir. Bloquer aussi les réductions
    enfermerait le compte dans son risque au moment où il faut le réduire ; laisser passer les
    entrées annulerait le halt.
    """
    entry = approved(client_order_id="cid-entry")
    exit_order = approved(client_order_id="cid-exit", reduce_only=True)

    halted = recheck_at_send(entry, send_context(halt_level=HaltLevel.SOFT_HALT))
    assert not halted.ok
    assert ReasonCode.HALT_LEVEL_CHANGED.value in halted.reason_codes
    assert ReasonCode.SOFT_HALT.value in halted.reason_codes
    assert recheck_at_send(exit_order, send_context(halt_level=HaltLevel.SOFT_HALT)).ok

    pending = recheck_at_send(entry, send_context(reconciliation_ok=False))
    assert not pending.ok
    assert ReasonCode.RECONCILIATION_PENDING.value in pending.reason_codes
    assert recheck_at_send(exit_order, send_context(reconciliation_ok=False)).ok


def test_an_inactive_reservation_invalidates_the_send() -> None:
    """Une approbation sans réservation active n'a plus de budget derrière elle : son exposition a
    été rendue à la comptabilité. Envoyer l'ordre créerait une exposition non réservée, donc non
    comptée par les contrôles suivants.
    """
    store = InMemoryReservationStore()
    reservation = store.create(
        Reservation(
            reservation_id="rsv-1",
            account_scope=SCOPE,
            intent_id="int-cid-1",
            inst_id=INST,
            signed_contracts=Decimal("10"),
            notional_usdt=Decimal("10000"),
            created_at=T0,
        )
    )
    order_intent = intent()
    order = ApprovedOrder(
        intent=order_intent,
        decision=decision(order_intent, reservations={INST: reservation.reservation_id}),
        payload_hash=order_intent.payload_hash(),
    )

    assert recheck_at_send(order, send_context(), reservations=store).ok

    store.release(reservation.reservation_id, final_state=OrderState.CANCELED, released_at=T0, reason="test")
    result = recheck_at_send(order, send_context(), reservations=store)
    assert not result.ok
    assert ReasonCode.RESERVATION_INACTIVE.value in result.reason_codes
    assert result.requires_reevaluation


def test_a_reservation_is_released_only_on_an_observed_final_state() -> None:
    """Une demande d'annulation n'est pas une annulation, et ``UNKNOWN`` n'est pas un rejet. Libérer
    sur ces états rendrait du budget pour une exposition qui existe peut-être encore — et le refus de
    libérer doit être SANS EFFET : la réservation reste ACTIVE.
    """
    store = InMemoryReservationStore()
    reservation = store.create(
        Reservation(
            reservation_id="rsv-1",
            account_scope=SCOPE,
            intent_id="int-cid-1",
            inst_id=INST,
            signed_contracts=Decimal("10"),
            notional_usdt=Decimal("10000"),
            created_at=T0,
        )
    )

    for non_final in (OrderState.CANCEL_REQUESTED, OrderState.UNKNOWN, OrderState.ACKNOWLEDGED):
        with pytest.raises(OrderStateError) as err:
            store.release(
                reservation.reservation_id, final_state=non_final, released_at=T0, reason="trop tôt"
            )
        assert err.value.code == ReasonCode.RESERVATION_INACTIVE.value
        assert store.get(reservation.reservation_id) is not None
        assert store.get(reservation.reservation_id).is_active, "la réservation a été libérée à tort"  # type: ignore[union-attr]

    released = store.release(
        reservation.reservation_id, final_state=OrderState.FILLED, released_at=T0, reason="observé"
    )
    assert not released.is_active
    assert store.active(SCOPE) == []
    # Libération idempotente et MONOTONE : une réservation libérée ne redevient jamais active.
    again = store.release(
        reservation.reservation_id, final_state=OrderState.CANCELED, released_at=T0, reason="rejeu"
    )
    assert not again.is_active
    assert again.release_reason == released.release_reason


# --- T46 : deux gateways concurrents ------------------------------------------------------------------


async def test_T46_only_one_of_two_concurrent_gateways_holds_a_send_path() -> None:
    """T46 : deux gateways sur la MÊME base. Un seul obtient le bail ; l'autre n'a aucun chemin
    d'envoi.

    Si les deux envoyaient, l'exposition serait doublée sans qu'aucune limite ne soit franchie du
    point de vue de chaque instance : chacune verrait son propre ordre comme unique. C'est la panne
    la plus coûteuse de tout le système, et la moins visible.
    """
    engine = memory_engine()
    uow = UnitOfWorkFactory(engine)
    clock = SimulatedClock(T0)
    first = build_rig(clock=clock, uow=uow, holder_id="gateway-a")
    second = build_rig(clock=clock, uow=uow, holder_id="gateway-b")

    assert first.lease.try_acquire() is True
    assert second.lease.try_acquire() is False
    assert first.lease.state is LeadershipState.LEADER
    assert second.lease.state is LeadershipState.FOLLOWER
    assert second.lease.is_leader is False

    # Le suiveur n'a pas de chemin d'envoi : sa soumission échoue AVANT tout réseau.
    with pytest.raises(LeadershipError):
        await second.gateway.submit(approved(client_order_id="cid-follower"))
    assert second.adapter.requests == []

    # Le leader, lui, envoie une fois et une seule.
    staged = first.gateway.stage(approved(client_order_id="cid-leader"))
    result = await first.gateway.dispatch_order(staged.order_id)
    assert result.outcome is SubmissionOutcome.ACK
    assert len(first.adapter.requests) == 1

    # Et un second dispatch du même ordre n'envoie RIEN : même le leader légitime ne peut pas
    # produire de doublon, car l'événement d'outbox est clôturé et la tentative déjà comptée.
    again = await first.gateway.dispatch_order(staged.order_id)
    assert again.outcome is SubmissionOutcome.NOT_SENT
    assert len(first.adapter.requests) == 1


async def test_T46_a_follower_that_takes_over_gets_a_strictly_higher_fencing_token() -> None:
    """Le jeton de cloisonnement n'augmente qu'à une NOUVELLE acquisition. C'est ce qui permet à la
    base de reconnaître un ancien titulaire : sans jeton monotone, un processus revenu à la vie après
    une pause serait indistinguable du titulaire légitime.
    """
    uow = UnitOfWorkFactory(memory_engine())
    clock = SimulatedClock(T0)
    first = LeaseManager(uow, holder_id="a", clock=clock, ttl_ms=4_000, heartbeat_ms=1_000)
    second = LeaseManager(uow, holder_id="b", clock=clock, ttl_ms=4_000, heartbeat_ms=1_000)

    assert first.try_acquire()
    first_token = first.fencing_token
    assert first_token is not None

    # Renouvellement par le même titulaire : le jeton NE change pas (sinon il ne fencerait rien).
    clock.advance(timedelta(seconds=1))
    assert first.heartbeat()
    assert first.fencing_token == first_token

    # Bail expiré : le second prend la main avec un jeton strictement supérieur.
    clock.advance(timedelta(seconds=5))
    assert second.try_acquire()
    assert second.fencing_token is not None
    assert second.fencing_token > first_token


# --- T47 : perte du bail ou de la base ----------------------------------------------------------------


async def test_T47_a_former_writer_whose_lease_was_stolen_can_no_longer_send() -> None:
    """T47 : le bail est passé à une autre instance pendant que l'ancien titulaire se croyait encore
    leader (son expiration LOCALE n'est pas atteinte). C'est le split-brain classique.

    La vérité est en base, relue DANS la transaction d'envoi : l'ancien titulaire doit devenir
    incapable d'envoyer, donc incapable d'augmenter le risque (§52.3). Rien ne part sur le réseau.
    """
    uow = UnitOfWorkFactory(memory_engine())
    clock = SimulatedClock(T0)
    rig = build_rig(clock=clock, uow=uow, holder_id="gateway-a", lease_ttl_ms=60_000)
    assert rig.lease.try_acquire()
    assert rig.lease.is_leader

    # Une autre instance prend le bail en base : l'ancien titulaire l'ignore encore localement.
    with uow.transaction() as tx:
        row = tx.leases.read(DEFAULT_LEASE_NAME)
        assert row is not None
        row.holder = "gateway-b"
        row.fencing_token = row.fencing_token + 1
    assert rig.lease.is_leader, "l'ancien titulaire se croit encore leader : c'est le cas à couvrir"

    with pytest.raises(LeadershipError):
        await rig.gateway.submit(approved(client_order_id="cid-stale-writer"))

    assert rig.adapter.requests == [], "l'ancien writer a envoyé un ordre"
    assert rig.lease.state is LeadershipState.LOST


async def test_T47_losing_the_database_makes_the_writer_stand_down_instead_of_guessing() -> None:
    """T47, perte de base : sans base, on ne peut pas confirmer l'autorité d'envoi. §52.3 impose de
    suspendre les prises de risque. Continuer « parce que le bail était valide il y a deux secondes »
    est précisément le raisonnement qui produit deux writers actifs.
    """
    inner = UnitOfWorkFactory(memory_engine())
    flaky = FlakyUowFactory(inner)
    clock = SimulatedClock(T0)
    lease = LeaseManager(
        flaky,  # type: ignore[arg-type]
        holder_id="gateway-a",
        clock=clock,
        ttl_ms=10_000,
        heartbeat_ms=2_000,
    )
    rig = build_rig(clock=clock, uow=inner, holder_id="gateway-a", lease=lease)
    assert lease.try_acquire()
    staged = rig.gateway.stage(approved(client_order_id="cid-db-loss"))

    flaky.broken = True
    assert lease.heartbeat() is False
    assert lease.state is LeadershipState.LOST
    assert lease.is_leader is False

    flaky.broken = False  # la base revient, mais le bail n'a PAS été réacquis
    with pytest.raises(LeadershipError):
        await rig.gateway.dispatch_order(staged.order_id)
    assert rig.adapter.requests == []

    # Contre-épreuve : après une réacquisition explicite du bail, l'envoi redevient possible.
    assert lease.try_acquire()
    result = await rig.gateway.dispatch_order(staged.order_id)
    assert result.outcome is SubmissionOutcome.ACK
    assert len(rig.adapter.requests) == 1


def test_T47_an_unreachable_database_refuses_the_send_at_the_assertion_point() -> None:
    """``assert_leader`` est le point unique de vérification. Une base injoignable y devient un REFUS
    explicite, pas une exception qui remonterait ailleurs ni un ``True`` optimiste.
    """
    uow = UnitOfWorkFactory(memory_engine())
    clock = SimulatedClock(T0)
    lease = LeaseManager(uow, holder_id="a", clock=clock, ttl_ms=10_000, heartbeat_ms=2_000)
    assert lease.try_acquire()

    with pytest.raises(LeadershipError):
        lease.assert_leader(UowWithUnreachableLeases())  # type: ignore[arg-type]
    assert lease.state is LeadershipState.LOST


def test_T47_a_locally_expired_lease_refuses_the_send_without_asking_the_database() -> None:
    """Un bail expiré localement est déjà une raison suffisante : inutile d'interroger la base pour
    savoir qu'on n'a plus l'autorité. Le contraire — attendre le verdict du réseau — laisserait une
    fenêtre d'envoi pendant une coupure.
    """
    uow = UnitOfWorkFactory(memory_engine())
    clock = SimulatedClock(T0)
    lease = LeaseManager(uow, holder_id="a", clock=clock, ttl_ms=4_000, heartbeat_ms=1_000)
    assert lease.try_acquire()

    with uow.transaction() as tx:
        assert lease.assert_leader(tx) == lease.fencing_token  # frais : autorisé

    clock.advance(timedelta(seconds=5))
    with uow.transaction() as tx:
        with pytest.raises(LeadershipError):
            lease.assert_leader(tx)
    assert lease.state is LeadershipState.LOST


def test_T47_a_broken_database_never_promotes_anyone_to_leader() -> None:
    """On ne devient pas leader sur une supposition : si la base est injoignable à l'acquisition, le
    résultat est « pas leader », jamais « probablement leader ».
    """

    class BrokenFactory:
        @contextmanager
        def transaction(self) -> Iterator[Any]:
            raise RuntimeError("base injoignable")
            yield  # pragma: no cover

    lease = LeaseManager(
        BrokenFactory(),  # type: ignore[arg-type]
        holder_id="a",
        clock=SimulatedClock(T0),
        ttl_ms=10_000,
        heartbeat_ms=2_000,
    )
    assert lease.try_acquire() is False
    assert lease.state is LeadershipState.LOST
    assert lease.is_leader is False
    assert lease.fencing_token is None


def test_a_lease_manager_refuses_a_heartbeat_slower_than_half_its_ttl() -> None:
    """Un heartbeat plus lent que la moitié du TTL garantit des expirations régulières donc des
    bascules inutiles — et pendant chaque bascule, personne n'envoie.
    """
    uow = UnitOfWorkFactory(memory_engine())
    with pytest.raises(ValueError):
        LeaseManager(uow, holder_id="a", clock=SimulatedClock(T0), ttl_ms=1_000, heartbeat_ms=900)
    # Contre-épreuve : la configuration saine est acceptée.
    assert LeaseManager(uow, holder_id="a", clock=SimulatedClock(T0), ttl_ms=1_000, heartbeat_ms=500)
