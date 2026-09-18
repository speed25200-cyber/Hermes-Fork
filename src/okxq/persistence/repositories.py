"""Dépôts transactionnels typés sur le schéma §44.

Principes (AGENTS.md §5) :
- chaque dépôt travaille sur une ``Session`` fournie par l'appelant : la transaction est délimitée par
  ``UnitOfWork`` et englobe écriture métier + outbox ;
- unicité durable de ``(account_scope, client_order_id)`` : une intention ou un ordre qui réutilise un
  identifiant, terminal ou non, est refusé (T36) ;
- fills dédupliqués par ``execution_key`` (§ ``build_execution_key``) ; événements d'ordre dédupliqués par
  ``(account_scope, client_order_id, raw_hash)`` (T34) ;
- projections à version optimiste : ``orders.version`` n'est mis à jour que si la version attendue est
  encore celle de la base — aucune mise à jour aveugle n'écrase un fill tardif ;
- consommation idempotente : ``consumer_offsets`` n'avance que vers l'avant.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, Engine, Result, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import IdempotencyError, OrderStateError
from okxq.domain.events import ApprovedOrder, Fill, OrderEvent, RiskDecision
from okxq.domain.ids import payload_hash
from okxq.domain.money import ZERO
from okxq.domain.orders import OrderState, PendingOperation, is_terminal
from okxq.persistence.db import make_session_factory
from okxq.persistence.models import (
    AccountSnapshot,
    ConsumerOffset,
    Decision,
    ExecutionReservation,
    FillRow,
    OperatorAction,
    OrderEventRow,
    OrderIntentRow,
    OrderRow,
    OutboxEvent,
    PortfolioTargetRow,
    PositionSnapshot,
    RiskApproval,
    RiskState,
    RuntimeLease,
)
from okxq.persistence.types import check_numeric_bounds

__all__ = [
    "SYSTEM_KEY_PREFIX",
    "ApprovalRepository",
    "ConsumerOffsetRepository",
    "DecisionRepository",
    "FillRepository",
    "IntentRepository",
    "LeaseRepository",
    "OperatorActionRepository",
    "OrderEventRepository",
    "OrderRepository",
    "OutboxRepository",
    "ReservationRepository",
    "RiskStateRepository",
    "SnapshotRepository",
    "StaleVersionError",
    "UnitOfWork",
    "build_execution_key",
    "system_execution_key",
]

SYSTEM_KEY_PREFIX = "sys:"


def _affected(result: Result[Any]) -> int:
    """Nombre de lignes touchées par un UPDATE (typage SQLAlchemy : Result -> CursorResult)."""
    return int(cast(CursorResult[Any], result).rowcount)


class StaleVersionError(OrderStateError):
    """La version optimiste attendue n'est plus celle de la base : relire, puis réappliquer."""

    code = "OPTIMISTIC_VERSION_STALE"


# --- clés d'exécution ----------------------------------------------------------------------------------


def build_execution_key(
    account_scope: str, inst_id: str, exchange_order_id: str | None, trade_id: str | None
) -> str:
    """Clé de déduplication des fills selon la portée RÉELLE des identifiants OKX.

    ``tradeId`` est unique par instrument et ``ordId`` par compte : la clé combine donc compte, instrument,
    ordre et trade. Sans ``ordId``/``tradeId`` (événement système : liquidation, ADL, règlement), utiliser
    :func:`system_execution_key`, jamais une clé vide.
    """
    if not account_scope or not inst_id:
        raise ValueError("account_scope et inst_id sont requis pour une clé d'exécution")
    if not exchange_order_id or not trade_id:
        raise ValueError("exchange_order_id et trade_id requis ; sinon utiliser system_execution_key")
    return f"{account_scope}:{inst_id}:{exchange_order_id}:{trade_id}"


def system_execution_key(account_scope: str, inst_id: str, kind: str, payload: Mapping[str, Any]) -> str:
    """Clé ``sys:`` pour un événement système atypique, dérivée du hachage canonique du payload brut."""
    if not account_scope or not inst_id or not kind:
        raise ValueError("account_scope, inst_id et kind sont requis")
    return f"{SYSTEM_KEY_PREFIX}{account_scope}:{inst_id}:{kind}:{payload_hash(dict(payload))[:32]}"


# --- unité de travail ----------------------------------------------------------------------------------


