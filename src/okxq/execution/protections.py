"""Protections (§31, §54) : stops reduce-only confirmés, Cancel-All-After, EMERGENCY_FLATTEN.

Règles :
- un stop n'existe que s'il est ACCEPTÉ côté exchange (``ProtectionStatus.state == "live"``) ; sinon la
  position est NON PROTÉGÉE, alertée, et réduite si la politique l'exige (T59) ;
- une entrée partielle protège la quantité RÉELLEMENT ouverte ; on n'ouvre jamais une position inversée
  (quantité du stop ≤ |position|, côté opposé, reduce-only) ;
- Cancel-All-After protège les ORDRES ouverts en cas de perte de heartbeat ; il ne ferme aucune position
  (T60) ;
- EMERGENCY_FLATTEN : figer les entrées, relever l'exposition, annuler, réduire par tranches bornées,
  suivre les fills, réconcilier ; exchange inaccessible → ``FLATTEN_PENDING`` / ``RESIDUAL_EXPOSURE``
  visibles ; jamais « FLAT » sans preuve (positions exchange relues à zéro) (T62).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import structlog

from okxq.domain.clocks import Clock
from okxq.domain.events import RiskEvent, Severity, SubmissionOutcome, SubmissionResult
from okxq.domain.ids import new_id
from okxq.domain.money import Side
from okxq.exchange.base import (
    ExchangeAdapter,
    PositionStatus,
    ProtectionRequest,
    ProtectionStatus,
)
from okxq.execution.gateway import DurableExecutionGateway

__all__ = [
    "CancelAllAfterHeartbeat",
    "EmergencyFlatten",
    "FlattenReport",
    "FlattenStatus",
    "ProtectionManager",
    "ProtectionOutcome",
    "UnprotectedPolicy",
]

log = structlog.get_logger(__name__)

AlertSink = Callable[[RiskEvent], None]
ReduceCallback = Callable[[str, Side, Decimal, datetime], Awaitable[SubmissionResult]]


def _log_alert(event: RiskEvent) -> None:
    log.warning("risk_event", **event.model_dump(mode="json"))


class UnprotectedPolicy(StrEnum):
    ALERT = "alert"
    REDUCE = "reduce"


@dataclass(frozen=True, slots=True)
class ProtectionOutcome:
    inst_id: str
    protected: bool
    position_contracts: Decimal
    protected_contracts: Decimal
    status: ProtectionStatus | None
    action_taken: str
    reason: str


class ProtectionManager:
    """Stops reduce-only confirmés ; suivi des positions non protégées."""

    def __init__(
        self,
        *,
        adapter: ExchangeAdapter,
        clock: Clock,
        account_scope: str,
        on_unprotected: UnprotectedPolicy = UnprotectedPolicy.ALERT,
        reduce_callback: ReduceCallback | None = None,
        alert_sink: AlertSink = _log_alert,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        if on_unprotected is UnprotectedPolicy.REDUCE and reduce_callback is None:
            raise ValueError("la politique REDUCE exige un reduce_callback (ordre reduce-only approuvé)")
        self._adapter = adapter
        self._clock = clock
        self.account_scope = account_scope
        self.policy = on_unprotected
        self._reduce = reduce_callback
        self._alert = alert_sink
        self._new_id = id_factory
        self.active: dict[str, ProtectionStatus] = {}
        self.unprotected: dict[str, ProtectionOutcome] = {}

    async def protect_position(
        self,
        position: PositionStatus,
        *,
        stop_price: Decimal,
        trigger_reference: str = "mark",
    ) -> ProtectionOutcome:
        qty = abs(position.signed_contracts)
        if qty == 0:
            self.unprotected.pop(position.inst_id, None)
            return ProtectionOutcome(position.inst_id, True, qty, Decimal(0), None, "none", "position plate")
        side = Side.SELL if position.signed_contracts > 0 else Side.BUY
        previous = self.active.get(position.inst_id)
        if previous is not None and previous.state == "live":
            try:
                await self._adapter.cancel_protection(
                    self.account_scope, previous.client_algo_id, position.inst_id
                )
            except Exception as exc:  # l'ancien stop reste peut-être actif : on le note, on ne suppose rien
                log.warning("protection_cancel_failed", inst_id=position.inst_id, error=str(exc))
        request = ProtectionRequest(
            account_scope=self.account_scope,
            client_algo_id=self._new_id("alg"),
            inst_id=position.inst_id,
            side=side.value,
            contracts=qty,  # jamais plus que la quantité réellement ouverte : pas d'inversion
            trigger_price=stop_price,
            trigger_reference=trigger_reference,
            reduce_only=True,
        )
        try:
            status = await self._adapter.place_protection(request)
        except Exception as exc:
            return await self._unprotected(position, qty, side, None, f"exchange_error: {exc}")
        if status.state != "live":
            return await self._unprotected(position, qty, side, status, f"stop non accepté ({status.state})")
        self.active[position.inst_id] = status
        self.unprotected.pop(position.inst_id, None)
        return ProtectionOutcome(position.inst_id, True, qty, qty, status, "stop_confirmed", "accepté")

    async def _unprotected(
        self, position: PositionStatus, qty: Decimal, side: Side, status: ProtectionStatus | None, reason: str
    ) -> ProtectionOutcome:
        now = self._clock.now_utc()
        self._alert(
            RiskEvent(
                event_id=self._new_id("rsk"),
                severity=Severity.CRITICAL,
                reason_code="POSITION_UNPROTECTED",
                affected_scope=f"{self.account_scope}:{position.inst_id}",
                evidence={"contracts": format(qty, "f"), "reason": reason},
                requested_action=self.policy.value,
                created_at=now,
            )
        )
        action = "alert"
        if self.policy is UnprotectedPolicy.REDUCE and self._reduce is not None:
            result = await self._reduce(position.inst_id, side, qty, now)
            action = f"reduce:{result.outcome.value}"
        outcome = ProtectionOutcome(position.inst_id, False, qty, Decimal(0), status, action, reason)
        self.unprotected[position.inst_id] = outcome
        return outcome

    async def refresh(self) -> list[ProtectionOutcome]:
        """Relit les protections côté exchange : tout stop absent/non live rend la position non protégée."""
        statuses = {s.client_algo_id: s for s in await self._adapter.protections(self.account_scope)}
        outcomes: list[ProtectionOutcome] = []
        for inst_id, active in list(self.active.items()):
            current = statuses.get(active.client_algo_id)
            if current is None or current.state != "live":
                del self.active[inst_id]
                outcome = ProtectionOutcome(
                    inst_id, False, Decimal(0), Decimal(0), current, "alert", "stop disparu"
                )
                self.unprotected[inst_id] = outcome
                outcomes.append(outcome)
        return outcomes


class CancelAllAfterHeartbeat:
    """Dead-man switch des ORDRES (§54) : ré-armé à chaque heartbeat, jamais une fermeture de position."""

    def __init__(
        self,
        *,
        adapter: ExchangeAdapter,
        clock: Clock,
        timeout_seconds: int,
        heartbeat_seconds: int,
        enabled: bool,
    ) -> None:
        if heartbeat_seconds * 3 > timeout_seconds:
            raise ValueError("heartbeat CAA trop lent par rapport à son timeout")
        self._adapter = adapter
        self._clock = clock
        self.timeout_seconds = timeout_seconds
        self.heartbeat = timedelta(seconds=heartbeat_seconds)
        self.enabled = enabled
        self.armed = False
        self.last_beat_at: datetime | None = None
        self.failures = 0

    async def beat(self, *, force: bool = False) -> bool:
        if not self.enabled:
            return False
        now = self._clock.now_utc()
        if not force and self.last_beat_at is not None and now - self.last_beat_at < self.heartbeat:
            return True
        try:
            accepted = await self._adapter.cancel_all_after(self.timeout_seconds)
        except Exception as exc:
            self.failures += 1
            log.warning("cancel_all_after_failed", error=str(exc))
            return False
        if accepted:
            self.armed = True
            self.last_beat_at = now
            self.failures = 0
        return accepted

    async def disarm(self) -> bool:
        if not self.armed:
            return True
        try:
            ok = await self._adapter.cancel_all_after(0)
        except Exception as exc:
            log.warning("cancel_all_after_disarm_failed", error=str(exc))
            return False
        self.armed = not ok
        return ok


class FlattenStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    FLATTEN_PENDING = "FLATTEN_PENDING"
    RESIDUAL_EXPOSURE = "RESIDUAL_EXPOSURE"
    FLAT_CONFIRMED = "FLAT_CONFIRMED"
    ESCALATED = "ESCALATED"


@dataclass(slots=True)
class FlattenReport:
    status: FlattenStatus = FlattenStatus.NOT_STARTED
    attempts: int = 0
    residual: dict[str, str] = field(default_factory=dict)
    cancelled_orders: int = 0
    reductions: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    escalated: bool = False
    reconciled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "attempts": self.attempts,
            "residual": dict(self.residual),
            "cancelled_orders": self.cancelled_orders,
            "reductions": list(self.reductions),
            "errors": list(self.errors),
            "escalated": self.escalated,
            "reconciled": self.reconciled,
        }


ReduceOrderFactory = Callable[[str, Side, Decimal, datetime], Awaitable[SubmissionResult]]


class EmergencyFlatten:
    """Procédure EMERGENCY_FLATTEN. La preuve d'être plat vient UNIQUEMENT des positions relues sur l'exchange."""

    def __init__(
        self,
        *,
        adapter: ExchangeAdapter,
        gateway: DurableExecutionGateway,
        clock: Clock,
        account_scope: str,
        reduce_order: ReduceOrderFactory,
        max_attempts: int = 3,
        max_contracts_per_reduction: Decimal = Decimal("1e9"),
        deadline: timedelta = timedelta(minutes=5),
        alert_sink: AlertSink = _log_alert,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        if max_attempts <= 0 or max_contracts_per_reduction <= 0:
            raise ValueError("max_attempts et max_contracts_per_reduction doivent être positifs")
        self._adapter = adapter
        self._gateway = gateway
        self._clock = clock
        self.account_scope = account_scope
        self._reduce_order = reduce_order
        self.max_attempts = max_attempts
        self.max_per_reduction = max_contracts_per_reduction
        self.deadline = deadline
        self._alert = alert_sink
        self._new_id = id_factory
        self.report = FlattenReport()

    async def run(self, *, reason: str) -> FlattenReport:
        rep = FlattenReport(status=FlattenStatus.FLATTEN_PENDING)
        self.report = rep
        started = self._clock.now_utc()
        self._gateway.freeze_entries(f"EMERGENCY_FLATTEN:{reason}")

        exposure = await self._read_exposure(rep)
        if exposure is None:
            return self._escalate(rep, "exposition illisible : exchange inaccessible")
        await self._cancel_open_orders(rep)

        while rep.attempts < self.max_attempts:
            if not exposure:
                break
            if self._clock.now_utc() - started > self.deadline:
                rep.errors.append("délai dépassé")
                break
            rep.attempts += 1
            now = self._clock.now_utc()
            for inst_id, signed in sorted(exposure.items()):
                qty = min(abs(signed), self.max_per_reduction)
                side = Side.SELL if signed > 0 else Side.BUY
                try:
                    result = await self._reduce_order(inst_id, side, qty, now)
                except Exception as exc:
                    rep.errors.append(f"{inst_id}: réduction impossible ({exc})")
                    continue
                rep.reductions.append(
                    {
                        "inst_id": inst_id,
                        "side": side.value,
                        "contracts": format(qty, "f"),
                        "outcome": result.outcome.value,
                        "attempt": rep.attempts,
                    }
                )
                if result.outcome is SubmissionOutcome.UNKNOWN:
                    rep.errors.append(f"{inst_id}: réduction UNKNOWN, réconciliation requise")
            exposure = await self._read_exposure(rep)
            if exposure is None:
                return self._escalate(rep, "exposition illisible après réductions")

        rep.residual = {k: format(v, "f") for k, v in exposure.items() if v != 0}
        if not rep.residual:
            rep.status = FlattenStatus.FLAT_CONFIRMED
        else:
            rep.status = FlattenStatus.RESIDUAL_EXPOSURE
            self._escalate(rep, "exposition résiduelle après réductions bornées")
        try:
            report = await self._gateway.reconcile()
            rep.reconciled = report.ok
        except Exception as exc:
            rep.errors.append(f"réconciliation impossible : {exc}")
        return rep

    async def _read_exposure(self, rep: FlattenReport) -> dict[str, Decimal] | None:
        try:
            positions = await self._adapter.positions(self.account_scope)
        except Exception as exc:
            rep.errors.append(f"positions illisibles : {exc}")
            return None
        return {p.inst_id: p.signed_contracts for p in positions if p.signed_contracts != 0}

    async def _cancel_open_orders(self, rep: FlattenReport) -> None:
        try:
            open_orders = await self._adapter.open_orders(self.account_scope)
        except Exception as exc:
            rep.errors.append(f"ordres ouverts illisibles : {exc}")
            return
        for o in open_orders:
            try:
                await self._gateway.cancel(o.client_order_id, reason="emergency_flatten")
                rep.cancelled_orders += 1
            except Exception as exc:
                rep.errors.append(f"{o.client_order_id}: annulation impossible ({exc})")

    def _escalate(self, rep: FlattenReport, why: str) -> FlattenReport:
        rep.escalated = True
        if rep.status is FlattenStatus.FLATTEN_PENDING:
            rep.status = (
                FlattenStatus.ESCALATED
                if rep.attempts >= self.max_attempts
                else FlattenStatus.FLATTEN_PENDING
            )
        self._alert(
            RiskEvent(
                event_id=self._new_id("rsk"),
                severity=Severity.CRITICAL,
                reason_code="EMERGENCY_FLATTEN_INCOMPLETE",
                affected_scope=self.account_scope,
                evidence={"status": rep.status.value, "why": why, "residual": dict(rep.residual)},
                requested_action="operator_intervention",
                created_at=self._clock.now_utc(),
            )
        )
        rep.errors.append(why)
        return rep
