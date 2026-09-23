import asyncio
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from hermes.config import ExecutionConfig
from hermes.execution.broker import PaperBroker, Position
from hermes.execution.okx.client import Credentials, OKXClient, OKXError
from hermes.execution.okx.instruments import Instrument, binance_price_factor, okx_inst_id
from hermes.execution.okx_broker import OKXBroker

INST = {
    "instId": "BTC-USDT-SWAP",
    "ctVal": "0.01",
    "ctMult": "1",
    "lotSz": "0.01",
    "minSz": "0.01",
    "tickSz": "0.1",
    "maxLmtSz": "10000",
    "maxMktSz": "3000",
    "state": "live",
    "lever": "100",
}


def test_signature_matches_spec():
    sig = OKXClient.sign("secret", "2020-12-08T09:08:57.715Z", "get", "/api/v5/account/balance?ccy=BTC", "")
    expected = base64.b64encode(
        hmac.new(b"secret", b"2020-12-08T09:08:57.715ZGET/api/v5/account/balance?ccy=BTC", hashlib.sha256).digest()
    ).decode()
    assert sig == expected


def test_instrument_rounding_and_mapping():
    inst = Instrument.from_okx(INST)
    assert inst.round_size_down(0.019) == 0.01
    assert inst.round_size_down(-0.029) == -0.02
    assert inst.round_size_down(0.009) == 0.0  # below min size
    assert inst.round_price(100.05, "buy", passive=True) == 100.0
    assert inst.round_price(100.05, "sell", passive=True) == 100.1
    assert inst.fmt_size(0.02) == "0.02" and inst.fmt_price(100.0) == "100.0"
    assert okx_inst_id("1000PEPEUSDT") == "PEPE-USDT-SWAP" and binance_price_factor("1000PEPEUSDT") == 1000
    assert binance_price_factor("BTCUSDT") == 1.0
    # 10 000 USDT at 50 000 = 0.2 BTC = 20 contracts of 0.01
    assert abs(inst.contracts_for_notional(10_000, 50_000) - 20) < 1e-9


class FakeOKX:
    """Minimal in-memory OKX: post_only fills after ``fill_after`` polls (or never), IOC fills at once."""

    def __init__(self, fill_after: int | None = 1, reject_first_post_only: bool = False):
        self.orders: dict[str, dict] = {}
        self.polls: dict[str, int] = {}
        self.fill_after = fill_after
        self.reject_first = reject_first_post_only
        self.calls: list[str] = []
        self.headers: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        self.headers.append(dict(request.headers))
        ok = lambda data: httpx.Response(200, json={"code": "0", "msg": "", "data": data})  # noqa: E731
        if path == "/api/v5/public/time":
            return ok([{"ts": "1700000000000"}])
        if path == "/api/v5/market/books":
            return ok([{"bids": [["100.0", "5", "0", "1"]], "asks": [["100.1", "5", "0", "1"]]}])
        if path == "/api/v5/trade/order" and request.method == "POST":
            body = json.loads(request.content)
            if body["ordType"] == "post_only" and self.reject_first:
                self.reject_first = False
                self.orders[body["clOrdId"]] = {**body, "state": "canceled", "accFillSz": "0", "avgPx": ""}
                return ok([{"clOrdId": body["clOrdId"], "sCode": "0"}])
            state = "filled" if body["ordType"] == "ioc" else "live"
            fill = body["sz"] if state == "filled" else "0"
            px = body["px"]
            self.orders[body["clOrdId"]] = {**body, "state": state, "accFillSz": fill, "avgPx": px, "fee": "-0.01"}
            return ok([{"clOrdId": body["clOrdId"], "sCode": "0"}])
        if path == "/api/v5/trade/order" and request.method == "GET":
            clid = request.url.params["clOrdId"]
            o = self.orders.get(clid)
            if o is None:
                return httpx.Response(200, json={"code": "51603", "msg": "no order", "data": []})
            self.polls[clid] = self.polls.get(clid, 0) + 1
            if o["state"] == "live" and self.fill_after is not None and self.polls[clid] >= self.fill_after:
                o.update(state="filled", accFillSz=o["sz"], avgPx=o["px"])
            return ok([o])
        if path == "/api/v5/trade/amend-order":
            body = json.loads(request.content)
            self.orders[body["clOrdId"]]["px"] = body["newPx"]
            return ok([{"sCode": "0"}])
        if path == "/api/v5/trade/cancel-order":
            body = json.loads(request.content)
            if self.orders[body["clOrdId"]]["state"] == "live":
                self.orders[body["clOrdId"]]["state"] = "canceled"
            return ok([{"sCode": "0"}])
        if path == "/api/v5/account/balance":
            return httpx.Response(200, json={"code": "50011", "msg": "Too Many Requests", "data": []})
        return httpx.Response(200, json={"code": "0", "msg": "", "data": []})


