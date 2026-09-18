"""Série d'equity, equity unitisée, drawdown, high-water mark, frontière journalière UTC, PnL marqué vs liquidé (§38).

Fonctions pures. La persistance du high-water mark vit dans ``RiskState`` (``okxq.persistence.models``) :
``write_high_water_mark`` copie l'état calculé dans la ligne, sans décider de la transaction.

Unitisation (T45) : un apport ou retrait externe crée ou détruit des parts au dernier prix de part
connu (convention déclarée : le flux entre EN DÉBUT de période). La valeur de part ne bouge donc qu'avec
le PnL de stratégie ; un dépôt n'améliore ni la performance ni le high-water mark.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from okxq.domain.clocks import ensure_utc, utc_day
from okxq.domain.errors import LedgerError
from okxq.domain.money import ONE, ZERO, dec
from okxq.domain.positions import Position
from okxq.persistence.models import RiskState
from okxq.portfolio.costs import FeeSchedule, Level, walk_levels

__all__ = [
    "DailyPnl",
    "DrawdownPoint",
    "EquityPoint",
    "HighWaterMark",
    "PnlPair",
    "UnitPoint",
    "daily_pnl",
    "drawdowns",
    "marked_and_liquidated_pnl",
    "max_drawdown",
    "strategy_pnl",
    "unitize",
    "update_high_water_mark",
    "write_high_water_mark",
]


@dataclass(frozen=True, slots=True)
class EquityPoint:
    at: datetime
    equity: Decimal
    external_cashflow_cum: Decimal = ZERO  # dépôts (+) / retraits (−) cumulés

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", ensure_utc(self.at, field="at"))
        object.__setattr__(self, "equity", dec(self.equity, field="equity"))
        object.__setattr__(self, "external_cashflow_cum", dec(self.external_cashflow_cum, field="external"))


@dataclass(frozen=True, slots=True)
class UnitPoint:
    at: datetime
    equity: Decimal
    units: Decimal
    unit_value: Decimal
    external_flow: Decimal  # flux externe de la période (t_{i-1}, t_i]


def strategy_pnl(start: EquityPoint, end: EquityPoint) -> Decimal:
    """``Δequity − flux externes nets`` sur l'intervalle (§38)."""
    if end.at < start.at:
        raise LedgerError("intervalle inversé pour strategy_pnl")
    return (end.equity - start.equity) - (end.external_cashflow_cum - start.external_cashflow_cum)


def unitize(points: Sequence[EquityPoint], *, initial_unit_value: Decimal = ONE) -> list[UnitPoint]:
    """Valeur de part neutralisant les apports/retraits (T45)."""
    if not points:
        return []
    unit_value = dec(initial_unit_value, field="initial_unit_value")
    if unit_value <= 0:
        raise LedgerError("valeur de part initiale non positive")
    first = points[0]
    if first.equity <= 0:
        raise LedgerError("equity initiale non positive : unitisation impossible", equity=str(first.equity))
    units = first.equity / unit_value
    out = [UnitPoint(first.at, first.equity, units, unit_value, first.external_cashflow_cum)]
    prev = first
    for p in points[1:]:
        if p.at < prev.at:
            raise LedgerError("série d'equity non ordonnée", at=p.at.isoformat())
        flow = p.external_cashflow_cum - prev.external_cashflow_cum
        if flow != 0:
            units += flow / unit_value  # parts créées/détruites au dernier prix de part connu
        if units <= 0:
            raise LedgerError("nombre de parts non positif après retrait", at=p.at.isoformat())
        unit_value = p.equity / units
        out.append(UnitPoint(p.at, p.equity, units, unit_value, flow))
        prev = p
    return out


@dataclass(frozen=True, slots=True)
class DrawdownPoint:
    at: datetime
    unit_value: Decimal
    high_water_mark: Decimal
    drawdown_fraction: Decimal  # ≥ 0 ; 0 au plus haut


def drawdowns(units: Sequence[UnitPoint]) -> list[DrawdownPoint]:
    out: list[DrawdownPoint] = []
    hwm: Decimal | None = None
    for u in units:
        hwm = u.unit_value if hwm is None or u.unit_value > hwm else hwm
        dd = ZERO if hwm <= 0 else max(ZERO, ONE - u.unit_value / hwm)
        out.append(DrawdownPoint(u.at, u.unit_value, hwm, dd))
    return out


def max_drawdown(units: Sequence[UnitPoint]) -> Decimal:
    return max((d.drawdown_fraction for d in drawdowns(units)), default=ZERO)


