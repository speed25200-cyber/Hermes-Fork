"""OKX instrument specifications, unit conversions and directed rounding.

A USDT-margined linear swap is quoted in USDT per base unit; orders are sized in **contracts**, one
contract = ``ctVal * ctMult`` base units. Rounding always goes in the direction that does not increase
risk: position increases are rounded toward zero, and passive prices away from the touch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from hermes.data.universe import base_asset


@dataclass(frozen=True)
class Instrument:
    inst_id: str
    ct_val: float
    ct_mult: float
    lot_sz: float
    min_sz: float
    tick_sz: float
    max_lmt_sz: float
    max_mkt_sz: float
    state: str = "live"
    max_lever: float = 0.0

    @classmethod
    def from_okx(cls, d: dict[str, str]) -> Instrument:
        def f(k: str, default: float = 0.0) -> float:
            try:
                return float(d.get(k) or default)
            except ValueError:
                return default

        return cls(
            inst_id=d["instId"],
            ct_val=f("ctVal"),
            ct_mult=f("ctMult", 1.0) or 1.0,
            lot_sz=f("lotSz", 1.0),
            min_sz=f("minSz", 1.0),
            tick_sz=f("tickSz", 0.0001),
            max_lmt_sz=f("maxLmtSz", 1e12),
            max_mkt_sz=f("maxMktSz", 1e12),
            state=d.get("state", "live"),
            max_lever=f("lever"),
        )

    @property
    def base_per_contract(self) -> float:
        return self.ct_val * self.ct_mult

    def contracts_for_notional(self, notional: float, price: float) -> float:
        """Exact (unrounded) contracts for a USDT notional at ``price``."""
        if price <= 0 or self.base_per_contract <= 0:
            return 0.0
        return notional / (price * self.base_per_contract)

    def notional(self, contracts: float, price: float) -> float:
        return contracts * self.base_per_contract * price

    def round_size_down(self, contracts: float) -> float:
        """Round |contracts| down to the lot size (toward zero), keep the sign; below min size -> 0."""
        if contracts == 0:
            return 0.0
        lot = Decimal(str(self.lot_sz))
        q = (Decimal(str(abs(contracts))) / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
        if q < Decimal(str(self.min_sz)):
            return 0.0
        return math.copysign(float(q), contracts)

    def round_price(self, price: float, side: str, passive: bool = True) -> float:
        """Round to tick. Passive buys round down / sells up; aggressive the opposite."""
        tick = Decimal(str(self.tick_sz))
        down = (side == "buy") == passive
        mode = ROUND_FLOOR if down else ROUND_CEILING
        return float((Decimal(str(price)) / tick).to_integral_value(rounding=mode) * tick)

    def fmt_size(self, contracts: float) -> str:
        return _fmt(abs(contracts), self.lot_sz)

    def fmt_price(self, price: float) -> str:
        return _fmt(price, self.tick_sz)


def _fmt(x: float, step: float) -> str:
    d = Decimal(str(step)).normalize()
    decimals = max(0, -d.as_tuple().exponent)  # type: ignore[operator]
    return f"{x:.{decimals}f}"


def okx_inst_id(binance_symbol: str) -> str:
    return f"{base_asset(binance_symbol)}-USDT-SWAP"


def binance_price_factor(binance_symbol: str) -> float:
    """Binance quotes some contracts per 1000 (or 1M) units: price_binance = factor * price_okx."""
    base = binance_symbol[:-4] if binance_symbol.endswith("USDT") else binance_symbol
    for prefix, factor in (("1000000", 1e6), ("1000", 1e3), ("1M", 1e6)):
        if base.startswith(prefix) and base_asset(binance_symbol) != base:
            return factor
    return 1.0