def _broker(fake: FakeOKX, **cfg) -> OKXBroker:
    http = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(fake.handler))
    client = OKXClient(Credentials("k", "s", "p"), base_url="https://x", demo=True, client=http)
    ec = ExecutionConfig(venue="okx", chase_interval_s=0.5, maker_timeout_s=cfg.pop("timeout", 3), **cfg)
    b = OKXBroker(client, ec, ["BTCUSDT"], leverage=3)
    inst = Instrument.from_okx(INST)
    b.instruments = {"BTCUSDT": inst}
    b.inst_to_symbol = {inst.inst_id: "BTCUSDT"}
    return b


def test_demo_header_and_error_mapping():
    fake = FakeOKX()
    b = _broker(fake)

    async def run():
        with pytest.raises(OKXError) as e:
            await b.c.request("GET", "/api/v5/account/balance", auth=True, retries=2)
        return e.value

    err = asyncio.run(run())
    assert err.code == "50011"
    assert fake.calls.count("GET /api/v5/account/balance") == 2  # reads are retried
    assert all(h.get("x-simulated-trading") == "1" for h in fake.headers)
    assert "ok-access-sign" in fake.headers[0]


def test_maker_child_fills_passively():
    from hermes.execution.okx_broker import Child

    fake = FakeOKX(fill_after=1)
    b = _broker(fake)
    ch = Child("BTCUSDT", b.instruments["BTCUSDT"], "buy", 0.05, False)
    fills, remaining = asyncio.run(b._execute_child(ch, urgent=False))
    assert remaining == 0 and len(fills) == 1 and fills[0].maker
    assert fills[0].price == 100.0  # posted at the bid
    posted = [o for o in fake.orders.values() if o["ordType"] == "post_only"]
    assert posted and posted[0]["px"] == "100.0"


def test_timeout_falls_back_to_bounded_ioc():
    from hermes.execution.okx_broker import Child

    fake = FakeOKX(fill_after=None)
    b = _broker(fake, timeout=1.2, taker_slippage_cap_bps=10)
    ch = Child("BTCUSDT", b.instruments["BTCUSDT"], "sell", 0.03, True)
    _, remaining = asyncio.run(b._execute_child(ch, urgent=False))
    assert remaining == 0
    ioc = [o for o in fake.orders.values() if o["ordType"] == "ioc"]
    assert len(ioc) == 1 and ioc[0]["reduceOnly"] is True
    assert float(ioc[0]["px"]) >= 100.0 * (1 - 10e-4) - 0.1  # bounded worst price


def test_post_only_rejection_is_reposted():
    from hermes.execution.okx_broker import Child

    fake = FakeOKX(fill_after=1, reject_first_post_only=True)
    b = _broker(fake)
    ch = Child("BTCUSDT", b.instruments["BTCUSDT"], "buy", 0.02, False)
    fills, remaining = asyncio.run(b._execute_child(ch, urgent=False))
    assert remaining == 0 and all(f.maker for f in fills)
    assert sum(1 for o in fake.orders.values() if o["ordType"] == "post_only") == 2


