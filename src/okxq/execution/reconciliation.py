"""Réconciliation (§52.4) : au démarrage et périodiquement, l'exchange est la vérité.

Séquence de démarrage (orchestrée par le runtime, documentée ici et dans ``docs/execution.md``) :
config → environnement → leadership → flux privés → bootstrap/réconciliation → protections →
validation des données → autorisation des entrées. Aucune nouvelle entrée tant que ``run(startup=True)``
n'a pas rendu un rapport ``ok`` sans UNKNOWN restant (T33).

Ce que fait ``run`` :
- ordres ouverts côté exchange et détail par ``clOrdId`` pour chacun de nos ordres non terminaux ;
- résolution des UNKNOWN : trouvé → événement de réconciliation appliqué par la machine d'état ;
  introuvable après le délai de grâce → REJECTED (jamais atteint l'exchange), réservation libérée ;
- fills avec RECOUVREMENT temporel (les identifiants dédupliquent) ;
- positions et soldes comparés par fenêtres de réconciliation, pas par égalité instantanée : chaque
  écart porte montant, âge, origine probable et nombre de tentatives ; au-delà d'un seuil ou d'un délai,
  les nouvelles prises de risque sont bloquées ;
- rapport ``ReconciliationReport``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from okxq.domain.clocks import Clock
from okxq.domain.events import OrderEvent, OrderEventKind, ReconciliationReport
from okxq.domain.ids import new_id, payload_hash
from okxq.domain.orders import OrderState
from okxq.exchange.base import ExchangeAdapter, ExchangeEvent, OrderStatus
from okxq.execution.gateway import DurableExecutionGateway
from okxq.persistence.repositories import UnitOfWorkFactory

__all__ = [
    "STARTUP_SEQUENCE",
    "PositionMismatch",
    "Reconciler",
    "ReconciliationSettings",
    "order_event_from_status",
]

log = structlog.get_logger(__name__)

STARTUP_SEQUENCE: tuple[str, ...] = (
    "config",
    "environment",
    "leadership",
    "private_streams",
    "bootstrap_reconciliation",
    "protections",
    "data_validation",
    "entries_authorized",
)

_STATUS_TO_STATE: dict[str, tuple[OrderState, OrderEventKind]] = {
    "live": (OrderState.ACKNOWLEDGED, OrderEventKind.ACK),
    "partially_filled": (OrderState.PARTIALLY_FILLED, OrderEventKind.PARTIAL_FILL),
    "filled": (OrderState.FILLED, OrderEventKind.FILL),
    "canceled": (OrderState.CANCELED, OrderEventKind.CANCEL),
    "rejected": (OrderState.REJECTED, OrderEventKind.REJECT),
    "expired": (OrderState.EXPIRED, OrderEventKind.EXPIRE),
}


@dataclass(frozen=True, slots=True)
class ReconciliationSettings:
    unknown_grace: timedelta = timedelta(seconds=10)
    fills_overlap: timedelta = timedelta(minutes=5)
    fills_initial_lookback: timedelta = timedelta(hours=24)
    position_tolerance_contracts: Decimal = Decimal(0)
    mismatch_block_after: timedelta = timedelta(seconds=60)
    mismatch_block_contracts: Decimal = Decimal("1e9")
    max_attempts_before_escalation: int = 3


@dataclass(slots=True)
class PositionMismatch:
    inst_id: str
    expected_contracts: Decimal
    observed_contracts: Decimal
    first_seen_at: datetime
    last_seen_at: datetime
    attempts: int = 1
    probable_origin: str = "unexplained"

    @property
    def amount(self) -> Decimal:
        return abs(self.observed_contracts - self.expected_contracts)

    def age(self, now: datetime) -> timedelta:
        return now - self.first_seen_at

    def as_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "inst_id": self.inst_id,
            "expected_contracts": format(self.expected_contracts, "f"),
            "observed_contracts": format(self.observed_contracts, "f"),
            "amount": format(self.amount, "f"),
            "age_seconds": int(self.age(now).total_seconds()),
            "probable_origin": self.probable_origin,
            "attempts": self.attempts,
        }


def order_event_from_status(
    status: OrderStatus, *, receive_ts: datetime, event_id: str, source: str = "reconciliation"
) -> OrderEvent | None:
    """Traduit un ``OrderStatus`` en événement de réconciliation (hash stable = idempotent)."""
    mapped = _STATUS_TO_STATE.get(status.state)
    if mapped is None:
        return None
    state, kind = mapped
    raw = {
        "source": source,
        "state": status.state,
        "filled": format(status.filled_contracts, "f"),
        "exchange_order_id": status.exchange_order_id,
        "updated_at": None if status.updated_at is None else status.updated_at.isoformat(),
    }
    return OrderEvent(
        event_id=event_id,
        client_order_id=status.client_order_id,
        exchange_order_id=status.exchange_order_id,
        event_kind=kind,
        observed_state=state,
        cumulative_filled=status.filled_contracts,
        average_fill_price=status.average_price,
        event_ts=status.updated_at,
        receive_ts=receive_ts,
        raw_hash=payload_hash({**raw, "client_order_id": status.client_order_id}),
        reason=source,
    )


@dataclass(slots=True)
class _RunState:
    notes: list[str] = field(default_factory=list)
    orders_checked: int = 0
    unknown_resolved: int = 0
    unknown_remaining: int = 0
    new_fills: dict[str, int] = field(default_factory=dict)
    exchange_unreachable: bool = False


class Reconciler:
    """Réconciliation périodique et de démarrage ; conserve les écarts entre exécutions."""

    def __init__(
        self,
        *,
        adapter: ExchangeAdapter,
        uow_factory: UnitOfWorkFactory,
        gateway: DurableExecutionGateway,
        clock: Clock,
        account_scope: str,
        settings: ReconciliationSettings | None = None,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        self._adapter = adapter
        self._uow = uow_factory
        self._gateway = gateway
        self._clock = clock
        self.account_scope = account_scope
        self.settings = settings or ReconciliationSettings()
        self._new_id = id_factory
        self.mismatches: dict[str, PositionMismatch] = {}
        self.last_fill_sync: datetime | None = None
        self.last_report: ReconciliationReport | None = None
        self.blocks_new_risk = True

    async def run(self, *, startup: bool) -> ReconciliationReport:
        started = self._clock.now_utc()
        st = _RunState()
        if startup:
            st.notes.append("startup: réconciliation avant toute nouvelle entrée")
        try:
            await self._reconcile_orders(st, started)
            await self._sync_fills(st, started)
            await self._reconcile_positions(st, started)
            balance_gap = await self._reconcile_balance(st, started)
        except Exception as exc:
            st.exchange_unreachable = True
            st.notes.append(f"exchange_unreachable: {type(exc).__name__}: {exc}")
            balance_gap = None
            with self._uow.transaction() as uow:
                st.unknown_remaining = len(uow.orders.in_states(self.account_scope, {OrderState.UNKNOWN}))
        now = self._clock.now_utc()
        blocking = [
            m
            for m in self.mismatches.values()
            if m.age(now) >= self.settings.mismatch_block_after
            or m.amount >= self.settings.mismatch_block_contracts
            or m.attempts >= self.settings.max_attempts_before_escalation
        ]
        self.blocks_new_risk = bool(blocking) or st.unknown_remaining > 0 or st.exchange_unreachable
        if blocking:
            st.notes.append("écarts de position bloquants : " + ", ".join(m.inst_id for m in blocking))
        report = ReconciliationReport(
            started_at=started,
            completed_at=now,
            orders_checked=st.orders_checked,
            unknown_resolved=st.unknown_resolved,
            unknown_remaining=st.unknown_remaining,
            position_mismatches=[m.as_dict(now) for m in self.mismatches.values()],
            balance_gap=balance_gap,
            ok=not self.blocks_new_risk,
            notes=st.notes,
        )
        self.last_report = report
        log.info(
            "reconciliation_done",
            ok=report.ok,
            orders_checked=report.orders_checked,
            unknown_remaining=report.unknown_remaining,
            mismatches=len(self.mismatches),
        )
        return report

    # --- ordres ------------------------------------------------------------------------------------------

    async def _reconcile_orders(self, st: _RunState, now: datetime) -> None:
        exchange_open = await self._adapter.open_orders(self.account_scope)
        open_by_id = {o.client_order_id: o for o in exchange_open}
        with self._uow.transaction() as uow:
            ours = uow.orders.open_orders(self.account_scope)
            known_ids = {r.client_order_id for r in ours}
            snapshot = [
                (r.client_order_id, r.inst_id, OrderState(r.observed_state), r.sent_at, r.created_at)
                for r in ours
            ]
        for cl_id in sorted(set(open_by_id) - known_ids):
            st.notes.append(f"ordre ouvert inconnu de notre journal : {cl_id}")
            self._gateway.orphan_events += 1
        for cl_id, inst_id, state, sent_at, created_at in snapshot:
            st.orders_checked += 1
            status = open_by_id.get(cl_id)
            if status is None:
                status = await self._adapter.get_order(self.account_scope, cl_id, inst_id)
            if status is not None:
                ev = order_event_from_status(
                    status, receive_ts=self._clock.now_utc(), event_id=self._new_id("rec")
                )
                if ev is None:
                    st.notes.append(f"{cl_id}: état exchange non interprétable ({status.state})")
                    st.unknown_remaining += 1
                    continue
                res = await self._gateway.consume_event(
                    ExchangeEvent(
                        kind="order",
                        receive_ts=ev.receive_ts,
                        order_event=ev,
                        raw={"source": "reconciliation"},
                    )
                )
                if (
                    state is OrderState.UNKNOWN
                    and res is not None
                    and res.projection.observed_state is not OrderState.UNKNOWN
                ):
                    st.unknown_resolved += 1
                continue
            reference = sent_at or created_at
            if state is OrderState.UNKNOWN and now - reference >= self.settings.unknown_grace:
                ev = OrderEvent(
                    event_id=self._new_id("rec"),
                    client_order_id=cl_id,
                    exchange_order_id=None,
                    event_kind=OrderEventKind.REJECT,
                    observed_state=OrderState.REJECTED,
                    cumulative_filled=Decimal(0),
                    event_ts=None,
                    receive_ts=self._clock.now_utc(),
                    raw_hash=payload_hash(
                        {"source": "reconciliation", "resolution": "not_on_exchange", "cl": cl_id}
                    ),
                    reason="introuvable sur l'exchange après le délai de grâce",
                )
                await self._gateway.consume_event(
                    ExchangeEvent(
                        kind="order",
                        receive_ts=ev.receive_ts,
                        order_event=ev,
                        raw={"source": "reconciliation"},
                    )
                )
                st.unknown_resolved += 1
                st.notes.append(f"{cl_id}: UNKNOWN résolu en REJECTED (jamais atteint l'exchange)")
            elif state is OrderState.UNKNOWN:
                st.unknown_remaining += 1
            else:
                st.unknown_remaining += 1
                st.notes.append(f"{cl_id}: ordre {state.value} introuvable sur l'exchange")

    # --- fills -------------------------------------------------------------------------------------------

    async def _sync_fills(self, st: _RunState, now: datetime) -> None:
        base = self.last_fill_sync or (now - self.settings.fills_initial_lookback)
        since = base - self.settings.fills_overlap
        fills = await self._adapter.fills_since(self.account_scope, since)
        inserted = 0
        with self._uow.transaction() as uow:
            for fill in fills:
                row = uow.orders.by_client_order_id(self.account_scope, fill.client_order_id)
                if uow.fills.insert(fill, order_id=None if row is None else row.order_id):
                    inserted += 1
                    st.new_fills[fill.inst_id] = st.new_fills.get(fill.inst_id, 0) + 1
        self.last_fill_sync = now
        if inserted:
            st.notes.append(f"{inserted} fill(s) récupéré(s) par recouvrement")

    # --- positions / soldes --------------------------------------------------------------------------------

    async def _reconcile_positions(self, st: _RunState, now: datetime) -> None:
        observed = {p.inst_id: p for p in await self._adapter.positions(self.account_scope)}
        with self._uow.transaction() as uow:
            local = uow.snapshots.latest_positions(self.account_scope, source="local")
            expected = (
                {k: v.signed_contracts for k, v in local.items()}
                if local
                else uow.fills.signed_contracts_by_instrument(self.account_scope)
            )
            unknown_insts = {
                r.inst_id for r in uow.orders.in_states(self.account_scope, {OrderState.UNKNOWN})
            }
            orphan_insts = {
                r.client_order_id for r in uow.order_events.orphans(self.account_scope)
            }  # identifiants seulement : l'instrument d'un orphelin n'est pas garanti
            for p in observed.values():
                uow.snapshots.record_position(
                    account_scope=self.account_scope,
                    inst_id=p.inst_id,
                    as_of=p.as_of,
                    source="exchange",
                    signed_base_qty=Decimal(0),
                    signed_contracts=p.signed_contracts,
                    average_entry_price=p.average_price,
                    mark_price=p.mark_price,
                    liquidation_price=p.liquidation_price,
                    margin=p.margin,
                    leverage=p.leverage,
                )
        for inst_id in sorted(set(observed) | set(expected)):
            exp = expected.get(inst_id, Decimal(0))
            obs = observed[inst_id].signed_contracts if inst_id in observed else Decimal(0)
            if abs(obs - exp) <= self.settings.position_tolerance_contracts:
                if inst_id in self.mismatches:
                    st.notes.append(f"{inst_id}: écart résolu")
                    del self.mismatches[inst_id]
                continue
            origin = "unexplained"
            if inst_id in unknown_insts:
                origin = "unknown_order"
            elif st.new_fills.get(inst_id):
                origin = "late_fill"
            elif orphan_insts:
                origin = "external_or_orphan_order"
            m = self.mismatches.get(inst_id)
            if m is None:
                self.mismatches[inst_id] = PositionMismatch(
                    inst_id=inst_id,
                    expected_contracts=exp,
                    observed_contracts=obs,
                    first_seen_at=now,
                    last_seen_at=now,
                    probable_origin=origin,
                )
            else:
                m.expected_contracts, m.observed_contracts = exp, obs
                m.last_seen_at = now
                m.attempts += 1
                m.probable_origin = origin

    async def _reconcile_balance(self, st: _RunState, now: datetime) -> Decimal | None:
        balance = await self._adapter.balance(self.account_scope)
        with self._uow.transaction() as uow:
            local = uow.snapshots.latest_account(self.account_scope, source="ledger")
            uow.snapshots.record_account(
                account_scope=self.account_scope,
                equity_version=f"exchange-{int(now.timestamp() * 1000)}-{self._new_id('eq')[-6:]}",
                as_of=balance.as_of,
                source="exchange",
                cash_collateral=balance.cash_balance,
                unrealized_pnl=balance.unrealized_pnl or Decimal(0),
                equity=balance.total_equity,
                available_margin=balance.available,
                used_margin=balance.used_margin,
                raw=dict(balance.raw),
            )
            if local is None:
                return None
            return balance.total_equity - local.equity
