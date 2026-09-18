"""Gateway d'exécution UNIQUE (§52.1) : le seul chemin de code qui envoie un ordre à un exchange.

Flux durable d'un ordre :

1. ``stage`` — UNE transaction : intention + approbation + ordre (RISK_APPROVED) + réservation pessimiste +
   événement d'outbox ``order.submit`` portant l'``ApprovedOrder`` exact (hash lié) ;
2. ``dispatch`` — réclamation fencée de l'outbox (bail + token), vérification du bail EN BASE, re-vérification
   de l'approbation (TTL, hash, âge de l'intention, mode, état d'urgence, ``ApprovalRechecker`` injecté) ;
3. marquage de la tentative et du payload EXACT en base, commit, PUIS un seul ``place_order`` ;
4. persistance du résultat : ACK → ACKNOWLEDGED ; REJECTED → REJECTED (réservation libérée) ; réponse
   perdue → UNKNOWN, réservation MAINTENUE, aucun retry aveugle, réconciliation exigée (T32, T33) ;
5. les événements du flux privé passent par la machine d'état et des mises à jour conditionnelles ; les
   réservations ne sont libérées que sur un état final OBSERVÉ.

Le gateway est agnostique de l'exchange : en PAPER/SHADOW il reçoit un adaptateur simulé, en DEMO/LIVE
l'adaptateur OKX. Il refuse un adaptateur réel hors DEMO/LIVE. Aucune garantie « exactly-once » n'est
possible à travers le réseau : l'envoi est « au plus une fois » par tentative, et la vérité est celle de
l'exchange, obtenue par les événements et la réconciliation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import structlog

from okxq.config.modes import Mode
from okxq.config.schema import AppConfig
from okxq.domain.clocks import Clock
from okxq.domain.errors import (
    ConfigError,
    IdempotencyError,
    LeadershipError,
    OrderStateError,
)
from okxq.domain.events import (
    ApprovedOrder,
    OrderEvent,
    OrderEventKind,
    ReconciliationReport,
    SubmissionOutcome,
    SubmissionResult,
)
from okxq.domain.ids import new_id, payload_hash
from okxq.domain.money import dec
from okxq.domain.orders import OrderState, PendingOperation
from okxq.domain.reasons import ReasonCode
from okxq.exchange.base import (
    CancelResponse,
    ExchangeAdapter,
    ExchangeEvent,
    OrderRequest,
    PlaceOutcome,
    PlaceResponse,
)
from okxq.execution.leadership import LeaseManager
from okxq.execution.outbox import ClaimedEvent, OutboxWorker
from okxq.execution.state_machine import (
    OrderProjection,
    TransitionResult,
    apply_event,
    mark_acknowledged,
    mark_cancel_requested,
    mark_rejected_locally,
    mark_unknown,
)
from okxq.persistence.models import OrderRow
from okxq.persistence.repositories import (
    OrderProjectionValues,
    StaleVersionError,
    UnitOfWork,
    UnitOfWorkFactory,
)

__all__ = [
    "SUBMIT_EVENT_TYPE",
    "ApprovalRechecker",
    "DurableExecutionGateway",
    "GatewaySettings",
    "RecheckResult",
    "ReconcilerProtocol",
    "StagedOrder",
    "projection_from_row",
]

log = structlog.get_logger(__name__)

SUBMIT_EVENT_TYPE = "order.submit"
_MAX_PERSIST_RETRIES = 3


@dataclass(frozen=True, slots=True)
class RecheckResult:
    ok: bool
    reason_codes: tuple[str, ...] = ()


@runtime_checkable
class ApprovalRechecker(Protocol):
    """Re-vérification au moment de l'envoi (T48) : contexte de risque, mode, état d'urgence.

    L'implémentation vit dans le module de risque ; le gateway ne fait que l'appeler et respecter sa réponse.
    """

    async def recheck(self, approved: ApprovedOrder, *, now: datetime) -> RecheckResult: ...


@runtime_checkable
class ReconcilerProtocol(Protocol):
    async def run(self, *, startup: bool) -> ReconciliationReport: ...


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    mode: Mode
    account_scope: str
    approval_ttl: timedelta
    max_intent_age: timedelta
    require_reconciliation_before_entries: bool
    outbox_lease_seconds: float = 30.0
    outbox_max_attempts: int = 3

    @classmethod
    def from_config(cls, cfg: AppConfig) -> GatewaySettings:
        return cls(
            mode=cfg.mode,
            account_scope=cfg.account.scope,
            approval_ttl=timedelta(milliseconds=cfg.risk.approval_ttl_ms),
            max_intent_age=timedelta(milliseconds=cfg.execution.max_order_intent_age_ms),
            require_reconciliation_before_entries=cfg.execution.require_reconciliation_before_entries,
        )


@dataclass(frozen=True, slots=True)
class StagedOrder:
    order_id: str
    intent_id: str
    client_order_id: str
    approval_id: str
    reservation_id: str
    outbox_event_id: int


def projection_from_row(
    row: OrderRow, *, last_event_ts: datetime | None = None, last_receive_ts: datetime | None = None
) -> OrderProjection:
    state = OrderState(row.observed_state)
    return OrderProjection(
        client_order_id=row.client_order_id,
        contracts=row.contracts,
        observed_state=state,
        pending_operation=PendingOperation(row.pending_operation),
        cumulative_filled=row.cumulative_filled,
        average_fill_price=row.average_fill_price,
        exchange_order_id=row.exchange_order_id,
        last_event_ts=last_event_ts,
        last_receive_ts=last_receive_ts,
        terminal_at=row.terminal_at,
        local_ack_at=row.ack_at,
        needs_reconciliation=state is OrderState.UNKNOWN,
    )


def _values_from_projection(p: OrderProjection) -> OrderProjectionValues:
    return OrderProjectionValues(
        observed_state=p.observed_state,
        pending_operation=p.pending_operation,
        cumulative_filled=p.cumulative_filled,
        average_fill_price=p.average_fill_price,
        exchange_order_id=p.exchange_order_id,
        terminal_at=p.terminal_at,
    )


class DurableExecutionGateway:
    """Implémente ``okxq.domain.protocols.ExecutionGateway`` (submit / reconcile) de façon durable."""

    def __init__(
        self,
        *,
        adapter: ExchangeAdapter,
        uow_factory: UnitOfWorkFactory,
        leadership: LeaseManager,
        rechecker: ApprovalRechecker,
        clock: Clock,
        settings: GatewaySettings,
        worker_id: str,
        reconciler: ReconcilerProtocol | None = None,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        if not settings.mode.sends_orders_to_exchange and bool(getattr(adapter, "is_real_exchange", False)):
            raise ConfigError(
                "un adaptateur d'exchange réel est interdit hors DEMO/LIVE", mode=settings.mode.value
            )
        self._adapter = adapter
        self._uow = uow_factory
        self._leadership = leadership
        self._rechecker = rechecker
        self._clock = clock
        self.settings = settings
        self.worker_id = worker_id
        self._reconciler = reconciler
        self._new_id = id_factory
        self._outbox = OutboxWorker(
            worker_id=worker_id,
            clock=clock,
            lease_seconds=settings.outbox_lease_seconds,
            max_attempts=settings.outbox_max_attempts,
        )
        self.entries_frozen = False
        self.reconciliation_required = settings.require_reconciliation_before_entries
        self.orphan_events = 0
        self.anomalies: list[dict[str, Any]] = []

    # --- cycle de vie ------------------------------------------------------------------------------------

    def attach_reconciler(self, reconciler: ReconcilerProtocol) -> None:
        self._reconciler = reconciler

    def freeze_entries(self, reason: str) -> None:
        log.warning("entries_frozen", reason=reason)
        self.entries_frozen = True

    def unfreeze_entries(self) -> None:
        self.entries_frozen = False

    def startup_check(self) -> int:
        """Au démarrage : toute tentative envoyée sans résultat persisté devient UNKNOWN ; réconcilier avant
        toute nouvelle entrée (T33). Retourne le nombre d'ordres à réconcilier."""
        now = self._clock.now_utc()
        pending = 0
        with self._uow.transaction() as uow:
            for row in uow.orders.in_states(
                self.settings.account_scope, {OrderState.SUBMITTED, OrderState.UNKNOWN}
            ):
                if row.observed_state == OrderState.SUBMITTED.value and row.attempt_count > 0:
                    p = projection_from_row(row)
                    res = mark_unknown(p, at=now)
                    self._persist(uow, row, res.projection, now=now)
                    self._journal_local(
                        uow, row, OrderEventKind.UNKNOWN, res.projection, now, reason="startup"
                    )
                pending += 1
        if pending:
            self.reconciliation_required = True
        return pending

    # --- soumission ----------------------------------------------------------------------------------------

    async def submit(self, approved: ApprovedOrder) -> SubmissionResult:
        staged = self.stage(approved)
        return await self.dispatch_order(staged.order_id)

    async def submit_many(self, approved: Sequence[ApprovedOrder]) -> list[SubmissionResult]:
        """Lot traité item par item (T37) : le résultat d'un ordre n'affecte jamais les autres."""
        results: list[SubmissionResult] = []
        for item in approved:
            try:
                results.append(await self.submit(item))
            except (IdempotencyError, OrderStateError, LeadershipError, ConfigError) as exc:
                results.append(
                    SubmissionResult(
                        intent_id=item.intent.intent_id,
                        client_order_id=item.intent.client_order_id,
                        outcome=SubmissionOutcome.NOT_SENT,
                        error_code=exc.code,
                        message=str(exc),
                    )
                )
        return results

    def stage(self, approved: ApprovedOrder) -> StagedOrder:
        """Transaction unique : intention + approbation + ordre + réservation + outbox."""
        if approved.intent.account_scope != self.settings.account_scope:
            raise ConfigError(
                "intention pour une autre portée de compte",
                expected=self.settings.account_scope,
                got=approved.intent.account_scope,
            )
        now = self._clock.now_utc()
        order_id = self._new_id("ord")
        approval_id = self._new_id("apr")
        reservation_id = self._new_id("rsv")
        intent = approved.intent
        with self._uow.transaction() as uow:
            uow.intents.insert(approved)
            uow.approvals.insert(approved.decision, approval_id=approval_id)
            uow.orders.create(approved, order_id=order_id, approval_id=approval_id, now=now)
            notional = dec(approved.decision.reservations.get("notional_usdt", "0"), field="notional_usdt")
            uow.reservations.create(
                reservation_id=reservation_id,
                account_scope=intent.account_scope,
                intent_id=intent.intent_id,
                inst_id=intent.inst_id,
                signed_contracts=intent.side.sign * intent.contracts,
                notional_usdt=notional,
                now=now,
            )
            event = uow.outbox.enqueue(
                aggregate_type="order",
                aggregate_id=order_id,
                event_type=SUBMIT_EVENT_TYPE,
                payload={
                    "order_id": order_id,
                    "approval_id": approval_id,
                    "approved": approved.model_dump(mode="json"),
                    "payload_hash": approved.payload_hash,
                },
                idempotency_key=f"submit:{intent.account_scope}:{intent.client_order_id}",
                now=now,
            )
            outbox_id = event.id
        return StagedOrder(
            order_id=order_id,
            intent_id=intent.intent_id,
            client_order_id=intent.client_order_id,
            approval_id=approval_id,
            reservation_id=reservation_id,
            outbox_event_id=outbox_id,
        )

    async def dispatch_order(self, order_id: str) -> SubmissionResult:
        """Réclame l'événement d'outbox de cet ordre et le traite. Non-leader : aucun chemin d'envoi."""
        claim = self._claim_for_order(order_id)
        if claim is None:
            with self._uow.transaction() as uow:
                row = uow.orders.get(order_id)
                if row is None:
                    raise OrderStateError("ordre inconnu", order_id=order_id)
                return SubmissionResult(
                    intent_id=row.intent_id,
                    client_order_id=row.client_order_id,
                    outcome=SubmissionOutcome.NOT_SENT,
                    error_code="OUTBOX_NOT_CLAIMED",
                    message="événement d'outbox déjà réclamé ou traité",
                )
        return await self._process_submit(claim)

    async def dispatch_pending(self, *, limit: int = 10) -> list[SubmissionResult]:
        """Traite les événements d'outbox en attente (dont ceux relivrés après expiration de bail)."""
        if not self._leadership.is_leader:
            return []
        token = self._leadership.fencing_token
        assert token is not None
        with self._uow.transaction() as uow:
            claims = self._outbox.claim(
                uow, fencing_token=token, limit=limit, event_types={SUBMIT_EVENT_TYPE}
            )
        results = []
        for claim in claims:
            results.append(await self._process_submit(claim))
        return results

    def _claim_for_order(self, order_id: str) -> ClaimedEvent | None:
        if not self._leadership.is_leader:
            raise LeadershipError("instance non leader : soumission refusée", holder=self.worker_id)
        token = self._leadership.fencing_token
        assert token is not None
        with self._uow.transaction() as uow:
            self._leadership.assert_leader(uow)
            claims = self._outbox.claim(uow, fencing_token=token, limit=50, event_types={SUBMIT_EVENT_TYPE})
            # Ne garder que l'événement de cet ordre ; libérer les autres réclamations prises au passage.
            wanted: ClaimedEvent | None = None
            for c in claims:
                if c.payload.get("order_id") == order_id and wanted is None:
                    wanted = c
                else:
                    self._outbox.fail(uow, c, error="réclamation relâchée (hors cible)")
                    uow.session.flush()
        return wanted

    async def _process_submit(self, claim: ClaimedEvent) -> SubmissionResult:
        order_id = str(claim.payload["order_id"])
        approved = ApprovedOrder.model_validate(claim.payload["approved"])
        intent = approved.intent
        now = self._clock.now_utc()

        # --- Transaction A : vérifications + marquage de la tentative AVANT le réseau ------------------------
        with self._uow.transaction() as uow:
            token = self._leadership.assert_leader(uow)
            row = uow.orders.get(order_id)
            if row is None:
                self._outbox.fail(uow, claim, error="ordre introuvable", dead=True)
                raise OrderStateError("ordre introuvable", order_id=order_id)
            refusal = await self._refusal_reasons(uow, row, approved, now)
            if refusal:
                return self._not_sent(uow, row, claim, refusal, now)
            request = OrderRequest(
                account_scope=intent.account_scope,
                client_order_id=intent.client_order_id,
                inst_id=intent.inst_id,
                side=intent.side.value,
                contracts=intent.contracts,
                price_limit=intent.price_limit,
                order_type=intent.order_type.value,
                reduce_only=intent.reduce_only,
                fencing_token=token,
            )
            sent_payload = _request_payload(request)
            if (
                payload_hash({k: v for k, v in sent_payload.items() if k != "fencing_token"})
                != row.payload_hash
            ):
                return self._not_sent(uow, row, claim, [ReasonCode.PAYLOAD_HASH_MISMATCH.value], now)
            uow.orders.mark_attempt(
                order_id, expected_version=row.version, sent_payload=sent_payload, sent_at=now
            )

        # --- Réseau : UNE tentative -------------------------------------------------------------------------
        try:
            response = await self._adapter.place_order(request)
        except Exception as exc:
            log.warning("place_order_ambiguous", client_order_id=intent.client_order_id, error=str(exc))
            response = PlaceResponse(
                outcome=PlaceOutcome.UNKNOWN,
                client_order_id=intent.client_order_id,
                message=f"{type(exc).__name__}: {exc}",
                sent_at=now,
            )

        # --- Transaction B : persistance du résultat (relue, conditionnelle) -----------------------------------
        return self._persist_submit_result(order_id, claim, approved, response, sent_at=now)

    async def _refusal_reasons(
        self, uow: UnitOfWork, row: OrderRow, approved: ApprovedOrder, now: datetime
    ) -> list[str]:
        reasons: list[str] = []
        intent, decision = approved.intent, approved.decision
        state = OrderState(row.observed_state)
        if state is not OrderState.RISK_APPROVED or row.attempt_count > 0:
            reasons.append(
                ReasonCode.ORDER_STATE_UNKNOWN.value
                if state is OrderState.UNKNOWN
                else "ORDER_ALREADY_ATTEMPTED"
            )
            return reasons
        approval_row = uow.approvals.get(row.approval_id)
        if approval_row is None or approval_row.allowed_payload_hash != row.payload_hash:
            reasons.append(ReasonCode.PAYLOAD_HASH_MISMATCH.value)
        if approved.payload_hash != row.payload_hash:
            reasons.append(ReasonCode.PAYLOAD_HASH_MISMATCH.value)
        if decision.expires_at <= now or now - decision.created_at > self.settings.approval_ttl:
            reasons.append(ReasonCode.APPROVAL_EXPIRED.value)
        if intent.expires_at <= now or now - intent.created_at > self.settings.max_intent_age:
            reasons.append("INTENT_EXPIRED")
        if not intent.reduce_only:
            if self.entries_frozen:
                reasons.append(ReasonCode.HALTED.value)
            if self.reconciliation_required:
                reasons.append(ReasonCode.RECONCILIATION_PENDING.value)
        if reasons:
            return sorted(set(reasons))
        recheck = await self._rechecker.recheck(approved, now=now)
        if not recheck.ok:
            reasons.extend(recheck.reason_codes or ("RISK_RECHECK_REJECTED",))
        return sorted(set(reasons))

    def _not_sent(
        self, uow: UnitOfWork, row: OrderRow, claim: ClaimedEvent, reasons: list[str], now: datetime
    ) -> SubmissionResult:
        """Jamais envoyé : ordre REJECTED localement, réservation libérée (sans risque), outbox clôturée."""
        state = OrderState(row.observed_state)
        if state is OrderState.RISK_APPROVED and row.attempt_count == 0:
            res = mark_rejected_locally(projection_from_row(row), at=now)
            self._persist(uow, row, res.projection, now=now)
            self._journal_local(
                uow, row, OrderEventKind.REJECT, res.projection, now, reason=",".join(reasons)
            )
            uow.reservations.release_for_intent(
                row.intent_id, reason="NOT_SENT:" + ",".join(reasons), now=now
            )
        self._outbox.complete(uow, claim)
        log.info("order_not_sent", client_order_id=row.client_order_id, reasons=reasons)
        return SubmissionResult(
            intent_id=row.intent_id,
            client_order_id=row.client_order_id,
            outcome=SubmissionOutcome.NOT_SENT,
            error_code=reasons[0] if reasons else None,
            message=", ".join(reasons),
        )

    def _persist_submit_result(
        self,
        order_id: str,
        claim: ClaimedEvent,
        approved: ApprovedOrder,
        response: PlaceResponse,
        *,
        sent_at: datetime,
    ) -> SubmissionResult:
        last_error: Exception | None = None
        for _ in range(_MAX_PERSIST_RETRIES):
            try:
                with self._uow.transaction() as uow:
                    row = uow.orders.get(order_id)
                    if row is None:
                        raise OrderStateError("ordre disparu pendant l'envoi", order_id=order_id)
                    now = self._clock.now_utc()
                    p = projection_from_row(row)
                    if response.outcome is PlaceOutcome.ACK:
                        res = mark_acknowledged(p, exchange_order_id=response.exchange_order_id, ack_at=now)
                        kind, outcome = OrderEventKind.ACK, SubmissionOutcome.ACK
                    elif response.outcome is PlaceOutcome.REJECTED:
                        res = mark_rejected_locally(p, at=now)
                        kind, outcome = OrderEventKind.REJECT, SubmissionOutcome.REJECTED
                    else:
                        res = mark_unknown(p, at=now)
                        kind, outcome = OrderEventKind.UNKNOWN, SubmissionOutcome.UNKNOWN
                    if res.changed:
                        self._persist(
                            uow,
                            row,
                            res.projection,
                            now=now,
                            ack_at=now if kind is OrderEventKind.ACK else None,
                        )
                    self._journal_local(
                        uow, row, kind, res.projection, now, reason=response.message, code=response.code
                    )
                    if res.became_terminal:
                        uow.reservations.release_for_intent(
                            row.intent_id, reason=f"FINAL:{res.projection.observed_state.value}", now=now
                        )
                    if (
                        outcome is SubmissionOutcome.UNKNOWN
                        and self.settings.require_reconciliation_before_entries
                    ):
                        self.reconciliation_required = True
                    if res.anomalies:
                        self._note_anomaly(row.client_order_id, res)
                    self._outbox.complete(uow, claim)
                    if approved.intent.reduce_only and outcome is SubmissionOutcome.REJECTED:
                        # T38 : un rejet reduce-only n'est JAMAIS rejoué sans cette protection.
                        log.warning(
                            "reduce_only_rejected_no_retry",
                            client_order_id=row.client_order_id,
                            code=response.code,
                        )
                    return SubmissionResult(
                        intent_id=row.intent_id,
                        client_order_id=row.client_order_id,
                        outcome=outcome,
                        exchange_order_id=res.projection.exchange_order_id,
                        sent_at=sent_at,
                        ack_at=now if outcome is SubmissionOutcome.ACK else None,
                        error_code=response.code,
                        message=response.message,
                    )
            except StaleVersionError as exc:
                last_error = exc
                continue
        raise OrderStateError(
            "impossible de persister le résultat d'envoi", order_id=order_id
        ) from last_error

    # --- annulation / amendement ---------------------------------------------------------------------------

    async def cancel(self, client_order_id: str, *, reason: str) -> CancelResponse:
        """Annulation = opération réseau DISTINCTE ; l'état observé n'évolue qu'avec les événements."""
        now = self._clock.now_utc()
        with self._uow.transaction() as uow:
            self._leadership.assert_leader(uow)
            row = uow.orders.by_client_order_id(self.settings.account_scope, client_order_id)
            if row is None:
                raise OrderStateError("ordre inconnu", client_order_id=client_order_id)
            p = mark_cancel_requested(projection_from_row(row))
            self._persist(uow, row, p, now=now)
            inst_id, intent_id = row.inst_id, row.intent_id
        try:
            response = await self._adapter.cancel_order(self.settings.account_scope, client_order_id, inst_id)
        except Exception as exc:
            response = CancelResponse(
                outcome=PlaceOutcome.UNKNOWN, client_order_id=client_order_id, message=str(exc)
            )
        with self._uow.transaction() as uow:
            row = uow.orders.by_client_order_id(self.settings.account_scope, client_order_id)
            assert row is not None
            p = projection_from_row(row)
            if response.outcome is PlaceOutcome.REJECTED and p.pending_operation is PendingOperation.CANCEL:
                # L'exchange a refusé la demande (ordre déjà terminal côté exchange, etc.) : réconcilier.
                self._persist(uow, row, replace(p, pending_operation=PendingOperation.NONE), now=now)
            elif response.outcome is PlaceOutcome.ACK and not p.is_terminal:
                self._persist(uow, row, replace(p, observed_state=OrderState.CANCEL_REQUESTED), now=now)
            log.info(
                "cancel_requested",
                client_order_id=client_order_id,
                reason=reason,
                outcome=response.outcome.value,
                intent_id=intent_id,
            )
        return response

    async def amend(
        self, client_order_id: str, replacement: ApprovedOrder, *, reason: str
    ) -> tuple[CancelResponse, SubmissionResult]:
        """Amendement = annulation + nouvelle intention approuvée (le hash change, donc l'approbation aussi)."""
        cancel = await self.cancel(client_order_id, reason=f"amend:{reason}")
        if cancel.outcome is PlaceOutcome.UNKNOWN:
            self.reconciliation_required = True
        result = await self.submit(replacement)
        return cancel, result

    # --- événements du flux privé --------------------------------------------------------------------------

    async def consume_event(self, event: ExchangeEvent) -> TransitionResult | None:
        """Journalise, applique via la machine d'état, met à jour de façon conditionnelle, libère sur état final."""
        result: TransitionResult | None = None
        if event.order_event is not None:
            result = self._consume_order_event(event.order_event, raw=event.raw)
        if event.fill is not None:
            with self._uow.transaction() as uow:
                row = uow.orders.by_client_order_id(event.fill.account_scope, event.fill.client_order_id)
                inserted = uow.fills.insert(event.fill, order_id=None if row is None else row.order_id)
                if not inserted:
                    log.info("fill_duplicate_ignored", execution_key=event.fill.execution_key)
        return result

    def _consume_order_event(self, ev: OrderEvent, *, raw: dict[str, Any]) -> TransitionResult | None:
        scope = self.settings.account_scope
        raw_doc = {**raw, "source": raw.get("source", "private_stream")}
        for _ in range(_MAX_PERSIST_RETRIES):
            try:
                with self._uow.transaction() as uow:
                    row = uow.orders.by_client_order_id(scope, ev.client_order_id)
                    inserted = uow.order_events.append(
                        ev, account_scope=scope, order_id=None if row is None else row.order_id, raw=raw_doc
                    )
                    if not inserted:
                        return TransitionResult(
                            projection_from_row(row)
                            if row is not None
                            else OrderProjection(ev.client_order_id, Decimal(0)),
                            changed=False,
                            duplicate=True,
                            ignored_reason="duplicate",
                        )
                    if row is None:
                        self.orphan_events += 1
                        self.reconciliation_required = True
                        log.warning("orphan_order_event", client_order_id=ev.client_order_id)
                        return None
                    last_event_ts, last_receive_ts = uow.order_events.last_timestamps(
                        scope, ev.client_order_id
                    )
                    # L'événement que l'on vient d'insérer ne doit pas compter comme « déjà appliqué ».
                    p = projection_from_row(
                        row,
                        last_event_ts=None
                        if last_event_ts == ev.event_ts and ev.event_ts is not None
                        else last_event_ts,
                        last_receive_ts=None if last_receive_ts == ev.receive_ts else last_receive_ts,
                    )
                    res = apply_event(p, ev)
                    if res.changed:
                        self._persist(uow, row, res.projection, now=ev.receive_ts)
                    if res.became_terminal:
                        uow.reservations.release_for_intent(
                            row.intent_id,
                            reason=f"FINAL:{res.projection.observed_state.value}",
                            now=ev.receive_ts,
                        )
                    if res.anomalies:
                        self._note_anomaly(ev.client_order_id, res)
                    return res
            except StaleVersionError:
                continue
        raise OrderStateError("conflit de version persistant sur l'ordre", client_order_id=ev.client_order_id)

    # --- réconciliation ----------------------------------------------------------------------------------

    async def reconcile(self) -> ReconciliationReport:
        if self._reconciler is None:
            raise ConfigError("aucun réconciliateur attaché au gateway")
        report = await self._reconciler.run(startup=self.reconciliation_required)
        if report.ok and report.unknown_remaining == 0:
            self.reconciliation_required = False
        else:
            self.reconciliation_required = True
        return report

    def unknown_orders(self) -> list[str]:
        with self._uow.transaction() as uow:
            return [
                r.client_order_id
                for r in uow.orders.in_states(self.settings.account_scope, {OrderState.UNKNOWN})
            ]

    # --- internes ----------------------------------------------------------------------------------------

    def _persist(
        self,
        uow: UnitOfWork,
        row: OrderRow,
        p: OrderProjection,
        *,
        now: datetime,
        ack_at: datetime | None = None,
    ) -> None:
        uow.orders.apply_projection(
            row.order_id,
            expected_version=row.version,
            values=_values_from_projection(p),
            now=now,
            ack_at=ack_at,
        )

    def _journal_local(
        self,
        uow: UnitOfWork,
        row: OrderRow,
        kind: OrderEventKind,
        p: OrderProjection,
        now: datetime,
        *,
        reason: str | None,
        code: str | None = None,
    ) -> None:
        raw = {"source": "rest_response", "kind": kind.value, "code": code, "attempt": row.attempt_count}
        ev = OrderEvent(
            event_id=self._new_id("oev"),
            client_order_id=row.client_order_id,
            exchange_order_id=p.exchange_order_id,
            event_kind=kind,
            observed_state=p.observed_state,
            cumulative_filled=p.cumulative_filled,
            average_fill_price=p.average_fill_price,
            event_ts=None,
            receive_ts=now,
            raw_hash=payload_hash({**raw, "client_order_id": row.client_order_id, "at": now.isoformat()}),
            reason=reason,
        )
        uow.order_events.append(ev, account_scope=row.account_scope, order_id=row.order_id, raw=raw)

    def _note_anomaly(self, client_order_id: str, res: TransitionResult) -> None:
        entry = {"client_order_id": client_order_id, "anomalies": list(res.anomalies)}
        self.anomalies.append(entry)
        log.warning("order_event_anomaly", **entry)


def _request_payload(request: OrderRequest) -> dict[str, Any]:
    payload = asdict(request)
    payload["contracts"] = format(request.contracts, "f")
    payload["price_limit"] = None if request.price_limit is None else format(request.price_limit, "f")
    return payload