def test_plan_flip_reduces_first_and_rounds_toward_zero():
    b = _broker(FakeOKX())
    # 5 contracts of 0.01 BTC at 100 USDT = 5 USDT long.
    pos = {"BTCUSDT": Position("BTCUSDT", 5.0, 5.0, 100.0, 100.0)}
    # Flip to short 7.919 USDT -> -7.919 contracts, rounded toward zero to the 0.01 lot: -7.91.
    kids = b.plan({"BTCUSDT": -7.919}, pos, {"BTCUSDT": 100.0})
    assert [k.reduce_only for k in kids] == [True, False]
    assert kids[0].side == "sell" and abs(kids[0].contracts - 5.0) < 1e-12
    assert kids[1].side == "sell" and abs(kids[1].contracts - 7.91) < 1e-9
    none = b.plan({"BTCUSDT": 5.0}, pos, {"BTCUSDT": 100.0})
    assert none == []


def test_paper_broker_accounting(tmp_path):
    pb = PaperBroker(
        tmp_path / "acc.json",
        10_000,
        maker_fee=0.0002,
        taker_fee=0.0005,
        maker_share=0.5,
        half_spread=0.0,
        slippage=0.0,
    )
    pb.set_prices({"BTCUSDT": 100.0})

    async def run():
        await pb.rebalance({"BTCUSDT": 5_000.0})
        eq1 = await pb.equity()
        pb.set_prices({"BTCUSDT": 110.0})
        eq2 = await pb.equity()
        paid = pb.accrue_funding({"BTCUSDT": 0.001})
        await pb.rebalance({})
        return eq1, eq2, paid, await pb.equity()

    eq1, eq2, paid, _ = asyncio.run(run())
    fee = 5000 * (0.5 * 0.0002 + 0.5 * 0.0005)
    assert abs(eq1 - (10_000 - fee)) < 1e-6
    assert abs(eq2 - eq1 - 500) < 1e-6
    assert abs(paid - 5.5) < 1e-6  # long pays positive funding on 5 500 notional
    # Restart keeps the book.
    pb2 = PaperBroker(tmp_path / "acc.json", 1.0, 0.0002, 0.0005)
    assert abs(pb2.cash - pb.cash) < 1e-9 and not pb2.qty


def test_rounding_absorbs_float_residue():
    inst = Instrument.from_okx({**INST, "lotSz": "0.1", "minSz": "0.1"})
    assert inst.round_size_down(0.3 - 0.1) == 0.2  # 0.19999999999999998 is two lots, not one
    assert inst.round_size_down(0.1 + 0.2) == 0.3