@dataclass(frozen=True, slots=True)
class HighWaterMark:
    """HWM sur la valeur de PART (pas sur l'equity brute, qui monterait avec un dépôt)."""

    unit_value: Decimal | None = None
    equity: Decimal | None = None
    at: datetime | None = None


def update_high_water_mark(
    hwm: HighWaterMark, *, unit_value: Decimal, equity: Decimal, at: datetime
) -> HighWaterMark:
    value = dec(unit_value, field="unit_value")
    if hwm.unit_value is None or value > hwm.unit_value:
        return HighWaterMark(
            unit_value=value, equity=dec(equity, field="equity"), at=ensure_utc(at, field="at")
        )
    return hwm


def write_high_water_mark(row: RiskState, hwm: HighWaterMark, *, updated_at: datetime) -> None:
    """Copie l'état calculé dans ``RiskState`` (version optimiste incrémentée). Le commit appartient à l'appelant."""
    row.high_water_mark_unit = hwm.unit_value
    row.high_water_mark_equity = hwm.equity
    row.updated_at = ensure_utc(updated_at, field="updated_at")
    row.version = row.version + 1


@dataclass(frozen=True, slots=True)
class DailyPnl:
    day: datetime  # 00:00:00 UTC (frontière déclarée)
    start_equity: Decimal
    end_equity: Decimal
    external_flows: Decimal
    strategy_pnl: Decimal
    points: int


def daily_pnl(points: Sequence[EquityPoint]) -> list[DailyPnl]:
    """Regroupe par jour UTC ; l'equity de départ d'un jour est la dernière observation du jour précédent."""
    out: list[DailyPnl] = []
    if not points:
        return out
    current_day = utc_day(points[0].at)
    day_start = points[0]
    last = points[0]
    count = 0
    for p in points:
        d = utc_day(p.at)
        if d != current_day:
            out.append(_close_day(current_day, day_start, last, count))
            current_day = d
            day_start = last
            count = 0
        last = p
        count += 1
    out.append(_close_day(current_day, day_start, last, count))
    return out


def _close_day(day: datetime, start: EquityPoint, end: EquityPoint, count: int) -> DailyPnl:
    return DailyPnl(
        day=day,
        start_equity=start.equity,
        end_equity=end.equity,
        external_flows=end.external_cashflow_cum - start.external_cashflow_cum,
        strategy_pnl=strategy_pnl(start, end),
        points=count,
    )


@dataclass(frozen=True, slots=True)
class PnlPair:
    """Deux nombres DISTINCTS : PnL marqué au mark, PnL après liquidation simulée au carnet observable."""

    marked_unrealized: Decimal
    liquidated_unrealized: Decimal
    liquidation_fee: Decimal  # signé (négatif = payé)
    liquidation_vwap: Decimal | None
    unliquidated_base_qty: Decimal  # part que la profondeur observable ne permet pas de sortir

    @property
    def liquidation_cost(self) -> Decimal:
        return self.marked_unrealized - self.liquidated_unrealized


def marked_and_liquidated_pnl(
    position: Position,
    *,
    mark_price: Decimal,
    bids: Sequence[Level],
    asks: Sequence[Level],
    base_units_per_contract: Decimal,
    fee_schedule: FeeSchedule,
) -> PnlPair:
    """Marque la position au mark ET la liquide en taker contre le carnet observable (fin d'historique)."""
    marked = position.unrealized_pnl(mark_price)
    if position.is_flat:
        return PnlPair(ZERO, ZERO, ZERO, None, ZERO)
    v = dec(base_units_per_contract, field="base_units_per_contract")
    contracts = abs(position.signed_base_qty) / v
    levels = bids if position.signed_base_qty > 0 else asks
    walk = walk_levels(levels, contracts)
    if walk.vwap is None:
        # Aucune contrepartie observable : rien n'est liquidable, le PnL liquidé de la part sortie est 0 et
        # la position reste entière ; on le déclare au lieu d'inventer un prix.
        return PnlPair(marked, ZERO, ZERO, None, abs(position.signed_base_qty))
    exited_base = walk.filled_contracts * v
    sign = position.side_sign
    liquidated = sign * exited_base * (walk.vwap - position.average_entry_price)
    fee = -(exited_base * walk.vwap) * fee_schedule.taker_rate
    return PnlPair(
        marked_unrealized=marked,
        liquidated_unrealized=liquidated + fee,
        liquidation_fee=fee,
        liquidation_vwap=walk.vwap,
        unliquidated_base_qty=walk.shortfall_contracts * v,
    )
