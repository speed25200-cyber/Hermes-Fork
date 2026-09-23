"""OKX broker: account setup, reconciliation, maker-first execution, server-side protection.

Execution algorithm per child order (one instrument, one direction):

1. **Passive phase** -- a ``post_only`` limit at the touch (best bid to buy, best ask to sell); every
   ``chase_interval_s`` the order is amended to the new touch if the market moved away (at most
   ``max_chases`` amendments). A post-only order that would have crossed is cancelled by OKX; it is simply
   re-posted at the new touch. Fills are read from the order itself (``accFillSz``), never assumed.
2. **Aggressive phase** -- after ``maker_timeout_s`` whatever remains is sent as an IOC limit at the touch
   plus at most ``taker_slippage_cap_bps``: the worst price is bounded, a thin book cannot run the order.
3. **Urgent** orders (risk reduction after a halt or kill switch) skip the passive phase.

Reductions run before increases (they free margin). A side flip is split into a reduce-only close and a
new opening order. Sizes are rounded toward zero to the lot size.

Protection that survives a crash of this process:

* every open position carries one exchange-side **catastrophe stop** (``conditional`` algo,
  ``closeFraction=1``, market execution, triggered on the mark price) placed ``k`` daily sigmas away;
* the **dead-man switch** (``cancel-all-after``) is refreshed while the engine is alive, so resting orders
  of a dead engine are cancelled by the exchange within a minute.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from hermes.config import ExecutionConfig
from hermes.execution.broker import ExecutionReport, Fill, Position, new_client_id
from hermes.execution.okx.client import OKXClient, OKXError
from hermes.execution.okx.instruments import Instrument, binance_price_factor, okx_inst_id

log = logging.getLogger(__name__)


# Answers after which an order may or may not exist: HTTP 5xx, service unavailable, request timeout
# ("does not indicate success or failure"), system busy, system error.
AMBIGUOUS_CODES = {"50001", "50004", "50013", "50026"}


def ambiguous(exc: OKXError) -> bool:
    code = str(exc.code)
    return code in AMBIGUOUS_CODES or (len(code) == 3 and code.startswith("5"))


TERMINAL = ("filled", "canceled", "mmp_canceled", "missing")


class ChildError(RuntimeError):
    """A child order stopped early; carries what was already executed so no fill is ever lost."""

    def __init__(self, msg: str, fills: list[Fill], remaining: float):
        super().__init__(msg)
        self.fills, self.remaining = fills, remaining


@dataclass
class Child:
    symbol: str
    inst: Instrument
    side: str  # buy / sell
    contracts: float  # positive
    reduce_only: bool


class OKXBroker:
    def __init__(
        self,
        client: OKXClient,
        cfg: ExecutionConfig,
        symbols: list[str] | None,
        leverage: int,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.0005,
    ):
        """``symbols=None`` maps contracts on demand (``register``): any OKX USDT perpetual can be traded."""
        self.c = client
        self.cfg = cfg
        self.symbols = list(symbols) if symbols is not None else None
        self.leverage = leverage
        self.maker_fee, self.taker_fee = maker_fee, taker_fee
        self.catalog: dict[str, Instrument] = {}  # live OKX crypto USDT swaps (what the model may trade), by instId
        self.known: dict[str, Instrument] = {}  # every live USDT swap: a position on any of them stays visible
        self.instruments: dict[str, Instrument] = {}  # model symbol -> instrument
        self.inst_to_symbol: dict[str, str] = {}
        self._derived: set[str] = set()  # symbols inferred from an exchange position, not chosen by the model
        self._leverage_set: set[str] = set()
        self._dms_task: asyncio.Task[None] | None = None

    # -- lifecycle ------------------------------------------------------------------------------------------
    async def start(self) -> None:
        await self.c.sync_clock()
        self.load_catalog(await self.c.instruments())
        if self.symbols is not None:
            self.register(self.symbols)
        conf = await self.c.account_config()
        if str(conf.get("acctLv")) == "1":
            raise RuntimeError("OKX account is in Spot mode (acctLv=1): switch it to Futures or Multi-currency")
        if conf.get("posMode") != "net_mode":
            try:
                await self.c.set_position_mode("net_mode")
            except OKXError as exc:
                raise RuntimeError(f"cannot switch to net_mode (close positions/orders first): {exc}") from exc
        # Resting orders from a previous (crashed) run are stale by definition.
        await self.cancel_own_orders()
        await self.positions()  # maps every instrument that already carries a position
        self._dms_task = asyncio.create_task(self._dead_man_loop())
        log.info("OKX broker ready: %d USDT perpetuals, leverage %dx", len(self.catalog), self.leverage)

    def load_catalog(self, instruments: list[dict[str, Any]]) -> None:
        """Every live crypto USDT swap. Equity/commodity swaps are excluded: BB-USDT-SWAP or ON-USDT-SWAP are
        BlackBerry and ON Semiconductor, not the Binance contracts BBUSDT / ONUSDT the model scored -- the
        research universe (``hermes.data.venue``) applies the same rule."""
        self.catalog, self.known = {}, {}
        for d in instruments:
            inst = Instrument.from_okx(d)
            if inst.inst_id.endswith("-USDT-SWAP") and inst.state == "live" and inst.ct_val > 0:
                self.known[inst.inst_id] = inst  # reconciled and closable (kill switch) whatever its category
                if inst.category == "1":
                    self.catalog[inst.inst_id] = inst

    def register(self, symbols: list[str]) -> list[str]:
        """Map model (Binance) symbols to OKX instruments; returns those that exist on OKX."""
        out = []
        for s in symbols:
            iid = okx_inst_id(s)
            inst = self.catalog.get(iid)
            if inst is None:
                continue
            owner = self.inst_to_symbol.get(iid)
            if owner is not None and owner != s:
                if owner not in self._derived:
                    continue  # two model symbols for one OKX contract: the first registered keeps it
                del self.instruments[owner]
                self._derived.discard(owner)
            self.instruments[s] = inst
            self.inst_to_symbol[iid] = s
            out.append(s)
        return out

    async def _ensure_leverage(self, inst: Instrument) -> None:
        if inst.inst_id in self._leverage_set:
            return
        try:
            await self.c.set_leverage(inst.inst_id, self.leverage, self.cfg.td_mode)
        except OKXError as exc:
            log.warning("set_leverage %s: %s", inst.inst_id, exc)
        self._leverage_set.add(inst.inst_id)

    async def stop(self) -> None:
        if self._dms_task:
            self._dms_task.cancel()
        await self.cancel_own_orders()
        with contextlib.suppress(OKXError):
            await self.c.cancel_all_after(0)

    async def _dead_man_loop(self) -> None:
        while True:
            try:
                await self.c.cancel_all_after(self.cfg.dead_man_switch_s)
            except Exception as exc:  # keep refreshing whatever happens
                log.warning("dead-man switch refresh failed: %s", exc)
            await asyncio.sleep(max(5.0, self.cfg.dead_man_switch_s / 3))

    async def heartbeat(self) -> None:
        await self.c.cancel_all_after(self.cfg.dead_man_switch_s)

    async def cancel_own_orders(self) -> None:
        for o in await self.c.pending_orders():
            if o.get("tag") == self.cfg.order_tag or str(o.get("clOrdId", "")).startswith("h"):
                try:
                    await self.c.cancel_order(o["instId"], o["clOrdId"])
                except OKXError as exc:
                    log.warning("cancel %s: %s", o.get("clOrdId"), exc)

    # -- state ----------------------------------------------------------------------------------------------
    async def equity(self) -> float:
        bal = await self.c.balance("USDT")
        for d in bal.get("details", []):
            if d.get("ccy") == "USDT":
                return float(d.get("eq") or 0.0)
        return float(bal.get("totalEq") or 0.0)

    async def positions(self) -> dict[str, Position]:
        """Every open USDT-swap position. One on a contract the model never mapped (a previous run, a manual
        trade) is mapped under its plain symbol, so it can still be protected and closed."""
        out = {}
        for p in await self.c.positions():
            iid = p.get("instId", "")
            pos = float(p.get("pos") or 0.0)
            if pos == 0:
                continue
            s = self.inst_to_symbol.get(iid)
            if s is None and iid in self.known:
                inst = self.known[iid]
                # A crypto swap maps to its Binance-style symbol; any other keeps its OKX id, so that it is never
                # mistaken for a Binance contract with the same ticker (BBUSDT is BounceBit, BB-USDT-SWAP is not).
                s = iid.split("-")[0] + "USDT" if inst.category == "1" else iid
                self.instruments[s] = inst
                self.inst_to_symbol[iid] = s
                self._derived.add(s)
            if s is None:
                log.warning("position on unknown instrument %s left alone", iid)
                continue
            inst = self.instruments[s]
            mark = float(p.get("markPx") or p.get("last") or 0.0)
            out[s] = Position(s, pos, inst.notional(pos, mark), float(p.get("avgPx") or mark), mark)
        return out

    async def _touch(self, inst: Instrument) -> tuple[float, float, float, float]:
        b = await self.c.books(inst.inst_id, 1)
        bid, bsz = float(b["bids"][0][0]), float(b["bids"][0][1])
        ask, asz = float(b["asks"][0][0]), float(b["asks"][0][1])
        return bid, ask, bsz, asz

    # -- planning -------------------------------------------------------------------------------------------
    def plan(self, targets: dict[str, float], positions: dict[str, Position], prices: dict[str, float]) -> list[Child]:
        children: list[Child] = []
        for s in sorted(set(targets) | set(positions)):
            inst = self.instruments.get(s)
            if inst is None:
                continue
            px = prices.get(s)
            if not px:
                continue
            cur = positions[s].contracts if s in positions else 0.0
            tgt = inst.round_size_down(inst.contracts_for_notional(targets.get(s, 0.0), px))
            if targets.get(s, 0.0) == 0.0:
                tgt = 0.0
            if abs(tgt - cur) < inst.min_sz and tgt != 0.0:
                continue
            if tgt == cur:
                continue
            if cur != 0 and (tgt == 0 or (tgt > 0) != (cur > 0)):
                # Close (reduce-only) first; the opening leg of a flip follows.
                children.append(Child(s, inst, "sell" if cur > 0 else "buy", abs(cur), True))
                if tgt != 0:
                    children.append(Child(s, inst, "buy" if tgt > 0 else "sell", abs(tgt), False))
            else:
                delta = tgt - cur
                reducing = abs(tgt) < abs(cur)
                size = inst.round_size_down(delta) if not reducing else delta
                if size == 0:
                    continue
                children.append(Child(s, inst, "buy" if delta > 0 else "sell", abs(size), reducing))
        children.sort(key=lambda c: not c.reduce_only)
        return children

    async def rebalance(
        self, targets: dict[str, float], urgent: bool = False, hold: set[str] | None = None
    ) -> ExecutionReport:
        """Trade toward ``targets`` (USDT notionals); positions absent from it are closed, except those in
        ``hold``, which are left exactly as they are (no price this bar, not in the data feed)."""
        rep = ExecutionReport()
        await self.cancel_own_orders()  # an order left resting by an earlier cycle must not fill twice
        positions = await self.positions()
        if hold:
            positions = {s: p for s, p in positions.items() if s not in hold}
            targets = {s: v for s, v in targets.items() if s not in hold}
        tick = {t["instId"]: t for t in await self.c.tickers()}
        prices = {}
        for s, inst in self.instruments.items():
            t = tick.get(inst.inst_id)
            if t and t.get("last"):
                prices[s] = float(t["last"])
        children = self.plan(targets, positions, prices)
        reduces = [c for c in children if c.reduce_only]
        increases = [c for c in children if not c.reduce_only]
        for group in (reduces, increases):
            results = await asyncio.gather(*(self._execute_child(c, urgent) for c in group), return_exceptions=True)
            for c, r in zip(group, results):
                if isinstance(r, ChildError):
                    rep.errors.append(f"{c.symbol}: {r}")
                    rep.fills.extend(r.fills)
                    if r.remaining > 0:
                        rep.unfilled[c.symbol] = r.remaining
                    continue
                if isinstance(r, BaseException):
                    rep.errors.append(f"{c.symbol}: {r}")
                    rep.unfilled[c.symbol] = c.contracts
                    continue
                fills, remaining = r
                rep.fills.extend(fills)
                if remaining > 0:
                    rep.unfilled[c.symbol] = remaining
        rep.finished = time.time()
        return rep

    async def flatten(self) -> ExecutionReport:
        return await self.rebalance({}, urgent=True)

    # -- child execution ------------------------------------------------------------------------------------
    async def _order_state(self, inst: Instrument, clid: str) -> tuple[float, float, str, float]:
        o = await self.c.get_order(inst.inst_id, clid)
        if o is None:
            return 0.0, 0.0, "missing", 0.0
        filled = float(o.get("accFillSz") or 0.0)
        avg = float(o.get("avgPx") or 0.0)
        fee = -float(o.get("fee") or 0.0)  # OKX reports fees as negative numbers
        return filled, avg, str(o.get("state")), fee

    async def _place(self, ch: Child, clid: str, **order: str) -> bool:
        """Send one order; True when it exists on the exchange.

        A clean rejection (``sCode`` error) means no order. Anything ambiguous -- transport failure, HTTP 5xx,
        timeout -- may still have created it: the exchange is asked by client id, and a resting order is
        tracked like any other instead of being forgotten on the book.
        """
        if not ch.reduce_only:
            await self._ensure_leverage(ch.inst)
        try:
            await self.c.place_order(
                instId=ch.inst.inst_id,
                tdMode=self.cfg.td_mode,
                side=ch.side,
                clOrdId=clid,
                tag=self.cfg.order_tag,
                reduceOnly=ch.reduce_only,
                **order,
            )
            return True
        except OKXError as exc:
            if not ambiguous(exc):
                log.warning("%s %s rejected: %s", order.get("ordType"), ch.inst.inst_id, exc)
                return False
            log.warning("%s %s ambiguous (%s): checking the exchange", order.get("ordType"), ch.inst.inst_id, exc)
        except Exception as exc:
            log.warning(
                "%s %s transport failure (%r): checking the exchange", order.get("ordType"), ch.inst.inst_id, exc
            )
        for _ in range(5):
            await asyncio.sleep(1.0)
            try:
                _, _, state, _ = await self._order_state(ch.inst, clid)
            except Exception:
                continue
            if state != "missing":
                return True
        # Still unknown: the order may yet reach the book. Sending another one could double the trade, so
        # this child stops here; the next cycle cancels anything resting and re-reads the positions.
        with contextlib.suppress(Exception):
            await self.c.cancel_order(ch.inst.inst_id, clid)
        raise RuntimeError(f"{ch.inst.inst_id} order {clid}: not confirmed after an ambiguous send, child aborted")

    async def _final_state(self, inst: Instrument, clid: str) -> tuple[float, float, str, float]:
        """Order state once terminal (filled / canceled / missing), polling briefly after a cancel or IOC."""
        filled, avg, state, fee = await self._order_state(inst, clid)
        for _ in range(10):
            if state in TERMINAL:
                break
            await asyncio.sleep(0.3)
            filled, avg, state, fee = await self._order_state(inst, clid)
        return filled, avg, state, fee

    async def _execute_child(self, ch: Child, urgent: bool) -> tuple[list[Fill], float]:
        """Passive phase then bounded IOC. Any failure raises ``ChildError`` carrying the fills already done."""
        fills: list[Fill] = []
        remaining = ch.contracts
        try:
            remaining = await self._run_child(ch, urgent, fills)
        except ChildError:
            raise
        except Exception as exc:
            raise ChildError(str(exc), fills, remaining) from exc
        return fills, max(remaining, 0.0)

    async def _run_child(self, ch: Child, urgent: bool, fills: list[Fill]) -> float:
        inst = ch.inst
        remaining = ch.contracts

        def book(filled: float, avg: float, fee: float, maker: bool) -> None:
            nonlocal remaining
            if filled > 0:
                qty = filled if ch.side == "buy" else -filled
                fills.append(Fill(ch.symbol, ch.side, qty, avg, fee, maker, notional=inst.notional(filled, avg)))
                remaining = round(remaining - filled, 12)

        if self.cfg.maker_first and not urgent:
            deadline = time.monotonic() + self.cfg.maker_timeout_s
            chases = 0
            while remaining >= inst.min_sz and time.monotonic() < deadline:
                clid = new_client_id()
                bid, ask, _, _ = await self._touch(inst)
                px = bid if ch.side == "buy" else ask
                if not await self._place(
                    ch, clid, ordType="post_only", sz=inst.fmt_size(remaining), px=inst.fmt_price(px)
                ):
                    break
                live_px = px
                state = "live"
                while time.monotonic() < deadline:
                    await asyncio.sleep(self.cfg.chase_interval_s)
                    filled, _, state, _ = await self._order_state(inst, clid)
                    if state in TERMINAL:
                        break
                    bid, ask, _, _ = await self._touch(inst)
                    touch = bid if ch.side == "buy" else ask
                    if touch != live_px and chases < self.cfg.max_chases:
                        try:
                            await self.c.amend_order(inst.inst_id, clid, new_px=inst.fmt_price(touch))
                            live_px, chases = touch, chases + 1
                        except OKXError:
                            pass  # filled or cancelled meanwhile; the next poll tells
                if state not in TERMINAL:
                    with contextlib.suppress(OKXError):
                        await self.c.cancel_order(inst.inst_id, clid)
                filled, avg, state, fee = await self._final_state(inst, clid)
                book(filled, avg, fee, True)
                if state not in TERMINAL:
                    # Cancel not confirmed (live / partially filled): it may still fill, so nothing more is sent
                    # for this child; the next cycle cancels it and re-reads the position.
                    raise ChildError(f"{inst.inst_id} order {clid} still {state} after cancel", fills, remaining)
                if state == "filled":
                    break
        if remaining >= inst.min_sz or (ch.reduce_only and remaining > 0):
            bid, ask, _, _ = await self._touch(inst)
            cap = self.cfg.taker_slippage_cap_bps * 1e-4
            px = ask * (1 + cap) if ch.side == "buy" else bid * (1 - cap)
            px = inst.round_price(px, ch.side, passive=False)
            clid = new_client_id()
            if await self._place(ch, clid, ordType="ioc", sz=inst.fmt_size(remaining), px=inst.fmt_price(px)):
                filled, avg, state, fee = await self._final_state(inst, clid)
                book(filled, avg, fee, False)
                if state not in TERMINAL:
                    raise ChildError(f"{inst.inst_id} IOC {clid} still {state}", fills, remaining)
        return remaining

    @staticmethod
    def price_to_model(symbol: str, price: float) -> float:
        """OKX price (per coin) in the model's (Binance) units, e.g. per 1000 coins for 1000PEPEUSDT."""
        return okx_price_to_model(symbol, price)

    # -- protection -----------------------------------------------------------------------------------------
    async def protect(self, stop_fraction: dict[str, float]) -> None:
        """One catastrophe stop per open position, on the right side, ``stop_fraction`` away from mark."""
        positions = await self.positions()
        existing = [a for a in await self.c.pending_algos("conditional") if a.get("tag") == self.cfg.order_tag]
        by_inst: dict[str, list[dict[str, str]]] = {}
        for a in existing:
            by_inst.setdefault(a["instId"], []).append(a)
        to_cancel: list[dict[str, str]] = []
        for iid, algos in by_inst.items():
            s = self.inst_to_symbol.get(iid)
            if s is None:
                continue  # not ours to judge: never remove a stop we cannot relate to a position
            pos = positions.get(s)
            for a in algos:
                side_ok = pos is not None and ((pos.contracts > 0) == (a.get("side") == "sell"))
                if not side_ok:
                    to_cancel.append({"instId": iid, "algoId": a["algoId"]})
        if to_cancel:
            await self.c.cancel_algos(to_cancel)
        cancelled = {x["algoId"] for x in to_cancel}
        for s, pos in positions.items():
            inst = self.instruments[s]
            have = [a for a in by_inst.get(inst.inst_id, []) if a["algoId"] not in cancelled]
            if have:
                continue
            frac = stop_fraction.get(s, 0.15)
            side = "sell" if pos.contracts > 0 else "buy"
            trig = pos.mark_px * (1 - frac) if pos.contracts > 0 else pos.mark_px * (1 + frac)
            trig = inst.round_price(trig, side, passive=True)
            try:
                await self.c.place_algo(
                    instId=inst.inst_id,
                    tdMode=self.cfg.td_mode,
                    side=side,
                    ordType="conditional",
                    closeFraction="1",
                    reduceOnly=True,
                    slTriggerPx=inst.fmt_price(trig),
                    slOrdPx="-1",
                    slTriggerPxType="mark",
                    tag=self.cfg.order_tag,
                    algoClOrdId=new_client_id("p"),
                )
            except OKXError as exc:
                log.error("stop for %s failed: %s", s, exc)


def okx_price_to_model(symbol: str, okx_price: float) -> float:
    return okx_price * binance_price_factor(symbol)