class AmbiguousOKX(FakeOKX):
    """The first POST reaches the exchange (the order exists) but the answer is lost: HTTP 502."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.lost_answer = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        resp = super().handler(request)
        if request.url.path == "/api/v5/trade/order" and request.method == "POST" and self.lost_answer:
            self.lost_answer = False
            return httpx.Response(502, text="bad gateway")
        return resp


def test_ambiguous_post_is_reconciled_not_forgotten():
    from hermes.execution.okx_broker import Child

    fake = AmbiguousOKX(fill_after=1)
    b = _broker(fake)
    ch = Child("BTCUSDT", b.instruments["BTCUSDT"], "buy", 0.05, False)
    fills, remaining = asyncio.run(b._execute_child(ch, urgent=False))
    # The order that the exchange did accept is tracked and filled; no second order is sent on top of it.
    assert remaining == 0 and sum(f.qty for f in fills) == 0.05
    assert sum(1 for o in fake.orders.values() if o["ordType"] == "post_only") == 1


class StopsOKX(FakeOKX):
    def handler(self, request: httpx.Request) -> httpx.Response:
        ok = lambda data: httpx.Response(200, json={"code": "0", "msg": "", "data": data})  # noqa: E731
        path = request.url.path
        if path == "/api/v5/trade/orders-algo-pending":
            self.calls.append(f"{request.method} {path}")
            return ok([{"instId": "ETH-USDT-SWAP", "algoId": "1", "side": "sell", "tag": "hermes"}])
        return super().handler(request)


def test_protect_never_cancels_stops_it_cannot_relate_to_a_position():
    fake = StopsOKX()
    b = _broker(fake)
    asyncio.run(b.protect({}))
    assert not any("cancel-algos" in c for c in fake.calls)


def test_unknown_instrument_position_is_mapped_and_closable():
    class PosOKX(FakeOKX):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v5/account/positions":
                row = {"instId": "ETH-USDT-SWAP", "pos": "3", "markPx": "2000", "avgPx": "1900"}
                return httpx.Response(200, json={"code": "0", "msg": "", "data": [row]})
            return super().handler(request)

    b = _broker(PosOKX())
    b.load_catalog([{**INST, "instId": "ETH-USDT-SWAP"}])
    pos = asyncio.run(b.positions())
    assert "ETHUSDT" in pos and pos["ETHUSDT"].contracts == 3
    # A model symbol registered later for the same contract takes over the derived mapping.
    assert b.register(["ETHUSDT"]) == ["ETHUSDT"]
    kids = b.plan({}, pos, {"ETHUSDT": 2000.0})
    assert len(kids) == 1 and kids[0].reduce_only and kids[0].side == "sell"


class StuckOKX(FakeOKX):
    """A post_only order that partially fills and whose cancel is never confirmed."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v5/trade/cancel-order":
            self.calls.append("POST cancel (ignored)")
            return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"sCode": "0"}]})
        resp = super().handler(request)
        if path == "/api/v5/trade/order" and request.method == "GET":
            for o in self.orders.values():
                if o["ordType"] == "post_only":
                    o.update(state="partially_filled", accFillSz="0.02", avgPx=o["px"])
            clid = request.url.params["clOrdId"]
            o = self.orders.get(clid)
            if o is not None:
                return httpx.Response(200, json={"code": "0", "msg": "", "data": [o]})
        return resp


def test_unconfirmed_cancel_stops_the_child_and_keeps_its_fills():
    from hermes.execution.okx_broker import Child, ChildError

    fake = StuckOKX(fill_after=None)
    b = _broker(fake, timeout=1.2)
    ch = Child("BTCUSDT", b.instruments["BTCUSDT"], "buy", 0.05, False)
    with pytest.raises(ChildError) as e:
        asyncio.run(b._execute_child(ch, urgent=False))
    assert sum(f.qty for f in e.value.fills) == pytest.approx(0.02)
    assert e.value.remaining == pytest.approx(0.03)
    assert not any(o["ordType"] == "ioc" for o in fake.orders.values()), "no IOC on top of a live order"
    # The report of a rebalance keeps those fills.
    rep = asyncio.run(b.rebalance({"BTCUSDT": 5.0}))
    assert all(isinstance(x, str) for x in rep.errors)


