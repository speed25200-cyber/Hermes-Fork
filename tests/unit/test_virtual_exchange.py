"""Simulateur : fills maker et agressifs, doublons, ACK perdu, annulations, protections, CAA
(T27–T32, T34, T35, T38, T60, T62)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from okxq.backtest.latency import LatencyModel
from okxq.backtest.market_state import BookView, MarketState, Trade
from okxq.backtest.queue_models import QueueAssumption
from okxq.backtest.virtual_exchange import ChaosOptions, VirtualExchange
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import ExchangeError
from okxq.domain.instruments import InstrumentSpec, InstrumentState
from okxq.domain.money import Side
from okxq.exchange.base import OrderRequest, PlaceOutcome, ProtectionRequest

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INST = "TEST-USDT-SWAP"


def spec() -> InstrumentSpec:
    return InstrumentSpec(
        inst_id=INST,
        valid_from=T0 - timedelta(days=1),
        observed_at=T0,
        settle_ccy="USDT",
        base_ccy="TEST",
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        lot_size=Decimal("1"),
        min_size=Decimal("1"),
        state=InstrumentState.LIVE,
        provenance="test",
    )


def market(
    *, bids=((Decimal("100.0"), Decimal("5")),), asks=((Decimal("100.1"), Decimal("3")),)
) -> MarketState:
    m = MarketState()
    m.specs[INST] = spec()
    m.books[INST] = _FakeBook(bids, asks)
    m.marks[INST] = Decimal("100.05")
    return m


class _FakeBook:
    """Carnet de test : expose la même ``view()`` que celui du replay, sans passer par des événements."""

    def __init__(self, bids, asks) -> None:
        self._bids, self._asks = tuple(bids), tuple(asks)

    def view(self) -> BookView:
        return BookView(INST, T0, 1, True, self._bids, self._asks)


def exchange(m: MarketState, clock: SimulatedClock, **kwargs) -> VirtualExchange:
    return VirtualExchange(
        clock=clock,
        market=m,
        account_scope="test",
        initial_cash=Decimal("10000"),
        latency=kwargs.pop("latency", LatencyModel.zero()),
        **kwargs,
    )


def request(**over) -> OrderRequest:
    base = dict(
        account_scope="test",
        client_order_id="cl_1",
        inst_id=INST,
        side="buy",
        contracts=Decimal("2"),
        price_limit=Decimal("100.1"),
        order_type="ioc",
        reduce_only=False,
    )
    base.update(over)
    return OrderRequest(**base)  # type: ignore[arg-type]


async def test_T29_ioc_partial_fill_cancels_the_remainder():
    clock = SimulatedClock(T0)
    m = market(asks=((Decimal("100.1"), Decimal("1")),))  # profondeur insuffisante
    ex = exchange(m, clock)
    response = await ex.place_order(request(contracts=Decimal("5")))
    assert response.outcome is PlaceOutcome.ACK
    ex.on_market_event()
    order = ex.orders["cl_1"]
    assert order.filled == Decimal("1")  # jamais un fill au dernier prix pour le reliquat
    assert order.state.value == "CANCELED"
    fills = [e.fill for e in ex.drain() if e.fill is not None]
    assert len(fills) == 1 and fills[0].contracts == Decimal("1")


async def test_T30_two_orders_never_consume_the_same_depth():
    clock = SimulatedClock(T0)
    m = market(asks=((Decimal("100.1"), Decimal("3")),))
    ex = exchange(m, clock)
    await ex.place_order(request(client_order_id="cl_a", contracts=Decimal("3")))
    await ex.place_order(request(client_order_id="cl_b", contracts=Decimal("3")))
    ex.on_market_event()
    total = ex.orders["cl_a"].filled + ex.orders["cl_b"].filled
    assert total == Decimal("3")  # la profondeur affichée n'est pas allouée deux fois
    assert ex.orders["cl_b"].filled == Decimal("0")


async def test_T27_T28_maker_order_needs_volume_not_a_touch():
    clock = SimulatedClock(T0)
    m = market()
    ex = exchange(m, clock, queue_assumption=QueueAssumption.PESSIMISTIC)
    await ex.place_order(
        request(order_type="post_only", price_limit=Decimal("100.0"), contracts=Decimal("2"))
    )
    ex.on_market_event()
    order = ex.orders["cl_1"]
    assert order.filled == 0  # le prix est « touché » mais rien n'a été échangé
    assert ex._positions.get(INST) is None  # aucune position fictive
    # Un trade au prix, du côté vendeur, ne suffit pas tant que la file devant nous n'est pas purgée.
    ex.on_market_event(trades=[Trade(INST, "t1", Decimal("100.0"), Decimal("3"), Side.SELL, T0, T0)])
    assert ex.orders["cl_1"].filled == 0  # 5 contrats affichés devant (hypothèse pessimiste)
    ex.on_market_event(trades=[Trade(INST, "t2", Decimal("100.0"), Decimal("4"), Side.SELL, T0, T0)])
    assert ex.orders["cl_1"].filled == Decimal("2")
    fills = [e.fill for e in ex.drain() if e.fill is not None]
    assert fills and fills[-1].liquidity.value == "maker"


async def test_post_only_that_would_cross_is_rejected_without_maker_fees():
    clock = SimulatedClock(T0)
    ex = exchange(market(), clock)
    await ex.place_order(request(order_type="post_only", price_limit=Decimal("100.5")))
    ex.on_market_event()
    order = ex.orders["cl_1"]
    assert order.state.value == "REJECTED" and order.filled == 0
    assert "croiserait" in (order.reject_reason or "")
    assert ex.fees_paid == 0


async def test_T32_lost_ack_leaves_the_order_existing_and_raises():
    clock = SimulatedClock(T0)
    ex = exchange(market(), clock, chaos=ChaosOptions(lose_ack=frozenset({"cl_1"})))
    with pytest.raises(ExchangeError):
        await ex.place_order(request())
    # L'ordre EXISTE côté exchange : renvoyer aveuglément créerait un doublon.
    assert "cl_1" in ex.orders
    assert ex.ambiguous_paths and "ACK perdu" in ex.ambiguous_paths[0]["reason"]
    status = await ex.get_order("test", "cl_1", INST)
    assert status is not None and status.state == "live"


async def test_T34_duplicate_fills_are_delivered_and_deduplicated_downstream():
    clock = SimulatedClock(T0)
    ex = exchange(market(), clock, chaos=ChaosOptions(duplicate_fills=3))
    await ex.place_order(request(contracts=Decimal("2")))
    ex.on_market_event()
    fills = [e.fill for e in ex.drain() if e.fill is not None]
    assert len(fills) == 3
    assert len({f.execution_key for f in fills}) == 1  # même clé : un seul effet comptable en aval


async def test_T35_fill_can_arrive_before_the_local_ack():
    clock = SimulatedClock(T0)
    ex = exchange(
        market(), clock, latency=LatencyModel(), chaos=ChaosOptions(fill_before_ack=frozenset({"cl_1"}))
    )
    response = await ex.place_order(request())
    clock.advance(timedelta(seconds=1))
    ex.on_market_event()
    events = ex.drain()
    fill_events = [e for e in events if e.fill is not None]
    assert fill_events
    assert response.ack_at is not None
    assert fill_events[0].receive_ts <= response.ack_at


async def test_T31_fill_during_cancellation_counts_once_and_is_not_retroactive():
    clock = SimulatedClock(T0)
    ex = exchange(market(), clock, latency=LatencyModel())
    await ex.place_order(
        request(order_type="post_only", price_limit=Decimal("100.0"), contracts=Decimal("2"))
    )
    clock.advance(timedelta(milliseconds=200))
    ex.on_market_event()
    await ex.cancel_order("test", "cl_1", INST)
    # Un fill survient AVANT que l'annulation ne prenne effet : il reste acquis.
    ex.on_market_event(trades=[Trade(INST, "t1", Decimal("100.0"), Decimal("9"), Side.SELL, T0, T0)])
    filled_before = ex.orders["cl_1"].filled
    assert filled_before == Decimal("2")
    clock.advance(timedelta(seconds=1))
    ex.on_market_event()
    assert ex.orders["cl_1"].filled == filled_before  # rien n'est effacé rétroactivement
    keys = {e.fill.execution_key for e in ex.drain() if e.fill is not None}
    assert len(keys) == 1


async def test_T38_reduce_only_without_position_is_refused():
    clock = SimulatedClock(T0)
    ex = exchange(market(), clock)
    response = await ex.place_order(request(reduce_only=True, side="sell", price_limit=Decimal("100.0")))
    assert response.outcome is PlaceOutcome.REJECTED and response.code == "51121"
    assert ex._positions.get(INST) is None  # un reduce-only n'ouvre JAMAIS


async def test_T60_cancel_all_after_cancels_orders_but_closes_no_position():
    clock = SimulatedClock(T0)
    m = market(asks=((Decimal("100.1"), Decimal("3")),))
    ex = exchange(m, clock)
    await ex.place_order(request(client_order_id="cl_fill", contracts=Decimal("2")))
    ex.on_market_event()
    position_before = ex._positions[INST].signed_base_qty
    assert position_before > 0
    await ex.place_order(
        request(
            client_order_id="cl_rest",
            order_type="post_only",
            price_limit=Decimal("100.0"),
            contracts=Decimal("1"),
        )
    )
    ex.on_market_event()
    await ex.cancel_all_after(30)
    clock.advance(timedelta(seconds=31))
    ex.on_market_event()
    assert ex.orders["cl_rest"].state.value == "CANCELED"
    assert ex._positions[INST].signed_base_qty == position_before  # la position est intacte
    assert ex.close_out()["cancel_all_after_fired"] == 1


async def test_T62_unreachable_exchange_and_residual_exposure_are_visible():
    clock = SimulatedClock(T0)
    m = market(asks=((Decimal("100.1"), Decimal("3")),))
    ex = exchange(m, clock)
    await ex.place_order(request(contracts=Decimal("2")))
    ex.on_market_event()
    ex.chaos = ChaosOptions(unreachable=True)
    with pytest.raises(ExchangeError):
        await ex.place_order(request(client_order_id="cl_2"))
    with pytest.raises(ExchangeError):
        await ex.cancel_order("test", "cl_1", INST)
    # Sans liquidité observable, la clôture déclare un RÉSIDU : jamais « FLAT » sans preuve.
    m.books[INST] = _FakeBook((), ())
    out = ex.close_out()
    assert out["residual_exposure"] and "sortie impossible" in out["residual_exposure"][0]["reason"]


async def test_protection_triggers_on_mark_and_reduces_position():
    clock = SimulatedClock(T0)
    m = market(asks=((Decimal("100.1"), Decimal("5")),), bids=((Decimal("100.0"), Decimal("5")),))
    ex = exchange(m, clock)
    await ex.place_order(request(contracts=Decimal("3")))
    ex.on_market_event()
    assert ex._positions[INST].signed_base_qty > 0
    await ex.place_protection(
        ProtectionRequest(
            account_scope="test",
            client_algo_id="algo_1",
            inst_id=INST,
            side="sell",
            contracts=Decimal("3"),
            trigger_price=Decimal("99.0"),
            trigger_reference="mark",
        )
    )
    protections = await ex.protections("test")
    assert protections[0].state == "live"
    m.marks[INST] = Decimal("98.5")  # le mark franchit le déclencheur
    ex.on_market_event()
    assert ex._positions[INST].is_flat
    assert (await ex.protections("test"))[0].state == "triggered"


async def test_funding_flows_only_with_a_position_at_settlement():
    clock = SimulatedClock(T0)
    m = market(asks=((Decimal("100.1"), Decimal("5")),))
    ex = exchange(m, clock)
    assert ex.settle_funding(INST, Decimal("0.001"), Decimal("100")) == 0  # aucune position : aucun flux
    await ex.place_order(request(contracts=Decimal("2")))
    ex.on_market_event()
    cash_before = ex.cash
    cashflow = ex.settle_funding(INST, Decimal("0.001"), Decimal("100"))
    assert cashflow < 0  # long payeur avec un taux positif
    assert ex.cash == cash_before + cashflow


async def test_duplicate_client_order_id_is_refused_by_the_exchange():
    clock = SimulatedClock(T0)
    ex = exchange(market(), clock)
    assert (await ex.place_order(request())).outcome is PlaceOutcome.ACK
    second = await ex.place_order(request())
    assert second.outcome is PlaceOutcome.REJECTED and second.code == "51000"
