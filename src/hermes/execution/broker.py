"""Broker interface and the paper broker.

The live engine only talks to a :class:`Broker`; switching from paper to OKX demo to OKX live changes the
broker, never the decision code. Targets are expressed as signed USDT notionals per model symbol.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    contracts: float  # signed (short < 0); for the paper broker, base units
    notional: float  # signed USDT at mark
    avg_px: float
    mark_px: float


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    maker: bool
    ts: float = field(default_factory=time.time)
    notional: float = 0.0  # USDT value of the fill (qty units differ by venue: coins, contracts)


@dataclass
class ExecutionReport:
    fills: list[Fill] = field(default_factory=list)
    unfilled: dict[str, float] = field(default_factory=dict)  # symbol -> notional not executed
    errors: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    @property
    def traded_notional(self) -> float:
        return sum(abs(f.notional) if f.notional else abs(f.qty * f.price) for f in self.fills)

    @property
    def fees(self) -> float:
        return sum(f.fee for f in self.fills)

    @property
    def maker_share(self) -> float:
        tot = self.traded_notional
        return sum(abs(f.qty * f.price) for f in self.fills if f.maker) / tot if tot else 0.0


class Broker(Protocol):
    async def start(self) -> None: ...

    async def equity(self) -> float: ...

    async def positions(self) -> dict[str, Position]: ...

    async def rebalance(
        self, targets: dict[str, float], urgent: bool = False, hold: set[str] | None = None
    ) -> ExecutionReport: ...

    async def protect(self, stop_fraction: dict[str, float]) -> None: ...

    async def flatten(self) -> ExecutionReport: ...

    async def heartbeat(self) -> None: ...

    async def stop(self) -> None: ...


def new_client_id(prefix: str = "h") -> str:
    """OKX clOrdId: letters/digits only, <= 32 chars, unique."""
    return (prefix + uuid.uuid4().hex)[:32]


class PaperBroker:
    """Simulated account on live prices. Persisted to JSON so a restart continues the same book.

    Fills: ``maker_share`` of each trade at the passive side of the estimated spread, the rest at the
    aggressive side plus slippage; fees per the cost configuration. Funding is accrued by the engine.
    """

    def __init__(
        self,
        state_file: str | Path,
        initial_equity: float,
        maker_fee: float,
        taker_fee: float,
        maker_share: float = 0.6,
        half_spread: float = 2e-4,
        slippage: float = 2e-4,
    ):
        self.path = Path(state_file)
        self.maker_fee, self.taker_fee = maker_fee, taker_fee
        self.maker_share, self.half_spread, self.slippage = maker_share, half_spread, slippage
        self.prices: dict[str, float] = {}
        self.stops: dict[str, tuple[float, float]] = {}  # symbol -> (side, trigger price), as placed on OKX
        if self.path.exists():
            s = json.loads(self.path.read_text())
            self.cash = float(s["cash"])
            self.qty = {k: float(v) for k, v in s["qty"].items()}
            self.avg = {k: float(v) for k, v in s.get("avg", {}).items()}
            self.fees_paid = float(s.get("fees_paid", 0.0))
            self.funding_paid = float(s.get("funding_paid", 0.0))
            # Last marks: after a restart the book is valued at market, not at entry prices.
            self.prices = {k: float(v) for k, v in s.get("prices", {}).items()}
            self.stops = {k: (float(v[0]), float(v[1])) for k, v in s.get("stops", {}).items()}
        else:
            self.cash = float(initial_equity)
            self.qty, self.avg = {}, {}
            self.fees_paid = self.funding_paid = 0.0

    def set_prices(self, prices: dict[str, float]) -> None:
        self.prices.update({k: v for k, v in prices.items() if v and v > 0})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "cash": self.cash,
                    "qty": self.qty,
                    "avg": self.avg,
                    "fees_paid": self.fees_paid,
                    "funding_paid": self.funding_paid,
                    "prices": {k: v for k, v in self.prices.items() if k in self.qty},
                    "stops": {k: list(v) for k, v in self.stops.items()},
                }
            )
        )
        tmp.replace(self.path)

    async def start(self) -> None:
        self._save()

    async def equity(self) -> float:
        return self.cash + sum(q * self.prices.get(s, self.avg.get(s, 0.0)) for s, q in self.qty.items())

    async def positions(self) -> dict[str, Position]:
        out = {}
        for s, q in self.qty.items():
            if q == 0:
                continue
            px = self.prices.get(s, self.avg.get(s, 0.0))
            out[s] = Position(s, q, q * px, self.avg.get(s, px), px)
        return out

    def accrue_funding(self, rates: dict[str, float]) -> float:
        """Longs pay positive funding: cash -= qty * price * rate."""
        paid = 0.0
        for s, r in rates.items():
            q = self.qty.get(s, 0.0)
            if q and r:
                paid += q * self.prices.get(s, 0.0) * r
        self.cash -= paid
        self.funding_paid += paid
        self._save()
        return paid

    async def rebalance(
        self, targets: dict[str, float], urgent: bool = False, hold: set[str] | None = None
    ) -> ExecutionReport:
        rep = ExecutionReport()
        for s in sorted(set(targets) | set(self.qty)):
            if hold and s in hold:
                continue
            px = self.prices.get(s)
            if not px:
                if targets.get(s, 0.0):
                    rep.errors.append(f"{s}: no price")
                continue
            cur = self.qty.get(s, 0.0)
            tgt_qty = targets.get(s, 0.0) / px
            dq = tgt_qty - cur
            if abs(dq * px) < 1.0:  # below 1 USDT: dust
                continue
            side = "buy" if dq > 0 else "sell"
            sgn = 1.0 if dq > 0 else -1.0
            m = 0.0 if urgent else self.maker_share
            for share, maker in ((m, True), (1 - m, False)):
                if share <= 0:
                    continue
                q = dq * share
                fpx = (
                    px * (1 - sgn * self.half_spread) if maker else px * (1 + sgn * (self.half_spread + self.slippage))
                )
                fee = abs(q * fpx) * (self.maker_fee if maker else self.taker_fee)
                self._apply(s, q, fpx, fee)
                rep.fills.append(Fill(s, side, q, fpx, fee, maker, notional=abs(q * fpx)))
        rep.finished = time.time()
        self._save()
        return rep

    def _apply(self, s: str, q: float, px: float, fee: float) -> None:
        cur = self.qty.get(s, 0.0)
        new = cur + q
        if cur == 0 or (cur > 0) == (q > 0):
            tot = abs(cur) + abs(q)
            self.avg[s] = (abs(cur) * self.avg.get(s, px) + abs(q) * px) / tot if tot else px
        elif (new > 0) != (cur > 0) and new != 0:
            self.avg[s] = px
        self.cash -= q * px + fee
        self.fees_paid += fee
        self.qty[s] = new
        if abs(new) * px < 1e-6:
            self.qty.pop(s, None)
            self.avg.pop(s, None)

    async def protect(self, stop_fraction: dict[str, float]) -> None:
        """Catastrophe stops as the OKX broker places them: one per position, ``stop_fraction`` from the mark
        when the position is opened, kept while it keeps its side, replaced when it flips."""
        for sym in list(self.stops):
            q = self.qty.get(sym, 0.0)
            if q == 0 or np.sign(q) != self.stops[sym][0]:
                del self.stops[sym]
        for sym, q in self.qty.items():
            px = self.prices.get(sym)
            if q == 0 or sym in self.stops or not px:
                continue
            side = float(np.sign(q))
            self.stops[sym] = (side, px * (1.0 - side * stop_fraction.get(sym, 0.15)))
        self._save()

    def check_stops(self, high: dict[str, float], low: dict[str, float], open_: dict[str, float]) -> list[Fill]:
        """Trigger the stops on the last closed bar's range (a gap through the level fills at the open), exit at
        market: taker fee plus the aggressive side of the spread and slippage. Same rule as the backtest."""
        fills = []
        for sym, (side, level) in list(self.stops.items()):
            q = self.qty.get(sym, 0.0)
            if q == 0:
                del self.stops[sym]
                continue
            lo, hi, op = low.get(sym), high.get(sym), open_.get(sym)
            if side > 0 and lo is not None and lo <= level:
                exit_px = min(level, op) if op else level
            elif side < 0 and hi is not None and hi >= level:
                exit_px = max(level, op) if op else level
            else:
                continue
            sgn = -side  # closing trade direction
            px = exit_px * (1 + sgn * (self.half_spread + self.slippage))
            fee = abs(q * px) * self.taker_fee
            self._apply(sym, -q, px, fee)
            fills.append(Fill(sym, "sell" if q > 0 else "buy", -q, px, fee, False, notional=abs(q * px)))
            del self.stops[sym]
        if fills:
            self._save()
        return fills

    @staticmethod
    def price_to_model(symbol: str, price: float) -> float:
        return price

    async def flatten(self) -> ExecutionReport:
        return await self.rebalance({}, urgent=True)

    async def heartbeat(self) -> None:
        return None

    async def stop(self) -> None:
        self._save()

    def snapshot(self) -> dict[str, object]:
        return {
            "cash": self.cash,
            "qty": dict(self.qty),
            "fees_paid": self.fees_paid,
            "funding_paid": self.funding_paid,
        }


__all__ = ["Broker", "ExecutionReport", "Fill", "PaperBroker", "Position", "asdict", "new_client_id"]