class UnitOfWork:
    """Une transaction, tous les dépôts. ``commit`` explicite ; toute exception provoque un rollback."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.decisions = DecisionRepository(session)
        self.intents = IntentRepository(session)
        self.approvals = ApprovalRepository(session)
        self.orders = OrderRepository(session)
        self.order_events = OrderEventRepository(session)
        self.fills = FillRepository(session)
        self.reservations = ReservationRepository(session)
        self.outbox = OutboxRepository(session)
        self.offsets = ConsumerOffsetRepository(session)
        self.leases = LeaseRepository(session)
        self.risk_state = RiskStateRepository(session)
        self.operator_actions = OperatorActionRepository(session)
        self.snapshots = SnapshotRepository(session)

    def flush(self) -> None:
        self.session.flush()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


class UnitOfWorkFactory:
    """Fabrique d'unités de travail liée à un moteur ; ``transaction()`` commet ou annule."""

    def __init__(self, engine: Engine) -> None:
        self._factory: sessionmaker[Session] = make_session_factory(engine)

    @contextmanager
    def transaction(self) -> Iterator[UnitOfWork]:
        session = self._factory()
        uow = UnitOfWork(session)
        try:
            yield uow
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# --- décisions / intentions / approbations -------------------------------------------------------------


class DecisionRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def ensure(
        self,
        decision_id: str,
        *,
        mode: str,
        snapshot_id: str,
        cutoff_at: datetime,
        started_at: datetime,
        outcome: str = "TRADE",
    ) -> Decision:
        row = self._s.get(Decision, decision_id)
        if row is not None:
            return row
        row = Decision(
            decision_id=decision_id,
            mode=mode,
            snapshot_id=snapshot_id,
            cutoff_at=ensure_utc(cutoff_at),
            started_at=ensure_utc(started_at),
            outcome=outcome,
        )
        self._s.add(row)
        self._s.flush()
        return row


class IntentRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, intent_id: str) -> OrderIntentRow | None:
        return self._s.get(OrderIntentRow, intent_id)

    def by_client_order_id(self, account_scope: str, client_order_id: str) -> OrderIntentRow | None:
        stmt = select(OrderIntentRow).where(
            OrderIntentRow.account_scope == account_scope,
            OrderIntentRow.client_order_id == client_order_id,
        )
        return self._s.execute(stmt).scalar_one_or_none()

    def insert(self, approved: ApprovedOrder) -> OrderIntentRow:
        """Insère l'intention ; refuse toute réutilisation de ``client_order_id`` dans la portée (T36)."""
        intent = approved.intent
        existing_order = self._s.execute(
            select(OrderRow).where(
                OrderRow.account_scope == intent.account_scope,
                OrderRow.client_order_id == intent.client_order_id,
            )
        ).scalar_one_or_none()
        if existing_order is not None:
            state = OrderState(existing_order.observed_state)
            raise IdempotencyError(
                "client_order_id déjà utilisé"
                + (" par un ordre en état terminal : réutilisation refusée" if is_terminal(state) else ""),
                account_scope=intent.account_scope,
                client_order_id=intent.client_order_id,
                observed_state=state.value,
            )
        if self.by_client_order_id(intent.account_scope, intent.client_order_id) is not None:
            raise IdempotencyError(
                "client_order_id déjà réservé par une intention",
                account_scope=intent.account_scope,
                client_order_id=intent.client_order_id,
            )
        target_id = (
            intent.target_id if self._s.get(PortfolioTargetRow, intent.target_id) is not None else None
        )
        row = OrderIntentRow(
            intent_id=intent.intent_id,
            decision_id=intent.decision_id,
            target_id=target_id,
            account_scope=intent.account_scope,
            inst_id=intent.inst_id,
            side=intent.side.value,
            contracts=check_numeric_bounds(intent.contracts, kind="qty"),
            price_limit=None
            if intent.price_limit is None
            else check_numeric_bounds(intent.price_limit, kind="price"),
            order_type=intent.order_type.value,
            reduce_only=intent.reduce_only,
            ttl_ms=intent.ttl_ms,
            reason=intent.reason,
            client_order_id=intent.client_order_id,
            payload_hash=approved.payload_hash,
            created_at=intent.created_at,
            expires_at=intent.expires_at,
        )
        self._s.add(row)
        try:
            self._s.flush()
        except IntegrityError as exc:
            raise IdempotencyError(
                "client_order_id déjà utilisé (contrainte d'unicité)",
                account_scope=intent.account_scope,
                client_order_id=intent.client_order_id,
            ) from exc
        return row


class ApprovalRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def insert(self, decision: RiskDecision, *, approval_id: str) -> RiskApproval:
        if self._s.get(RiskApproval, approval_id) is not None:
            raise IdempotencyError("approval_id déjà enregistré", approval_id=approval_id)
        row = RiskApproval(
            approval_id=approval_id,
            intent_id=decision.intent_id,
            intent_hash=decision.intent_hash,
            action=decision.action.value,
            allowed_payload_hash=decision.allowed_payload_hash,
            allowed_contracts=decision.allowed_contracts,
            limits_version=decision.limits_version,
            position_version=decision.position_version,
            reservations=dict(decision.reservations),
            reason_codes=list(decision.reason_codes),
            created_at=decision.created_at,
            expires_at=decision.expires_at,
        )
        self._s.add(row)
        self._s.flush()
        return row

    def get(self, approval_id: str) -> RiskApproval | None:
        return self._s.get(RiskApproval, approval_id)


# --- ordres ----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderProjectionValues:
    """Valeurs de projection écrites de façon conditionnelle sur ``orders``."""

    observed_state: OrderState
    pending_operation: PendingOperation
    cumulative_filled: Decimal
    average_fill_price: Decimal | None
    exchange_order_id: str | None
    terminal_at: datetime | None


class OrderRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def create(
        self,
        approved: ApprovedOrder,
        *,
        order_id: str,
        approval_id: str,
        now: datetime,
        observed_state: OrderState = OrderState.RISK_APPROVED,
    ) -> OrderRow:
        intent = approved.intent
        row = OrderRow(
            order_id=order_id,
            account_scope=intent.account_scope,
            client_order_id=intent.client_order_id,
            intent_id=intent.intent_id,
            approval_id=approval_id,
            exchange_order_id=None,
            inst_id=intent.inst_id,
            side=intent.side.value,
            order_type=intent.order_type.value,
            contracts=intent.contracts,
            price_limit=intent.price_limit,
            reduce_only=intent.reduce_only,
            observed_state=observed_state.value,
            pending_operation=PendingOperation.NONE.value,
            cumulative_filled=Decimal(0),
            average_fill_price=None,
            payload_hash=approved.payload_hash,
            sent_payload=None,
            attempt_count=0,
            created_at=ensure_utc(now),
            updated_at=ensure_utc(now),
            version=0,
        )
        self._s.add(row)
        try:
            self._s.flush()
        except IntegrityError as exc:
            raise IdempotencyError(
                "ordre déjà présent pour ce client_order_id",
                account_scope=intent.account_scope,
                client_order_id=intent.client_order_id,
            ) from exc
        return row

    def get(self, order_id: str) -> OrderRow | None:
        return self._s.get(OrderRow, order_id)

    def refresh(self, row: OrderRow) -> OrderRow:
        self._s.refresh(row)
        return row

    def by_client_order_id(self, account_scope: str, client_order_id: str) -> OrderRow | None:
        stmt = select(OrderRow).where(
            OrderRow.account_scope == account_scope, OrderRow.client_order_id == client_order_id
        )
        return self._s.execute(stmt).scalar_one_or_none()

    def by_intent(self, intent_id: str) -> OrderRow | None:
        return self._s.execute(select(OrderRow).where(OrderRow.intent_id == intent_id)).scalar_one_or_none()

    def in_states(self, account_scope: str, states: set[OrderState]) -> list[OrderRow]:
        stmt = (
            select(OrderRow)
            .where(
                OrderRow.account_scope == account_scope,
                OrderRow.observed_state.in_([s.value for s in states]),
            )
            .order_by(OrderRow.created_at)
        )
        return list(self._s.execute(stmt).scalars())

    def open_orders(self, account_scope: str) -> list[OrderRow]:
        non_terminal = {s for s in OrderState if not is_terminal(s)}
        return self.in_states(account_scope, non_terminal)

    def _conditional_update(self, order_id: str, expected_version: int, values: dict[str, Any]) -> None:
        stmt = (
            update(OrderRow)
            .where(OrderRow.order_id == order_id, OrderRow.version == expected_version)
            .values(**values, version=expected_version + 1)
            .execution_options(synchronize_session="fetch")
        )
        if _affected(self._s.execute(stmt)) != 1:
            raise StaleVersionError(
                "mise à jour refusée : version optimiste périmée",
                order_id=order_id,
                expected_version=expected_version,
            )

    def mark_attempt(
        self,
        order_id: str,
        *,
        expected_version: int,
        sent_payload: Mapping[str, Any],
        sent_at: datetime,
    ) -> None:
        """Trace la tentative et le payload EXACT avant l'envoi réseau (§52.1)."""
        row = self.get(order_id)
        if row is None:
            raise OrderStateError("ordre inconnu", order_id=order_id)
        self._conditional_update(
            order_id,
            expected_version,
            {
                "sent_payload": dict(sent_payload),
                "attempt_count": row.attempt_count + 1,
                "sent_at": ensure_utc(sent_at),
                "observed_state": OrderState.SUBMITTED.value,
                "pending_operation": PendingOperation.SUBMIT.value,
                "updated_at": ensure_utc(sent_at),
            },
        )

    def apply_projection(
        self,
        order_id: str,
        *,
        expected_version: int,
        values: OrderProjectionValues,
        now: datetime,
        ack_at: datetime | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "observed_state": values.observed_state.value,
            "pending_operation": values.pending_operation.value,
            "cumulative_filled": check_numeric_bounds(values.cumulative_filled, kind="qty"),
            "average_fill_price": values.average_fill_price,
            "exchange_order_id": values.exchange_order_id,
            "terminal_at": values.terminal_at,
            "updated_at": ensure_utc(now),
        }
        if ack_at is not None:
            payload["ack_at"] = ensure_utc(ack_at)
        self._conditional_update(order_id, expected_version, payload)


class OrderEventRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def append(
        self,
        event: OrderEvent,
        *,
        account_scope: str,
        order_id: str | None,
        raw: Mapping[str, Any] | None = None,
    ) -> bool:
        """Journalise l'événement brut. Retourne False si déjà présent (même raw_hash) : idempotent (T34)."""
        raw_hash = event.raw_hash or payload_hash(event.model_dump(mode="json", exclude={"event_id"}))
        dup = self._s.execute(
            select(OrderEventRow.event_id).where(
                OrderEventRow.account_scope == account_scope,
                OrderEventRow.client_order_id == event.client_order_id,
                OrderEventRow.raw_hash == raw_hash,
            )
        ).first()
        if dup is not None or self._s.get(OrderEventRow, event.event_id) is not None:
            return False
        row = OrderEventRow(
            event_id=event.event_id,
            order_id=order_id,
            account_scope=account_scope,
            client_order_id=event.client_order_id,
            exchange_order_id=event.exchange_order_id,
            event_kind=event.event_kind.value,
            observed_state=event.observed_state.value,
            cumulative_filled=event.cumulative_filled,
            event_ts=event.event_ts,
            receive_ts=event.receive_ts,
            raw_hash=raw_hash,
            raw=dict(raw or {}),
        )
        self._s.add(row)
        try:
            with self._s.begin_nested():
                self._s.flush()
        except IntegrityError:
            return False
        return True

    def for_order(self, account_scope: str, client_order_id: str) -> list[OrderEventRow]:
        stmt = (
            select(OrderEventRow)
            .where(
                OrderEventRow.account_scope == account_scope,
                OrderEventRow.client_order_id == client_order_id,
            )
            .order_by(OrderEventRow.receive_ts, OrderEventRow.event_id)
        )
        return list(self._s.execute(stmt).scalars())

    def last_timestamps(
        self,
        account_scope: str,
        client_order_id: str,
        *,
        exclude_sources: tuple[str, ...] = ("rest_response",),
    ) -> tuple[datetime | None, datetime | None]:
        """(max event_ts, max receive_ts) des événements d'exchange déjà appliqués (hors réponses locales)."""
        rows = [
            r
            for r in self.for_order(account_scope, client_order_id)
            if r.raw.get("source") not in exclude_sources
        ]
        event_ts = [r.event_ts for r in rows if r.event_ts is not None]
        receive_ts = [r.receive_ts for r in rows]
        return (max(event_ts) if event_ts else None, max(receive_ts) if receive_ts else None)

    def orphans(self, account_scope: str) -> list[OrderEventRow]:
        """Événements reçus pour un client_order_id inconnu de notre journal (à réconcilier)."""
        stmt = select(OrderEventRow).where(
            OrderEventRow.account_scope == account_scope, OrderEventRow.order_id.is_(None)
        )
        return list(self._s.execute(stmt).scalars())


class FillRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def insert(self, fill: Fill, *, order_id: str | None) -> bool:
        """Insère un fill ; False s'il existe déjà (même ``execution_key``)."""
        if self._s.get(FillRow, fill.execution_key) is not None:
            return False
        row = FillRow(
            execution_key=fill.execution_key,
            account_scope=fill.account_scope,
            order_id=order_id,
            client_order_id=fill.client_order_id,
            exchange_order_id=fill.exchange_order_id,
            trade_id=fill.trade_id,
            inst_id=fill.inst_id,
            side=fill.side.value,
            contracts=check_numeric_bounds(fill.contracts, kind="qty"),
            fill_price=check_numeric_bounds(fill.fill_price, kind="price"),
            fee_cashflow=check_numeric_bounds(fill.fee_cashflow, kind="money"),
            fee_ccy=fill.fee_ccy,
            liquidity=fill.liquidity.value,
            fill_at=fill.fill_at,
            receive_ts=fill.receive_ts,
        )
        self._s.add(row)
        try:
            with self._s.begin_nested():
                self._s.flush()
        except IntegrityError:
            return False
        return True

    def for_order(self, order_id: str) -> list[FillRow]:
        stmt = select(FillRow).where(FillRow.order_id == order_id).order_by(FillRow.fill_at)
        return list(self._s.execute(stmt).scalars())

    def since(self, account_scope: str, since: datetime) -> list[FillRow]:
        stmt = (
            select(FillRow)
            .where(FillRow.account_scope == account_scope, FillRow.fill_at >= ensure_utc(since))
            .order_by(FillRow.fill_at)
        )
        return list(self._s.execute(stmt).scalars())

    def signed_contracts_by_instrument(self, account_scope: str) -> dict[str, Decimal]:
        """Somme signée des contrats exécutés par instrument (attente locale de position)."""
        stmt = select(FillRow.inst_id, FillRow.side, func.sum(FillRow.contracts)).where(
            FillRow.account_scope == account_scope
        )
        stmt = stmt.group_by(FillRow.inst_id, FillRow.side)
        out: dict[str, Decimal] = {}
        for inst_id, side, total in self._s.execute(stmt):
            sign = Decimal(1) if side == "buy" else Decimal(-1)
            out[inst_id] = out.get(inst_id, Decimal(0)) + sign * Decimal(str(total))
        return out


class ReservationRepository:
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"

    def __init__(self, session: Session) -> None:
        self._s = session

    def create(
        self,
        *,
        reservation_id: str,
        account_scope: str,
        intent_id: str,
        inst_id: str,
        signed_contracts: Decimal,
        notional_usdt: Decimal,
        now: datetime,
        pessimistic: bool = True,
    ) -> ExecutionReservation:
        row = ExecutionReservation(
            reservation_id=reservation_id,
            account_scope=account_scope,
            intent_id=intent_id,
            inst_id=inst_id,
            signed_contracts=signed_contracts,
            notional_usdt=check_numeric_bounds(notional_usdt, kind="money"),
            pessimistic=pessimistic,
            status=self.ACTIVE,
            created_at=ensure_utc(now),
        )
        self._s.add(row)
        self._s.flush()
        return row

    def get(self, reservation_id: str) -> ExecutionReservation | None:
        return self._s.get(ExecutionReservation, reservation_id)

    def for_intent(self, intent_id: str) -> list[ExecutionReservation]:
        stmt = select(ExecutionReservation).where(ExecutionReservation.intent_id == intent_id)
        return list(self._s.execute(stmt).scalars())

    def active(self, account_scope: str) -> list[ExecutionReservation]:
        stmt = select(ExecutionReservation).where(
            ExecutionReservation.account_scope == account_scope,
            ExecutionReservation.status == self.ACTIVE,
        )
        return list(self._s.execute(stmt).scalars())

    def release_for_intent(self, intent_id: str, *, reason: str, now: datetime) -> int:
        """Libère les réservations actives d'une intention ; idempotent (0 si déjà libérées)."""
        stmt = (
            update(ExecutionReservation)
            .where(
                ExecutionReservation.intent_id == intent_id,
                ExecutionReservation.status == self.ACTIVE,
            )
            .values(status=self.RELEASED, released_at=ensure_utc(now), release_reason=reason)
            .execution_options(synchronize_session="fetch")
        )
        return _affected(self._s.execute(stmt))


# --- outbox et offsets ------------------------------------------------------------------------------------


