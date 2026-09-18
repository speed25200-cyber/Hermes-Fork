"""Exchange virtuel : même interface que OKX (``ExchangeAdapter``), horloge et marché simulés (§32, §33).

Le backtest et les modes directs partagent le domaine, le risque, la comptabilité et le gateway. Seuls
l'horloge et l'adaptateur d'exchange changent. Il n'existe donc PAS de logique de stratégie propre au
backtest, qui serait par construction plus favorable.

Ce que le simulateur refuse de faire, parce que ce serait mentir :

- faire progresser notre file d'attente avec les ANNULATIONS d'un niveau : le carnet L2 ne dit pas où
  nous sommes dans la file, seul le volume échangé à notre prix fait avancer (§33.2) ;
- exécuter un maker parce que le prix a été « touché » : il faut du volume, et deux de nos ordres ne
  consomment jamais deux fois la même profondeur (T28, T30) ;
- accorder des frais maker à un post-only qui aurait croisé à son arrivée : il est rejeté (§33.2) ;
- supprimer rétroactivement un fill survenu avant la confirmation d'une annulation (§33.3, T31) ;
- afficher « FLAT » sans preuve, ou confondre PnL marqué et PnL après liquidation simulée (§33.3).

Les pannes sont des OPTIONS explicites (``ChaosOptions``) : ACK perdu, fill avant ACK, fills dupliqués,
rejet d'une jambe, exchange injoignable. Elles servent les scénarios §68.3, jamais à embellir un résultat.
"""

from __future__ import annotations

import heapq
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from okxq.backtest.fills import DepthReservation, match_aggressive, post_only_would_cross
from okxq.backtest.latency import LatencyKind, LatencyModel
from okxq.backtest.market_state import BookView, MarketState, Trade
from okxq.backtest.queue_models import QueueAssumption, QueueState, initial_ahead
from okxq.domain.clocks import Clock
from okxq.domain.errors import ExchangeError
from okxq.domain.events import (
    Fill,
    Liquidity,
    OrderEvent,
    OrderEventKind,
)
from okxq.domain.ids import new_id
from okxq.domain.instruments import InstrumentSpec
from okxq.domain.money import ZERO, Side, dec
from okxq.domain.orders import OrderKind, OrderState
from okxq.domain.positions import Position, apply_fill
from okxq.exchange.base import (
    BalanceStatus,
    CancelResponse,
    ExchangeEvent,
    OrderRequest,
    OrderStatus,
    PlaceOutcome,
    PlaceResponse,
    PositionStatus,
    ProtectionRequest,
    ProtectionStatus,
)

__all__ = ["ChaosOptions", "VirtualExchange", "VirtualOrder"]

FEE_MAKER_DEFAULT = Decimal("0.0002")
FEE_TAKER_DEFAULT = Decimal("0.0005")


class _Phase(StrEnum):
    PENDING_ARRIVAL = "pending_arrival"
    RESTING = "resting"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class ChaosOptions:
    """Pannes simulées, toutes explicites et nommées (§68.3)."""

    lose_ack: frozenset[str] = frozenset()  # client_order_id dont l'ACK est perdu
    fill_before_ack: frozenset[str] = frozenset()
    duplicate_fills: int = 1  # nombre de livraisons du même fill (dédupliqué en aval)
    reject_client_order_ids: frozenset[str] = frozenset()
    unreachable: bool = False  # exchange injoignable : toute écriture lève
    suspended_instruments: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.duplicate_fills < 1:
            raise ValueError("duplicate_fills doit valoir au moins 1")


