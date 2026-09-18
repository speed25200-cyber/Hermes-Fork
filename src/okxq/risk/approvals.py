"""Approbations et réservations d'exposition (§25, §53, T48, T49).

- ``recheck_at_send`` : recontrôle au moment de l'envoi. Une approbation expirée est refusée (T48) ; un
  payload modifié invalide le hash (T49) ; un contexte changé (version de position, version des limites,
  niveau de halt, perte de leadership) exige une NOUVELLE validation par le Risk Engine.
- Réservations d'exposition PESSIMISTES : créées avec l'approbation, libérées UNIQUEMENT sur un état
  final observé (FILLED / CANCELED / REJECTED / EXPIRED, ou fills rapprochés par la réconciliation) —
  jamais à la seule demande d'annulation. Deux implémentations : mémoire (tests) et SQL
  (``execution_reservations``). La libération est monotone : une réservation libérée ne redevient
  jamais active.
- Propriété d'impossibilité d'un ordre sans approbation : le seul objet que le gateway accepte est une
  ``SendAuthorization``, constructible uniquement par ``authorize_send`` à partir d'un ``ApprovedOrder``
  valide et d'un recontrôle réussi.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import ApprovalError, OrderStateError
from okxq.domain.events import ApprovedOrder, OrderIntent, RiskAction, RiskDecision
from okxq.domain.ids import new_id, payload_hash
from okxq.domain.money import ZERO, Side, dec
from okxq.domain.orders import OrderState, is_terminal
from okxq.domain.reasons import ReasonCode
from okxq.persistence.models import ExecutionReservation
from okxq.risk.kill_switch import HaltLevel


class ReservationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    account_scope: str
    intent_id: str
    inst_id: str
    signed_contracts: Decimal
    notional_usdt: Decimal
    created_at: datetime
    pessimistic: bool = True
    reduce_only: bool = False
    status: ReservationStatus = ReservationStatus.ACTIVE
    released_at: datetime | None = None
    release_reason: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status is ReservationStatus.ACTIVE


RELEASABLE_STATES: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}
)


def assert_releasable(final_state: OrderState) -> None:
    """Seul un état FINAL observé libère ; CANCEL_REQUESTED, UNKNOWN, etc. ne libèrent rien."""
    if final_state not in RELEASABLE_STATES or not is_terminal(final_state):
        raise OrderStateError(
            "libération de réservation refusée : l'état n'est pas final observé",
            code=ReasonCode.RESERVATION_INACTIVE.value,
            state=final_state.value,
        )


@runtime_checkable
class ReservationStore(Protocol):
    def create(self, reservation: Reservation) -> Reservation: ...

    def get(self, reservation_id: str) -> Reservation | None: ...

    def active(self, account_scope: str) -> list[Reservation]: ...

    def release(
        self, reservation_id: str, *, final_state: OrderState, released_at: datetime, reason: str
    ) -> Reservation: ...


class InMemoryReservationStore:
    def __init__(self) -> None:
        self._rows: dict[str, Reservation] = {}

    def create(self, reservation: Reservation) -> Reservation:
        if reservation.reservation_id in self._rows:
            raise ApprovalError("réservation déjà existante", reservation_id=reservation.reservation_id)
        self._rows[reservation.reservation_id] = reservation
        return reservation

    def get(self, reservation_id: str) -> Reservation | None:
        return self._rows.get(reservation_id)

    def active(self, account_scope: str) -> list[Reservation]:
        return [r for r in self._rows.values() if r.account_scope == account_scope and r.is_active]

    def release(
        self, reservation_id: str, *, final_state: OrderState, released_at: datetime, reason: str
    ) -> Reservation:
        assert_releasable(final_state)
        current = self._rows.get(reservation_id)
        if current is None:
            raise ApprovalError("réservation inconnue", reservation_id=reservation_id)
        if not current.is_active:
            return current  # idempotent, jamais de réactivation
        released = replace(
            current,
            status=ReservationStatus.RELEASED,
            released_at=ensure_utc(released_at),
            release_reason=f"{final_state.value}:{reason}"[:64],
        )
        self._rows[reservation_id] = released
        return released


class SqlReservationStore:
    """Persistance dans ``execution_reservations``."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def create(self, reservation: Reservation) -> Reservation:
        with self._factory() as session:
            if session.get(ExecutionReservation, reservation.reservation_id) is not None:
                raise ApprovalError("réservation déjà existante", reservation_id=reservation.reservation_id)
            session.add(
                ExecutionReservation(
                    reservation_id=reservation.reservation_id,
                    account_scope=reservation.account_scope,
                    intent_id=reservation.intent_id,
                    inst_id=reservation.inst_id,
                    signed_contracts=reservation.signed_contracts,
                    notional_usdt=reservation.notional_usdt,
                    pessimistic=reservation.pessimistic,
                    status=reservation.status.value,
                    created_at=reservation.created_at,
                    released_at=None,
                    release_reason=None,
                )
            )
            session.commit()
        return reservation

    def get(self, reservation_id: str) -> Reservation | None:
        with self._factory() as session:
            row = session.get(ExecutionReservation, reservation_id)
            return None if row is None else _from_row(row)

    def active(self, account_scope: str) -> list[Reservation]:
        with self._factory() as session:
            stmt = select(ExecutionReservation).where(
                ExecutionReservation.account_scope == account_scope,
                ExecutionReservation.status == ReservationStatus.ACTIVE.value,
            )
            return [_from_row(r) for r in session.scalars(stmt)]

    def release(
        self, reservation_id: str, *, final_state: OrderState, released_at: datetime, reason: str
    ) -> Reservation:
        assert_releasable(final_state)
        with self._factory() as session:
            row = session.get(ExecutionReservation, reservation_id)
            if row is None:
                raise ApprovalError("réservation inconnue", reservation_id=reservation_id)
            if row.status == ReservationStatus.ACTIVE.value:
                row.status = ReservationStatus.RELEASED.value
                row.released_at = ensure_utc(released_at)
                row.release_reason = f"{final_state.value}:{reason}"[:64]
                session.commit()
            return _from_row(row)