class OutboxRepository:
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PROCESSED = "PROCESSED"
    DEAD = "DEAD"

    def __init__(self, session: Session) -> None:
        self._s = session

    def enqueue(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> OutboxEvent:
        """Ajoute un événement ; si la clé d'idempotence existe déjà, retourne l'existant sans doublon."""
        existing = self._s.execute(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = OutboxEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=dict(payload),
            idempotency_key=idempotency_key,
            created_at=ensure_utc(now),
            status=self.PENDING,
            attempts=0,
        )
        self._s.add(row)
        try:
            with self._s.begin_nested():
                self._s.flush()
        except IntegrityError:
            existing = self._s.execute(
                select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key)
            ).scalar_one()
            return existing
        return row

    def get(self, event_id: int) -> OutboxEvent | None:
        return self._s.get(OutboxEvent, event_id)

    def claimable(
        self, *, now: datetime, limit: int, event_types: set[str] | None = None
    ) -> list[OutboxEvent]:
        """PENDING, ou CLAIMED dont le bail a expiré (relivraison)."""
        now = ensure_utc(now)
        cond = or_(
            OutboxEvent.status == self.PENDING,
            and_(OutboxEvent.status == self.CLAIMED, OutboxEvent.claimed_until < now),
        )
        stmt = select(OutboxEvent).where(cond)
        if event_types:
            stmt = stmt.where(OutboxEvent.event_type.in_(sorted(event_types)))
        stmt = stmt.order_by(OutboxEvent.id).limit(limit)
        return list(self._s.execute(stmt).scalars())

    def try_claim(
        self,
        event_id: int,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        lease_seconds: float,
    ) -> bool:
        """Réclame un événement de façon atomique (mise à jour conditionnelle). False si déjà pris."""
        now = ensure_utc(now)
        cond = or_(
            OutboxEvent.status == self.PENDING,
            and_(OutboxEvent.status == self.CLAIMED, OutboxEvent.claimed_until < now),
        )
        stmt = (
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id, cond)
            .values(
                status=self.CLAIMED,
                claimed_by=worker_id,
                claimed_until=now + timedelta(seconds=lease_seconds),
                fencing_token=fencing_token,
                attempts=OutboxEvent.attempts + 1,
            )
            .execution_options(synchronize_session="fetch")
        )
        return _affected(self._s.execute(stmt)) == 1

    def complete(self, event_id: int, *, worker_id: str, fencing_token: int, now: datetime) -> bool:
        """Marque PROCESSED seulement si le bail est encore le nôtre (clôture fencée)."""
        stmt = (
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == self.CLAIMED,
                OutboxEvent.claimed_by == worker_id,
                OutboxEvent.fencing_token == fencing_token,
            )
            .values(status=self.PROCESSED, processed_at=ensure_utc(now), last_error=None)
            .execution_options(synchronize_session="fetch")
        )
        return _affected(self._s.execute(stmt)) == 1

    def fail(
        self, event_id: int, *, worker_id: str, fencing_token: int, error: str, dead: bool = False
    ) -> bool:
        stmt = (
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event_id,
                OutboxEvent.status == self.CLAIMED,
                OutboxEvent.claimed_by == worker_id,
                OutboxEvent.fencing_token == fencing_token,
            )
            .values(
                status=self.DEAD if dead else self.PENDING,
                last_error=error[:2000],
                claimed_by=None,
                claimed_until=None,
            )
            .execution_options(synchronize_session="fetch")
        )
        return _affected(self._s.execute(stmt)) == 1

    def after(self, last_event_id: int, *, limit: int, statuses: set[str] | None = None) -> list[OutboxEvent]:
        stmt = select(OutboxEvent).where(OutboxEvent.id > last_event_id)
        if statuses:
            stmt = stmt.where(OutboxEvent.status.in_(sorted(statuses)))
        return list(self._s.execute(stmt.order_by(OutboxEvent.id).limit(limit)).scalars())


class ConsumerOffsetRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, consumer_name: str) -> int:
        row = self._s.get(ConsumerOffset, consumer_name)
        return 0 if row is None else row.last_event_id

    def advance(self, consumer_name: str, event_id: int, *, now: datetime) -> bool:
        """N'avance que vers l'avant : une relivraison d'un identifiant déjà consommé retourne False."""
        row = self._s.get(ConsumerOffset, consumer_name)
        if row is None:
            self._s.add(
                ConsumerOffset(
                    consumer_name=consumer_name, last_event_id=event_id, updated_at=ensure_utc(now)
                )
            )
            self._s.flush()
            return True
        stmt = (
            update(ConsumerOffset)
            .where(ConsumerOffset.consumer_name == consumer_name, ConsumerOffset.last_event_id < event_id)
            .values(last_event_id=event_id, updated_at=ensure_utc(now))
            .execution_options(synchronize_session="fetch")
        )
        return _affected(self._s.execute(stmt)) == 1


# --- bail d'exécution ----------------------------------------------------------------------------------


class LeaseRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def read(self, lease_name: str) -> RuntimeLease | None:
        return self._s.get(RuntimeLease, lease_name)

    def ensure_row(self, lease_name: str) -> RuntimeLease:
        row = self._s.get(RuntimeLease, lease_name)
        if row is None:
            row = RuntimeLease(lease_name=lease_name, holder=None, fencing_token=0)
            self._s.add(row)
            try:
                with self._s.begin_nested():
                    self._s.flush()
            except IntegrityError:
                row = self._s.get(RuntimeLease, lease_name)
                assert row is not None
        return row

    def try_acquire(self, lease_name: str, *, holder: str, now: datetime, ttl_seconds: float) -> int | None:
        """Acquiert si libre, expiré ou déjà tenu par ``holder``. Retourne le token (monotone) ou None.

        Une NOUVELLE acquisition (bail libre/expiré) incrémente ``fencing_token`` ; un renouvellement par le
        même titulaire conserve son token.
        """
        now = ensure_utc(now)
        row = self.ensure_row(lease_name)
        expired = row.expires_at is None or row.expires_at <= now
        if row.holder == holder and not expired:
            return self.heartbeat(
                lease_name, holder=holder, token=row.fencing_token, now=now, ttl_seconds=ttl_seconds
            )
        if row.holder is not None and not expired:
            return None
        new_token = row.fencing_token + 1
        stmt = (
            update(RuntimeLease)
            .where(
                RuntimeLease.lease_name == lease_name,
                RuntimeLease.fencing_token == row.fencing_token,
                or_(RuntimeLease.expires_at.is_(None), RuntimeLease.expires_at <= now),
            )
            .values(
                holder=holder,
                fencing_token=new_token,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            .execution_options(synchronize_session="fetch")
        )
        if _affected(self._s.execute(stmt)) != 1:
            return None
        return new_token

    def heartbeat(
        self, lease_name: str, *, holder: str, token: int, now: datetime, ttl_seconds: float
    ) -> int | None:
        """Prolonge le bail seulement si titulaire ET token correspondent encore ; sinon None (bail perdu)."""
        now = ensure_utc(now)
        stmt = (
            update(RuntimeLease)
            .where(
                RuntimeLease.lease_name == lease_name,
                RuntimeLease.holder == holder,
                RuntimeLease.fencing_token == token,
                RuntimeLease.expires_at > now,
            )
            .values(heartbeat_at=now, expires_at=now + timedelta(seconds=ttl_seconds))
            .execution_options(synchronize_session="fetch")
        )
        return token if _affected(self._s.execute(stmt)) == 1 else None

    def release(self, lease_name: str, *, holder: str, token: int) -> bool:
        stmt = (
            update(RuntimeLease)
            .where(
                RuntimeLease.lease_name == lease_name,
                RuntimeLease.holder == holder,
                RuntimeLease.fencing_token == token,
            )
            .values(holder=None, expires_at=None, heartbeat_at=None)
            .execution_options(synchronize_session="fetch")
        )
        return _affected(self._s.execute(stmt)) == 1

    def is_current(self, lease_name: str, *, holder: str, token: int, now: datetime) -> bool:
        """Vérification au point d'envoi : le bail est tenu par ``holder`` avec ce token et non expiré."""
        row = self._s.get(RuntimeLease, lease_name)
        if row is None:
            return False
        self._s.refresh(row)
        return (
            row.holder == holder
            and row.fencing_token == token
            and row.expires_at is not None
            and row.expires_at > ensure_utc(now)
        )


# --- état de risque, actions opérateur, instantanés ----------------------------------------------------


class RiskStateRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, account_scope: str) -> RiskState | None:
        return self._s.get(RiskState, account_scope)

    def upsert(
        self, account_scope: str, *, now: datetime, expected_version: int | None = None, **values: Any
    ) -> RiskState:
        row = self._s.get(RiskState, account_scope)
        if row is None:
            row = RiskState(account_scope=account_scope, updated_at=ensure_utc(now), version=0, **values)
            self._s.add(row)
            self._s.flush()
            return row
        if expected_version is not None and row.version != expected_version:
            raise StaleVersionError("risk_state : version périmée", account_scope=account_scope)
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = ensure_utc(now)
        row.version += 1
        self._s.flush()
        return row


class OperatorActionRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def record(
        self,
        *,
        request_id: str,
        action: str,
        scope: str,
        reason: str,
        actor: str,
        role: str,
        now: datetime,
        status: str = "REQUESTED",
    ) -> OperatorAction:
        if self._s.get(OperatorAction, request_id) is not None:
            raise IdempotencyError("request_id déjà enregistré", request_id=request_id)
        row = OperatorAction(
            request_id=request_id,
            action=action,
            scope=scope,
            reason=reason,
            actor=actor,
            role=role,
            requested_at=ensure_utc(now),
            status=status,
        )
        self._s.add(row)
        self._s.flush()
        return row

    def complete(
        self, request_id: str, *, status: str, observed_result: Mapping[str, Any], now: datetime
    ) -> OperatorAction:
        row = self._s.get(OperatorAction, request_id)
        if row is None:
            raise OrderStateError("action opérateur inconnue", request_id=request_id)
        row.status = status
        row.observed_result = dict(observed_result)
        row.completed_at = ensure_utc(now)
        self._s.flush()
        return row


class SnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def record_account(
        self,
        *,
        account_scope: str,
        equity_version: str,
        as_of: datetime,
        source: str,
        cash_collateral: Decimal,
        unrealized_pnl: Decimal,
        equity: Decimal,
        available_margin: Decimal | None = None,
        used_margin: Decimal | None = None,
        external_cashflow_cum: Decimal = ZERO,
        unit_value: Decimal | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> AccountSnapshot:
        row = AccountSnapshot(
            account_scope=account_scope,
            equity_version=equity_version,
            as_of=ensure_utc(as_of),
            source=source,
            cash_collateral=check_numeric_bounds(cash_collateral, kind="money"),
            unrealized_pnl=check_numeric_bounds(unrealized_pnl, kind="money"),
            equity=check_numeric_bounds(equity, kind="money"),
            available_margin=available_margin,
            used_margin=used_margin,
            external_cashflow_cum=check_numeric_bounds(external_cashflow_cum, kind="money"),
            unit_value=unit_value,
            raw=dict(raw or {}),
        )
        self._s.add(row)
        try:
            with self._s.begin_nested():
                self._s.flush()
        except IntegrityError as exc:
            raise IdempotencyError("equity_version déjà enregistrée", equity_version=equity_version) from exc
        return row

    def record_position(
        self,
        *,
        account_scope: str,
        inst_id: str,
        as_of: datetime,
        source: str,
        signed_base_qty: Decimal,
        signed_contracts: Decimal,
        average_entry_price: Decimal,
        mark_price: Decimal | None = None,
        liquidation_price: Decimal | None = None,
        margin: Decimal | None = None,
        leverage: Decimal | None = None,
        protection: Mapping[str, Any] | None = None,
    ) -> PositionSnapshot:
        latest = self.latest_position(account_scope, inst_id, source=source)
        row = PositionSnapshot(
            account_scope=account_scope,
            inst_id=inst_id,
            as_of=ensure_utc(as_of),
            source=source,
            signed_base_qty=check_numeric_bounds(signed_base_qty, kind="qty"),
            signed_contracts=check_numeric_bounds(signed_contracts, kind="qty"),
            average_entry_price=check_numeric_bounds(average_entry_price, kind="price"),
            mark_price=mark_price,
            liquidation_price=liquidation_price,
            margin=margin,
            leverage=leverage,
            protection=dict(protection or {}),
            version=0 if latest is None else latest.version + 1,
        )
        self._s.add(row)
        self._s.flush()
        return row

    def latest_position(
        self, account_scope: str, inst_id: str, *, source: str | None = None
    ) -> PositionSnapshot | None:
        stmt = select(PositionSnapshot).where(
            PositionSnapshot.account_scope == account_scope, PositionSnapshot.inst_id == inst_id
        )
        if source is not None:
            stmt = stmt.where(PositionSnapshot.source == source)
        stmt = stmt.order_by(PositionSnapshot.as_of.desc(), PositionSnapshot.id.desc()).limit(1)
        return self._s.execute(stmt).scalar_one_or_none()

    def latest_positions(self, account_scope: str, *, source: str) -> dict[str, PositionSnapshot]:
        stmt = (
            select(PositionSnapshot)
            .where(PositionSnapshot.account_scope == account_scope, PositionSnapshot.source == source)
            .order_by(PositionSnapshot.as_of.desc(), PositionSnapshot.id.desc())
        )
        out: dict[str, PositionSnapshot] = {}
        for row in self._s.execute(stmt).scalars():
            out.setdefault(row.inst_id, row)
        return out

    def latest_account(self, account_scope: str, *, source: str | None = None) -> AccountSnapshot | None:
        stmt = select(AccountSnapshot).where(AccountSnapshot.account_scope == account_scope)
        if source is not None:
            stmt = stmt.where(AccountSnapshot.source == source)
        stmt = stmt.order_by(AccountSnapshot.as_of.desc(), AccountSnapshot.id.desc()).limit(1)
        return self._s.execute(stmt).scalar_one_or_none()