@dataclass(slots=True)
class VirtualOrder:
    client_order_id: str
    exchange_order_id: str
    inst_id: str
    side: Side
    order_type: OrderKind
    contracts: Decimal
    price_limit: Decimal | None
    reduce_only: bool
    created_at: datetime
    arrives_at: datetime
    phase: _Phase = _Phase.PENDING_ARRIVAL
    state: OrderState = OrderState.SUBMITTED
    filled: Decimal = ZERO
    notional: Decimal = ZERO
    queue: QueueState | None = None
    cancel_requested_at: datetime | None = None
    cancel_effective_at: datetime | None = None
    reject_reason: str | None = None

    @property
    def remaining(self) -> Decimal:
        return self.contracts - self.filled

    @property
    def average_price(self) -> Decimal | None:
        return self.notional / self.filled if self.filled > 0 else None

    def status(self, as_of: datetime) -> OrderStatus:
        mapping = {
            OrderState.SUBMITTED: "live",
            OrderState.ACKNOWLEDGED: "live",
            OrderState.PARTIALLY_FILLED: "partially_filled",
            OrderState.FILLED: "filled",
            OrderState.CANCELED: "canceled",
            OrderState.REJECTED: "rejected",
            OrderState.EXPIRED: "expired",
        }
        return OrderStatus(
            client_order_id=self.client_order_id,
            exchange_order_id=self.exchange_order_id,
            state=mapping.get(self.state, "unknown"),
            filled_contracts=self.filled,
            average_price=self.average_price,
            updated_at=as_of,
            raw={"reduce_only": self.reduce_only, "order_type": self.order_type.value},
        )


@dataclass(slots=True)
class _VirtualProtection:
    client_algo_id: str
    exchange_algo_id: str
    inst_id: str
    side: Side
    contracts: Decimal
    trigger_price: Decimal
    trigger_reference: str
    state: str = "live"
    triggered_at: datetime | None = None