def _from_row(row: ExecutionReservation) -> Reservation:
    return Reservation(
        reservation_id=row.reservation_id,
        account_scope=row.account_scope,
        intent_id=row.intent_id,
        inst_id=row.inst_id,
        signed_contracts=row.signed_contracts,
        notional_usdt=row.notional_usdt,
        created_at=row.created_at,
        pessimistic=row.pessimistic,
        status=ReservationStatus(row.status),
        released_at=row.released_at,
        release_reason=row.release_reason,
    )


def reservation_for(
    intent: OrderIntent, *, contracts: Decimal, notional_usdt: Decimal, now: datetime
) -> Reservation:
    signed = dec(contracts) if intent.side is Side.BUY else -dec(contracts)
    return Reservation(
        reservation_id=new_id("rsv"),
        account_scope=intent.account_scope,
        intent_id=intent.intent_id,
        inst_id=intent.inst_id,
        signed_contracts=signed,
        notional_usdt=max(dec(notional_usdt), ZERO),
        created_at=ensure_utc(now),
        pessimistic=True,
        reduce_only=intent.reduce_only,
    )


# --- recontrôle à l'envoi -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SendContext:
    now: datetime
    position_version: str
    limits_version: str
    halt_level: HaltLevel
    is_leader: bool = True
    reconciliation_ok: bool = True


@dataclass(frozen=True, slots=True)
class RecheckResult:
    ok: bool
    reason_codes: list[str] = field(default_factory=list)
    requires_reevaluation: bool = False