class LostOrderOKX(FakeOKX):
    """The POST times out (HTTP 504) and the order never shows up: the child must stop, not resend."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/trade/order" and request.method == "POST":
            self.calls.append("POST order (lost)")
            return httpx.Response(504, text="gateway timeout")
        return super().handler(request)


def test_unconfirmed_ambiguous_send_aborts_the_child():
    from hermes.execution.okx_broker import Child, ChildError

    fake = LostOrderOKX()
    b = _broker(fake)
    ch = Child("BTCUSDT", b.instruments["BTCUSDT"], "buy", 0.05, False)
    with pytest.raises(ChildError):
        asyncio.run(b._execute_child(ch, urgent=False))
    assert fake.calls.count("POST order (lost)") == 1


def test_paper_broker_hold_and_marks_survive_restart(tmp_path):
    pb = PaperBroker(tmp_path / "acc.json", 10_000, 0.0, 0.0, maker_share=1.0, half_spread=0.0, slippage=0.0)
    pb.set_prices({"BTCUSDT": 100.0, "ETHUSDT": 10.0})

    async def run():
        await pb.rebalance({"BTCUSDT": 1_000.0, "ETHUSDT": -500.0})
        pb.set_prices({"BTCUSDT": 120.0})
        await pb.rebalance({"BTCUSDT": 1_000.0}, hold={"ETHUSDT"})  # ETH has no price this bar: untouched
        return await pb.positions()

    pos = asyncio.run(run())
    assert pos["ETHUSDT"].contracts == pytest.approx(-50.0)
    pb2 = PaperBroker(tmp_path / "acc.json", 1.0, 0.0, 0.0)
    assert pb2.prices["BTCUSDT"] == 120.0  # valued at the last mark after a restart, not at entry


def test_paper_stops_trigger_like_the_exchange(tmp_path):
    pb = PaperBroker(tmp_path / "acc.json", 10_000, 0.0, 0.0005, maker_share=1.0, half_spread=0.0, slippage=0.0)
    pb.set_prices({"AUSDT": 100.0, "BUSDT": 50.0})

    async def run():
        await pb.rebalance({"AUSDT": 1_000.0, "BUSDT": -500.0})
        await pb.protect({"AUSDT": 0.10, "BUSDT": 0.10})  # long stop at 90, short stop at 55

    asyncio.run(run())
    assert pb.stops["AUSDT"] == (1.0, 90.0) and pb.stops["BUSDT"][1] == pytest.approx(55.0)
    asyncio.run(pb.protect({"AUSDT": 0.5}))  # an existing stop is never moved while the side is kept
    assert pb.stops["AUSDT"] == (1.0, 90.0)
    # A bar that trades through the long stop, and gaps through the short one (opens at 58).
    fills = pb.check_stops(
        high={"AUSDT": 101.0, "BUSDT": 60.0}, low={"AUSDT": 88.0, "BUSDT": 57.0}, open_={"AUSDT": 99.0, "BUSDT": 58.0}
    )
    px = {f.symbol: f.price for f in fills}
    assert px["AUSDT"] == pytest.approx(90.0) and px["BUSDT"] == pytest.approx(58.0)
    assert not pb.qty and not pb.stops and all(not f.maker for f in fills)


def test_catalog_keeps_crypto_swaps_only():
    b = _broker(FakeOKX())
    b.instruments, b.inst_to_symbol = {}, {}
    equity = {**INST, "instId": "BB-USDT-SWAP", "instCategory": "3"}
    crypto = {**INST, "instId": "ETH-USDT-SWAP", "instCategory": "1"}
    b.load_catalog([INST, equity, crypto])
    assert set(b.catalog) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    assert b.register(["BBUSDT", "ETHUSDT"]) == ["ETHUSDT"]


def test_position_on_a_non_crypto_swap_stays_visible_and_is_closed():
    # The model never trades equity swaps, but one already on the account (manual trade, older version)
    # must stay visible, under its OKX id (BB-USDT-SWAP is BlackBerry, not Binance's BBUSDT), and be closed.
    class PosOKX(FakeOKX):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v5/account/positions":
                row = {"instId": "BB-USDT-SWAP", "pos": "-4", "markPx": "5", "avgPx": "5.2"}
                return httpx.Response(200, json={"code": "0", "msg": "", "data": [row]})
            return super().handler(request)

    b = _broker(PosOKX())
    b.instruments, b.inst_to_symbol = {}, {}
    b.load_catalog([{**INST, "instId": "BB-USDT-SWAP", "instCategory": "3"}])
    assert b.register(["BBUSDT"]) == []  # never tradable by the model
    pos = asyncio.run(b.positions())
    assert list(pos) == ["BB-USDT-SWAP"] and pos["BB-USDT-SWAP"].contracts == -4
    kids = b.plan({}, pos, {"BB-USDT-SWAP": 5.0})
    assert len(kids) == 1 and kids[0].reduce_only and kids[0].side == "buy"