class VirtualExchange:
    """Adaptateur d'exchange simulé. Implémente ``okxq.exchange.base.ExchangeAdapter``."""

    def __init__(
        self,
        *,
        # N'importe quelle horloge du protocole convient : le simulateur ne lit que `now_utc()`. Un
        # processus PAPER en marche l'alimente avec l'horloge système, un backtest avec une horloge simulée.
        clock: Clock,
        market: MarketState,
        account_scope: str = "paper-local",
        initial_cash: Decimal = Decimal("100000"),
        latency: LatencyModel | None = None,
        queue_assumption: QueueAssumption = QueueAssumption.PESSIMISTIC,
        fee_maker: Decimal = FEE_MAKER_DEFAULT,
        fee_taker: Decimal = FEE_TAKER_DEFAULT,
        chaos: ChaosOptions | None = None,
        settle_ccy: str = "USDT",
    ) -> None:
        self._clock = clock
        self.market = market
        self.account_scope = account_scope
        self.cash = dec(initial_cash, field="initial_cash")
        self.latency = latency or LatencyModel()
        self.queue_assumption = queue_assumption
        self.fee_maker = dec(fee_maker, field="fee_maker")
        self.fee_taker = dec(fee_taker, field="fee_taker")
        self.chaos = chaos or ChaosOptions()
        self.settle_ccy = settle_ccy
        self.orders: dict[str, VirtualOrder] = {}
        self._protections: dict[str, _VirtualProtection] = {}
        self._positions: dict[str, Position] = {}
        self.realized_pnl = ZERO
        self.fees_paid = ZERO
        self.funding_paid = ZERO
        self.reservation = DepthReservation()
        # Version de carnet par instrument : la réservation de profondeur n'est libérée que lorsqu'une
        # NOUVELLE version de carnet est publiée, c'est-à-dire lorsqu'une profondeur est réellement
        # observée à nouveau. La libérer à la fin d'un ordre permettrait à deux de nos ordres de
        # consommer deux fois le même volume affiché (T30).
        self._book_versions: dict[str, int] = {}
        self._events: list[tuple[datetime, int, ExchangeEvent]] = []
        self._seq = 0
        self._cancel_all_after_deadline: datetime | None = None
        self._caa_fired = 0
        self.ambiguous_paths: list[dict[str, Any]] = []

    # --- identité -----------------------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "virtual"

    def _now(self) -> datetime:
        return self._clock.now_utc()

    def _spec(self, inst_id: str) -> InstrumentSpec:
        spec = self.market.specs.get(inst_id)
        if spec is None:
            raise ExchangeError("instrument inconnu du marché simulé", inst_id=inst_id)
        return spec

    def _emit(self, event: ExchangeEvent, *, at: datetime | None = None) -> None:
        self._seq += 1
        when = at or self._now()
        heapq.heappush(self._events, (when, self._seq, event))

    # --- ordres -------------------------------------------------------------------------------------

    async def instruments(self) -> list[InstrumentSpec]:
        return list(self.market.specs.values())

    async def place_order(self, request: OrderRequest) -> PlaceResponse:
        if self.chaos.unreachable:
            raise ExchangeError("exchange simulé injoignable", client_order_id=request.client_order_id)
        now = self._now()
        if request.client_order_id in self.orders:
            # Idempotence côté exchange : un même clOrdId n'ouvre jamais deux ordres.
            existing = self.orders[request.client_order_id]
            return PlaceResponse(
                PlaceOutcome.REJECTED,
                request.client_order_id,
                existing.exchange_order_id,
                code="51000",
                message="clOrdId déjà utilisé",
                sent_at=now,
                ack_at=now,
            )
        if request.inst_id in self.chaos.suspended_instruments:
            return PlaceResponse(
                PlaceOutcome.REJECTED,
                request.client_order_id,
                None,
                code="51001",
                message="instrument suspendu",
                sent_at=now,
                ack_at=now,
            )
        if request.client_order_id in self.chaos.reject_client_order_ids:
            return PlaceResponse(
                PlaceOutcome.REJECTED,
                request.client_order_id,
                None,
                code="51008",
                message="rejet simulé",
                sent_at=now,
                ack_at=now,
            )
        side = Side(request.side)
        order_type = OrderKind(request.order_type)
        if order_type is OrderKind.MARKET and request.price_limit is not None:
            raise ExchangeError("ordre market avec price_limit", client_order_id=request.client_order_id)
        if request.reduce_only:
            position = self._positions.get(request.inst_id)
            # Un reduce-only qui n'a rien à réduire est refusé : il ne doit JAMAIS ouvrir (T38).
            if position is None or position.is_flat or (position.signed_base_qty > 0) == (side is Side.BUY):
                return PlaceResponse(
                    PlaceOutcome.REJECTED,
                    request.client_order_id,
                    None,
                    code="51121",
                    message="reduce-only sans position à réduire",
                    sent_at=now,
                    ack_at=now,
                )
        arrival = now + self.latency.delay(LatencyKind.ORDER, request.client_order_id)
        order = VirtualOrder(
            client_order_id=request.client_order_id,
            exchange_order_id=new_id("vxo"),
            inst_id=request.inst_id,
            side=side,
            order_type=order_type,
            contracts=dec(request.contracts, field="contracts"),
            price_limit=None
            if request.price_limit is None
            else dec(request.price_limit, field="price_limit"),
            reduce_only=request.reduce_only,
            created_at=now,
            arrives_at=arrival,
        )
        self.orders[order.client_order_id] = order
        if order.client_order_id in self.chaos.lose_ack:
            # L'ordre EXISTE côté exchange, mais l'appelant ne reçoit rien : c'est un UNKNOWN (T32).
            self.ambiguous_paths.append(
                {
                    "client_order_id": order.client_order_id,
                    "reason": "ACK perdu : ordre accepté sans confirmation",
                }
            )
            raise ExchangeError("réponse perdue après envoi", client_order_id=order.client_order_id)
        ack_at = arrival + self.latency.delay(LatencyKind.ACK, order.client_order_id)
        self._emit(
            ExchangeEvent(
                kind="order",
                receive_ts=ack_at,
                order_event=OrderEvent(
                    event_id=new_id("oev"),
                    client_order_id=order.client_order_id,
                    exchange_order_id=order.exchange_order_id,
                    event_kind=OrderEventKind.ACK,
                    observed_state=OrderState.ACKNOWLEDGED,
                    cumulative_filled=ZERO,
                    event_ts=arrival,
                    receive_ts=ack_at,
                ),
            ),
            at=ack_at,
        )
        return PlaceResponse(
            PlaceOutcome.ACK,
            order.client_order_id,
            order.exchange_order_id,
            sent_at=now,
            ack_at=ack_at,
        )

    async def cancel_order(self, account_scope: str, client_order_id: str, inst_id: str) -> CancelResponse:
        if self.chaos.unreachable:
            raise ExchangeError("exchange simulé injoignable", client_order_id=client_order_id)
        order = self.orders.get(client_order_id)
        if order is None:
            return CancelResponse(
                PlaceOutcome.REJECTED, client_order_id, code="51603", message="ordre introuvable"
            )
        if order.phase is _Phase.DONE:
            return CancelResponse(
                PlaceOutcome.REJECTED, client_order_id, code="51402", message="ordre déjà terminal"
            )
        now = self._now()
        order.cancel_requested_at = now
        # L'annulation arrive après SON délai : des fills restent possibles d'ici là (§33.3, T31).
        order.cancel_effective_at = now + self.latency.delay(LatencyKind.CANCEL, client_order_id)
        return CancelResponse(PlaceOutcome.ACK, client_order_id)

    async def cancel_all_after(self, timeout_seconds: int) -> bool:
        if timeout_seconds <= 0:
            self._cancel_all_after_deadline = None
            return True
        self._cancel_all_after_deadline = self._now() + timedelta(seconds=timeout_seconds)
        return True

    async def get_order(self, account_scope: str, client_order_id: str, inst_id: str) -> OrderStatus | None:
        order = self.orders.get(client_order_id)
        return None if order is None else order.status(self._now())

    async def open_orders(self, account_scope: str) -> list[OrderStatus]:
        now = self._now()
        return [o.status(now) for o in self.orders.values() if o.phase is not _Phase.DONE]

    async def fills_since(self, account_scope: str, since: datetime) -> list[Fill]:
        out: list[Fill] = []
        for _, _, event in sorted(self._events):
            if event.fill is not None and event.fill.fill_at >= since:
                out.append(event.fill)
        return out

    # --- compte -------------------------------------------------------------------------------------

    def _mark(self, inst_id: str) -> Decimal | None:
        """Prix de référence : le mark d'OKX quand il est connu, sinon le mid du carnet observable."""
        mark = self.market.marks.get(inst_id)
        if mark is not None:
            return mark
        view = self.market.book_view(inst_id)
        return view.mid if view else None

    def unrealized(self) -> Decimal:
        total = ZERO
        for inst_id, position in self._positions.items():
            mark = self._mark(inst_id)
            if mark is not None:
                total += position.unrealized_pnl(mark)
        return total

    def equity(self) -> Decimal:
        return self.cash + self.unrealized()

    async def positions(self, account_scope: str) -> list[PositionStatus]:
        now = self._now()
        out: list[PositionStatus] = []
        for inst_id, position in self._positions.items():
            if position.is_flat:
                continue
            spec = self._spec(inst_id)
            out.append(
                PositionStatus(
                    inst_id=inst_id,
                    signed_contracts=position.signed_base_qty / spec.base_units_per_contract,
                    average_price=position.average_entry_price,
                    mark_price=self._mark(inst_id),
                    liquidation_price=None,  # inconnu du simulateur : une absence n'est pas une distance infinie
                    margin=None,
                    leverage=None,
                    as_of=now,
                    raw={"signed_base_qty": format(position.signed_base_qty, "f")},
                )
            )
        return out

    async def balance(self, account_scope: str) -> BalanceStatus:
        return BalanceStatus(
            settle_ccy=self.settle_ccy,
            total_equity=self.equity(),
            cash_balance=self.cash,
            available=self.cash,
            used_margin=None,
            unrealized_pnl=self.unrealized(),
            as_of=self._now(),
        )

    # --- protections --------------------------------------------------------------------------------

    async def place_protection(self, request: ProtectionRequest) -> ProtectionStatus:
        if self.chaos.unreachable:
            raise ExchangeError("exchange simulé injoignable", client_algo_id=request.client_algo_id)
        protection = _VirtualProtection(
            client_algo_id=request.client_algo_id,
            exchange_algo_id=new_id("vxa"),
            inst_id=request.inst_id,
            side=Side(request.side),
            contracts=dec(request.contracts, field="contracts"),
            trigger_price=dec(request.trigger_price, field="trigger_price"),
            trigger_reference=request.trigger_reference,
        )
        self._protections[protection.client_algo_id] = protection
        return self._protection_status(protection)

    def _protection_status(self, protection: _VirtualProtection) -> ProtectionStatus:
        return ProtectionStatus(
            client_algo_id=protection.client_algo_id,
            exchange_algo_id=protection.exchange_algo_id,
            state=protection.state,
            trigger_price=protection.trigger_price,
            contracts=protection.contracts,
            as_of=self._now(),
            raw={"trigger_reference": protection.trigger_reference},
        )

    async def cancel_protection(
        self, account_scope: str, client_algo_id: str, inst_id: str
    ) -> ProtectionStatus:
        protection = self._protections.get(client_algo_id)
        if protection is None:
            raise ExchangeError("protection introuvable", client_algo_id=client_algo_id)
        if protection.state == "live":
            protection.state = "canceled"
        return self._protection_status(protection)

    async def protections_list(self) -> list[ProtectionStatus]:
        return [self._protection_status(p) for p in self._protections.values()]

    async def protections(self, account_scope: str) -> list[ProtectionStatus]:
        return await self.protections_list()

    # --- flux d'événements --------------------------------------------------------------------------

    def drain(self, until: datetime | None = None) -> list[ExchangeEvent]:
        """Événements dont l'heure de réception est atteinte (ordre chronologique stable)."""
        limit = until or self._now()
        out: list[ExchangeEvent] = []
        while self._events and self._events[0][0] <= limit:
            out.append(heapq.heappop(self._events)[2])
        return out

    async def events(self) -> AsyncIterator[ExchangeEvent]:
        for event in self.drain():
            yield event

    # --- moteur de marché ---------------------------------------------------------------------------

    def on_market_event(
        self, *, trades: Sequence[Trade] = (), books: Mapping[str, BookView] | None = None
    ) -> None:
        """Avance la simulation : arrivées d'ordres, files maker, annulations, protections, CAA."""
        now = self._now()
        self._release_reservations_on_new_book(books)
        self._expire_cancel_all_after(now)
        for order in list(self.orders.values()):
            if order.phase is _Phase.PENDING_ARRIVAL and order.arrives_at <= now:
                self._on_arrival(order, now)
        for trade in trades:
            self._apply_trade_to_queues(trade, now)
        self._trigger_protections(now)
        self._apply_effective_cancels(now)

    def _release_reservations_on_new_book(self, books: Mapping[str, BookView] | None) -> None:
        """Une nouvelle version de carnet = profondeur réellement réobservée : la réservation tombe."""
        views = books if books is not None else self.market.book_views()
        for inst_id, view in views.items():
            if self._book_versions.get(inst_id) != view.version:
                self._book_versions[inst_id] = view.version
                self.reservation.release_instrument(inst_id)

    def _expire_cancel_all_after(self, now: datetime) -> None:
        """Cancel All After annule les ORDRES en attente. Il ne ferme AUCUNE position (T60)."""
        if self._cancel_all_after_deadline is None or now < self._cancel_all_after_deadline:
            return
        self._cancel_all_after_deadline = None
        self._caa_fired += 1
        for order in list(self.orders.values()):
            if order.phase is not _Phase.DONE:
                self._finish(order, OrderState.CANCELED, now, reason="cancel_all_after")

    def _on_arrival(self, order: VirtualOrder, now: datetime) -> None:
        view = self.market.book_view(order.inst_id)
        if view is None or not view.valid:
            self._finish(order, OrderState.REJECTED, now, reason="carnet indisponible à l'arrivée")
            return
        if order.order_type is OrderKind.POST_ONLY:
            if order.price_limit is None:
                self._finish(order, OrderState.REJECTED, now, reason="post-only sans prix")
                return
            if post_only_would_cross(order.side, order.price_limit, view.best_bid, view.best_ask):
                # Rejet documenté : PAS de frais maker artificiels (§33.2).
                self._finish(order, OrderState.REJECTED, now, reason="post-only croiserait le carnet")
                return
            levels = view.bids if order.side is Side.BUY else view.asks
            displayed = next((q for p, q in levels if p == order.price_limit), ZERO)
            order.queue = QueueState(
                inst_id=order.inst_id,
                side=order.side,
                price=order.price_limit,
                remaining=order.contracts,
                ahead=initial_ahead(displayed, self.queue_assumption),
                assumption=self.queue_assumption,
            )
            order.phase = _Phase.RESTING
            return
        # IOC / LIMIT / MARKET : consommation immédiate du carnet observable À L'ARRIVÉE.
        opposite = view.asks if order.side is Side.BUY else view.bids
        result = match_aggressive(
            inst_id=order.inst_id,
            side=order.side,
            contracts=order.remaining,
            price_limit=order.price_limit,
            opposite_levels=opposite,
            reservation=self.reservation,
        )
        for level in result.fills:
            self._register_fill(order, level.price, level.contracts, Liquidity.TAKER, now)
        if order.remaining > 0:
            if order.order_type in (OrderKind.IOC, OrderKind.MARKET):
                # Reliquat annulé : jamais un fill au dernier prix (T29).
                self._finish(order, OrderState.CANCELED, now, reason=result.reason or "reliquat IOC annulé")
            else:
                order.phase = _Phase.RESTING
                order.queue = QueueState(
                    inst_id=order.inst_id,
                    side=order.side,
                    price=order.price_limit if order.price_limit is not None else (view.mid or ZERO),
                    remaining=order.remaining,
                    ahead=ZERO,
                    assumption=self.queue_assumption,
                )
        else:
            self._finish(order, OrderState.FILLED, now)

    def _apply_trade_to_queues(self, trade: Trade, now: datetime) -> None:
        for order in list(self.orders.values()):
            if order.phase is not _Phase.RESTING or order.queue is None or order.inst_id != trade.inst_id:
                continue
            filled = order.queue.on_trade(trade.price, trade.contracts, trade.taker_side)
            if filled > 0:
                self._register_fill(order, order.queue.price, filled, Liquidity.MAKER, now)
                if order.remaining <= 0:
                    self._finish(order, OrderState.FILLED, now)

    def _apply_effective_cancels(self, now: datetime) -> None:
        for order in list(self.orders.values()):
            if order.phase is _Phase.DONE or order.cancel_effective_at is None:
                continue
            if order.cancel_effective_at <= now:
                # Les fills survenus AVANT cet instant restent acquis : rien n'est rétroactif.
                self._finish(order, OrderState.CANCELED, now, reason="annulation effective")

    def _trigger_protections(self, now: datetime) -> None:
        for protection in self._protections.values():
            if protection.state != "live":
                continue
            mark = self._mark(protection.inst_id)
            if mark is None:
                continue
            hit = (
                mark <= protection.trigger_price
                if protection.side is Side.SELL
                else mark >= protection.trigger_price
            )
            if not hit:
                continue
            protection.state = "triggered"
            protection.triggered_at = now
            self._register_protection_exit(protection, mark, now)
            self._emit(
                ExchangeEvent(
                    kind="protection", receive_ts=now, protection=self._protection_status(protection)
                ),
                at=now,
            )

    def _register_protection_exit(
        self, protection: _VirtualProtection, price: Decimal, now: datetime
    ) -> None:
        position = self._positions.get(protection.inst_id)
        if position is None or position.is_flat:
            return
        spec = self._spec(protection.inst_id)
        contracts = min(protection.contracts, abs(position.signed_base_qty) / spec.base_units_per_contract)
        if contracts <= 0:
            return
        synthetic = VirtualOrder(
            client_order_id=f"prot:{protection.client_algo_id}",
            exchange_order_id=protection.exchange_algo_id,
            inst_id=protection.inst_id,
            side=protection.side,
            order_type=OrderKind.MARKET,
            contracts=contracts,
            price_limit=None,
            reduce_only=True,
            created_at=now,
            arrives_at=now,
            phase=_Phase.RESTING,
        )
        self.orders.setdefault(synthetic.client_order_id, synthetic)
        self._register_fill(synthetic, price, contracts, Liquidity.TAKER, now)
        self._finish(synthetic, OrderState.FILLED, now)

    # --- comptabilité interne -----------------------------------------------------------------------

    def _register_fill(
        self, order: VirtualOrder, price: Decimal, contracts: Decimal, liquidity: Liquidity, now: datetime
    ) -> None:
        spec = self._spec(order.inst_id)
        signed_base = contracts * spec.base_units_per_contract * Decimal(order.side.sign)
        notional = contracts * spec.base_units_per_contract * price
        rate = self.fee_maker if liquidity is Liquidity.MAKER else self.fee_taker
        fee_cashflow = -(notional.copy_abs() * rate)  # débit : signe négatif, jamais abs() (§37)
        position = self._positions.get(order.inst_id) or Position(
            inst_id=order.inst_id, base_ccy=spec.base_ccy, settle_ccy=spec.settle_ccy
        )
        new_position, split = apply_fill(position, signed_base, price)
        self._positions[order.inst_id] = new_position
        self.realized_pnl += split.realized_pnl
        self.fees_paid += -fee_cashflow
        self.cash += split.realized_pnl + fee_cashflow
        order.filled += contracts
        order.notional += notional
        order.state = OrderState.FILLED if order.remaining <= 0 else OrderState.PARTIALLY_FILLED

        fill = Fill(
            execution_key=f"{self.account_scope}:{order.inst_id}:{order.exchange_order_id}:{new_id('trd')}",
            account_scope=self.account_scope,
            inst_id=order.inst_id,
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
            trade_id=new_id("vtr"),
            side=order.side,
            contracts=contracts,
            fill_price=price,
            fee_cashflow=fee_cashflow,
            fee_ccy=spec.settle_ccy,
            fill_at=now,
            receive_ts=now + self.latency.delay(LatencyKind.EVENT, order.client_order_id),
            liquidity=liquidity,
        )
        receive = fill.receive_ts
        if order.client_order_id in self.chaos.fill_before_ack:
            receive = order.created_at  # le fill précède l'ACK local (T35)
        for _ in range(self.chaos.duplicate_fills):
            self._emit(ExchangeEvent(kind="fill", receive_ts=receive, fill=fill), at=receive)
        self._emit(
            ExchangeEvent(
                kind="order",
                receive_ts=receive,
                order_event=OrderEvent(
                    event_id=new_id("oev"),
                    client_order_id=order.client_order_id,
                    exchange_order_id=order.exchange_order_id,
                    event_kind=OrderEventKind.FILL if order.remaining <= 0 else OrderEventKind.PARTIAL_FILL,
                    observed_state=order.state,
                    cumulative_filled=order.filled,
                    average_fill_price=order.average_price,
                    event_ts=now,
                    receive_ts=receive,
                ),
            ),
            at=receive,
        )

    def _finish(
        self, order: VirtualOrder, state: OrderState, now: datetime, *, reason: str | None = None
    ) -> None:
        if order.phase is _Phase.DONE:
            return
        order.phase = _Phase.DONE
        order.state = state
        order.reject_reason = reason
        if order.queue is not None:
            order.queue.cancel_remaining()
        kind = {
            OrderState.FILLED: OrderEventKind.FILL,
            OrderState.CANCELED: OrderEventKind.CANCEL,
            OrderState.REJECTED: OrderEventKind.REJECT,
            OrderState.EXPIRED: OrderEventKind.EXPIRE,
        }.get(state, OrderEventKind.UNKNOWN)
        receive = now + self.latency.delay(LatencyKind.EVENT, order.client_order_id)
        self._emit(
            ExchangeEvent(
                kind="order",
                receive_ts=receive,
                order_event=OrderEvent(
                    event_id=new_id("oev"),
                    client_order_id=order.client_order_id,
                    exchange_order_id=order.exchange_order_id,
                    event_kind=kind,
                    observed_state=state,
                    cumulative_filled=order.filled,
                    average_fill_price=order.average_price,
                    event_ts=now,
                    receive_ts=receive,
                    reason=reason,
                ),
            ),
            at=receive,
        )

    # --- clôture -------------------------------------------------------------------------------------

    def settle_funding(self, inst_id: str, realized_rate: Decimal, reference_price: Decimal) -> Decimal:
        """Flux de funding au règlement TRAVERSÉ, avec la position détenue à cet instant (§37, T25)."""
        position = self._positions.get(inst_id)
        if position is None or position.is_flat:
            return ZERO
        signed_notional = position.signed_base_qty * dec(reference_price, field="reference_price")
        cashflow = -(signed_notional * dec(realized_rate, field="realized_rate"))
        self.cash += cashflow
        self.funding_paid += -cashflow
        return cashflow

    def close_out(self) -> dict[str, Any]:
        """Clôture : PnL MARQUÉ et PnL après liquidation simulée, publiés SÉPARÉMENT (§33.3)."""
        marked_equity = self.equity()
        residual: list[dict[str, str]] = []
        liquidation_cash = self.cash
        for inst_id, position in self._positions.items():
            if position.is_flat:
                continue
            spec = self._spec(inst_id)
            view = self.market.book_view(inst_id)
            contracts = abs(position.signed_base_qty) / spec.base_units_per_contract
            side = Side.SELL if position.signed_base_qty > 0 else Side.BUY
            levels = (view.bids if side is Side.SELL else view.asks) if view and view.valid else ()
            result = (
                match_aggressive(
                    inst_id=inst_id,
                    side=side,
                    contracts=contracts,
                    price_limit=None,
                    opposite_levels=levels,
                    reservation=None,
                )
                if levels
                else None
            )
            if result is None or result.filled <= 0:
                residual.append(
                    {
                        "inst_id": inst_id,
                        "signed_base_qty": format(position.signed_base_qty, "f"),
                        "reason": "aucune liquidité observable : sortie impossible, exposition résiduelle",
                    }
                )
                continue
            exit_price = result.vwap or ZERO
            _, split = apply_fill(position, -position.signed_base_qty, exit_price)
            fee = abs(result.filled * spec.base_units_per_contract * exit_price) * self.fee_taker
            liquidation_cash += split.realized_pnl - fee
            if result.remaining > 0:
                residual.append(
                    {
                        "inst_id": inst_id,
                        "remaining_contracts": format(result.remaining, "f"),
                        "reason": "profondeur insuffisante pour liquider entièrement",
                    }
                )
        return {
            "marked_equity": format(marked_equity, "f"),
            "liquidated_equity": format(liquidation_cash, "f"),
            "cash": format(self.cash, "f"),
            "realized_pnl": format(self.realized_pnl, "f"),
            "fees_paid": format(self.fees_paid, "f"),
            "funding_paid": format(self.funding_paid, "f"),
            "residual_exposure": residual,
            "cancel_all_after_fired": self._caa_fired,
            "ambiguous_paths": list(self.ambiguous_paths),
        }