def recheck_at_send(
    approved: ApprovedOrder,
    context_now: SendContext,
    *,
    payload: Mapping[str, Any] | None = None,
    reservations: ReservationStore | None = None,
) -> RecheckResult:
    """Recontrôle immédiatement avant l'envoi. ``payload`` est le dictionnaire EXACT que le gateway va
    envoyer (par défaut ``approved.intent.normalized_payload()``)."""
    reasons: list[str] = []
    reevaluate = False
    now = ensure_utc(context_now.now)
    decision = approved.decision
    intent = approved.intent
    if decision.action not in (RiskAction.ALLOW, RiskAction.REDUCE):
        reasons.append(ReasonCode.NO_APPROVAL.value)
    if now >= decision.expires_at:
        reasons.append(ReasonCode.APPROVAL_EXPIRED.value)
    if now >= intent.expires_at:
        reasons.append(ReasonCode.INTENT_EXPIRED.value)
    sent = dict(payload) if payload is not None else intent.normalized_payload()
    if (
        payload_hash(sent) != decision.allowed_payload_hash
        or approved.payload_hash != decision.allowed_payload_hash
    ):
        reasons.append(ReasonCode.PAYLOAD_HASH_MISMATCH.value)
    if decision.action is RiskAction.REDUCE and decision.allowed_contracts is not None:
        if dec(str(sent.get("contracts", "0"))) != decision.allowed_contracts:
            reasons.append(ReasonCode.PAYLOAD_HASH_MISMATCH.value)
    if context_now.position_version != decision.position_version:
        reasons.append(ReasonCode.POSITION_VERSION_CHANGED.value)
        reevaluate = True
    if context_now.limits_version != decision.limits_version:
        reasons.append(ReasonCode.LIMITS_VERSION_CHANGED.value)
        reevaluate = True
    if context_now.halt_level.blocks_increases and not intent.reduce_only:
        reasons.append(ReasonCode.HALT_LEVEL_CHANGED.value)
        reasons.append(context_now.halt_level.reason_code.value)
        reevaluate = True
    if not context_now.is_leader:
        reasons.append(ReasonCode.NOT_LEADER.value)
    if not context_now.reconciliation_ok and not intent.reduce_only:
        reasons.append(ReasonCode.RECONCILIATION_PENDING.value)
        reevaluate = True
    if reservations is not None:
        for rid in decision.reservations.values():
            r = reservations.get(rid)
            if r is None or not r.is_active:
                reasons.append(ReasonCode.RESERVATION_INACTIVE.value)
                reevaluate = True
                break
    return RecheckResult(ok=not reasons, reason_codes=_dedupe(reasons), requires_reevaluation=reevaluate)


def _dedupe(codes: list[str]) -> list[str]:
    out: list[str] = []
    for c in codes:
        if c not in out:
            out.append(c)
    return out


# --- autorisation d'envoi -----------------------------------------------------------------------------

_SEND_TOKEN = object()


@dataclass(frozen=True, slots=True)
class SendAuthorization:
    """Le SEUL objet accepté par le gateway. Ne se construit que via ``authorize_send``."""

    approved: ApprovedOrder
    payload: dict[str, Any]
    payload_hash: str
    authorized_at: datetime
    _token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _SEND_TOKEN:
            raise ApprovalError(
                "SendAuthorization ne se construit que par authorize_send", code=ReasonCode.NO_APPROVAL.value
            )


def authorize_send(
    approved: ApprovedOrder,
    context_now: SendContext,
    *,
    payload: Mapping[str, Any] | None = None,
    reservations: ReservationStore | None = None,
) -> SendAuthorization:
    """Lève ``ApprovalError`` si le recontrôle échoue ; sinon rend l'autorisation liée au payload exact."""
    result = recheck_at_send(approved, context_now, payload=payload, reservations=reservations)
    if not result.ok:
        raise ApprovalError(
            "envoi refusé au recontrôle",
            code=result.reason_codes[0],
            reason_codes=result.reason_codes,
            requires_reevaluation=result.requires_reevaluation,
        )
    sent = dict(payload) if payload is not None else approved.intent.normalized_payload()
    return SendAuthorization(
        approved=approved,
        payload=sent,
        payload_hash=payload_hash(sent),
        authorized_at=ensure_utc(context_now.now),
        _token=_SEND_TOKEN,
    )


def bind_approval(intent: OrderIntent, decision: RiskDecision) -> ApprovedOrder:
    """Construit l'``ApprovedOrder`` (le contrat vérifie lui-même action, intention et hash)."""
    try:
        return ApprovedOrder(intent=intent, decision=decision, payload_hash=intent.payload_hash())
    except ValueError as exc:
        raise ApprovalError(str(exc), code=ReasonCode.NO_APPROVAL.value) from exc
