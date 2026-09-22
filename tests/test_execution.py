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
