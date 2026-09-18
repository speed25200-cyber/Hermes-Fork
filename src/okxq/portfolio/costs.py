"""Modèle de coûts (§37, §21) : composants SIGNÉS en fraction du notionnel, anti-double comptage, stress.

Conventions :
- un composant négatif est un COÛT, un composant positif un GAIN (rebate maker, funding favorable) ;
- ``cost_basis`` MID : le prix de référence est le milieu ; le demi-spread est un coût explicite.
  ``cost_basis`` EXECUTABLE : le prix de référence est le prix touchable (meilleure contrepartie) ;
  le spread est DÉJÀ contenu dans le prix (``included_in_price``) et ne doit pas être déduit une seconde
  fois (T24) ;
- ``slippage`` = marche déterministe dans la profondeur OBSERVABLE au-delà du premier niveau ;
  ``impact`` = composante déclarée (coefficient × √participation) pour ce que le carnet ne montre pas ;
  les deux ne se recouvrent pas : le premier se lit dans le carnet, le second est une hypothèse
  explicite, nulle par défaut ;
- les multiplicateurs de stress n'amplifient que les coûts (composants négatifs), jamais les gains ;
- §37 : à taille nulle et marché plat, un aller-retour sans rebate ni funding favorable coûte de l'argent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from okxq.domain.errors import CostModelError, DoubleCountingError
from okxq.domain.events import CostBasis, Liquidity
from okxq.domain.instruments import InstrumentSpec
from okxq.domain.money import ONE, ZERO, Money, Side, dec

__all__ = [
    "CostComponent",
    "CostEstimate",
    "CostModel",
    "FeeSchedule",
    "ShortfallBreakdown",
    "StressMultipliers",
    "WalkResult",
    "fee_cashflow",
    "implementation_shortfall",
    "walk_levels",
]

Level = tuple[Decimal, Decimal]  # (prix, quantité en contrats)


class CostComponent(StrEnum):
    FEES = "fees"
    SPREAD = "spread"
    SLIPPAGE = "slippage"
    FUNDING_EXPECTED = "funding_expected"
    IMPACT = "impact"


ALL_COMPONENTS: tuple[CostComponent, ...] = tuple(CostComponent)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Taux en fraction du notionnel ; un taux NÉGATIF est un rebate (le maker est payé)."""

    maker_rate: Decimal
    taker_rate: Decimal
    fee_ccy: str = "USDT"

    def __post_init__(self) -> None:
        object.__setattr__(self, "maker_rate", dec(self.maker_rate, field="maker_rate"))
        object.__setattr__(self, "taker_rate", dec(self.taker_rate, field="taker_rate"))
        if self.taker_rate < 0:
            raise CostModelError(
                "un taux taker négatif n'est pas un cas supporté", taker_rate=str(self.taker_rate)
            )

    def rate(self, liquidity: Liquidity) -> Decimal:
        if liquidity is Liquidity.MAKER:
            return self.maker_rate
        if liquidity is Liquidity.TAKER:
            return self.taker_rate
        # Liquidité inconnue : hypothèse prudente, le taux le plus coûteux.
        return max(self.maker_rate, self.taker_rate)


def fee_cashflow(notional: Money, liquidity: Liquidity, schedule: FeeSchedule) -> Money:
    """Flux de commission SIGNÉ : ``-|notionnel| × taux`` (négatif = payé, positif = rebate). Jamais abs()."""
    if notional.ccy != schedule.fee_ccy:
        raise CostModelError(
            "devise du notionnel ≠ devise des frais", notional=notional.ccy, fee=schedule.fee_ccy
        )
    return Money(-abs(notional.amount) * schedule.rate(liquidity), schedule.fee_ccy)


@dataclass(frozen=True, slots=True)
class StressMultipliers:
    fees: Decimal = ONE
    spread: Decimal = ONE
    slippage: Decimal = ONE
    funding: Decimal = ONE
    impact: Decimal = ONE

    def __post_init__(self) -> None:
        for name in ("fees", "spread", "slippage", "funding", "impact"):
            value = dec(getattr(self, name), field=name)
            if value < 1:
                raise CostModelError("un multiplicateur de stress est ≥ 1", multiplier=name, value=str(value))
            object.__setattr__(self, name, value)

    def for_component(self, component: CostComponent) -> Decimal:
        return {
            CostComponent.FEES: self.fees,
            CostComponent.SPREAD: self.spread,
            CostComponent.SLIPPAGE: self.slippage,
            CostComponent.FUNDING_EXPECTED: self.funding,
            CostComponent.IMPACT: self.impact,
        }[component]


@dataclass(frozen=True, slots=True)
class WalkResult:
    filled_contracts: Decimal
    notional: Decimal
    vwap: Decimal | None
    levels_consumed: int
    shortfall_contracts: Decimal  # quantité que la profondeur observable ne couvre pas


def walk_levels(
    levels: Sequence[Level],
    contracts: Decimal,
    *,
    price_limit: Decimal | None = None,
    side: Side | None = None,
) -> WalkResult:
    """Marche dans les niveaux opposés (déjà triés du meilleur au pire), bornée par la profondeur et la limite."""
    remaining = dec(contracts, field="contracts")
    if remaining < 0:
        raise CostModelError("quantité négative", contracts=str(contracts))
    filled = ZERO
    notional = ZERO
    consumed = 0
    for price, qty in levels:
        if remaining <= 0:
            break
        if price_limit is not None and side is not None:
            if side is Side.BUY and price > price_limit:
                break
            if side is Side.SELL and price < price_limit:
                break
        if qty <= 0:
            continue
        take = min(qty, remaining)
        filled += take
        notional += take * price
        remaining -= take
        consumed += 1
    vwap = notional / filled if filled > 0 else None
    return WalkResult(filled, notional, vwap, consumed, remaining)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    cost_basis: CostBasis
    reference_price: Decimal
    components: dict[CostComponent, Decimal]  # fractions signées du notionnel (négatif = coût)
    included_in_price: tuple[str, ...]
    stress: StressMultipliers
    depth_shortfall_contracts: Decimal = ZERO
    notes: tuple[str, ...] = ()

    @property
    def total(self) -> Decimal:
        return sum(self.components.values(), ZERO)

    def money(self, notional: Money) -> Money:
        return Money(abs(notional.amount) * self.total, notional.ccy)


@dataclass(frozen=True, slots=True)
class CostModel:
    """Estimation ex ante des coûts d'une exécution (une jambe) ou d'un aller-retour."""

    cost_basis: CostBasis
    fee_schedule: FeeSchedule
    impact_coefficient: Decimal = ZERO  # fraction du notionnel par √participation ; 0 = non modélisé
    stress: StressMultipliers = field(default_factory=StressMultipliers)

    def __post_init__(self) -> None:
        coefficient = dec(self.impact_coefficient, field="impact_coefficient")
        if coefficient < 0:
            raise CostModelError("impact_coefficient négatif", value=str(coefficient))
        object.__setattr__(self, "impact_coefficient", coefficient)

    @property
    def default_included_in_price(self) -> tuple[str, ...]:
        return (CostComponent.SPREAD.value,) if self.cost_basis is CostBasis.EXECUTABLE else ()

    def estimate(
        self,
        *,
        side: Side,
        contracts: Decimal,
        best_bid: Decimal,
        best_ask: Decimal,
        opposite_levels: Sequence[Level] = (),
        liquidity: Liquidity = Liquidity.TAKER,
        funding_rate_estimate: Decimal = ZERO,
        settlements_in_horizon: int = 0,
        position_sign: int = 0,
        participation_fraction: Decimal = ZERO,
        included_in_price: Sequence[str] | None = None,
        deduct: Sequence[CostComponent] = ALL_COMPONENTS,
    ) -> CostEstimate:
        """Coûts d'UNE jambe. ``deduct`` liste ce que l'on déduit ; ``included_in_price`` ce que le prix contient déjà.

        Lever ``DoubleCountingError`` si un composant figure dans les deux (T24).
        """
        bid = dec(best_bid, field="best_bid")
        ask = dec(best_ask, field="best_ask")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise CostModelError("carnet invalide pour l'estimation", bid=str(bid), ask=str(ask))
        qty = dec(contracts, field="contracts")
        if qty < 0:
            raise CostModelError("quantité négative", contracts=str(qty))
        included = (
            tuple(included_in_price) if included_in_price is not None else self.default_included_in_price
        )
        wanted = tuple(deduct)
        clash = [c.value for c in wanted if c.value in included]
        if clash:
            raise DoubleCountingError(
                "coût déjà contenu dans le prix et déduit une seconde fois",
                components=clash,
                cost_basis=self.cost_basis.value,
            )
        mid = (bid + ask) / 2
        touch = ask if side is Side.BUY else bid
        reference = mid if self.cost_basis is CostBasis.MID else touch
        if self.cost_basis is CostBasis.EXECUTABLE and CostComponent.SPREAD.value not in included:
            raise CostModelError(
                "convention EXECUTABLE : le spread est contenu dans le prix et doit être déclaré dans included_in_price"
            )
        components: dict[CostComponent, Decimal] = {}
        notes: list[str] = []
        if CostComponent.FEES in wanted:
            components[CostComponent.FEES] = -self.fee_schedule.rate(liquidity)
        if CostComponent.SPREAD in wanted:
            # MID : le demi-spread est payé en traversant ; un maker le gagne en théorie mais ne l'encaisse
            # qu'exécuté, donc l'estimation ex ante d'un maker est 0, jamais un gain.
            half = (ask - bid) / 2
            components[CostComponent.SPREAD] = -(half / mid) if liquidity is not Liquidity.MAKER else ZERO
        shortfall = ZERO
        if CostComponent.SLIPPAGE in wanted:
            if qty > 0 and opposite_levels and liquidity is not Liquidity.MAKER:
                walk = walk_levels(opposite_levels, qty)
                shortfall = walk.shortfall_contracts
                if walk.vwap is not None:
                    adverse = (walk.vwap - touch) if side is Side.BUY else (touch - walk.vwap)
                    components[CostComponent.SLIPPAGE] = -max(adverse, ZERO) / reference
                else:
                    components[CostComponent.SLIPPAGE] = ZERO
                if shortfall > 0:
                    notes.append(f"profondeur observable insuffisante : {shortfall} contrats non couverts")
            else:
                components[CostComponent.SLIPPAGE] = ZERO
        if CostComponent.FUNDING_EXPECTED in wanted:
            rate = dec(funding_rate_estimate, field="funding_rate_estimate")
            sign = position_sign if position_sign != 0 else side.sign
            # Un long paie un taux positif : flux = -sign × taux, par règlement traversé, sans proratisation.
            components[CostComponent.FUNDING_EXPECTED] = -sign * rate * settlements_in_horizon
        if CostComponent.IMPACT in wanted:
            part = dec(participation_fraction, field="participation_fraction")
            if part < 0:
                raise CostModelError("participation négative")
            components[CostComponent.IMPACT] = -self.impact_coefficient * part.sqrt() if part > 0 else ZERO
        stressed = {c: self._stress(c, v) for c, v in components.items()}
        return CostEstimate(
            cost_basis=self.cost_basis,
            reference_price=reference,
            components=stressed,
            included_in_price=included,
            stress=self.stress,
            depth_shortfall_contracts=shortfall,
            notes=tuple(notes),
        )

    def _stress(self, component: CostComponent, value: Decimal) -> Decimal:
        if value >= 0:
            return value  # un gain n'est jamais amplifié par le stress
        return value * self.stress.for_component(component)

    def round_trip(
        self,
        *,
        side: Side,
        contracts: Decimal,
        best_bid: Decimal,
        best_ask: Decimal,
        entry_liquidity: Liquidity = Liquidity.TAKER,
        exit_liquidity: Liquidity = Liquidity.TAKER,
        opposite_levels_entry: Sequence[Level] = (),
        opposite_levels_exit: Sequence[Level] = (),
        funding_rate_estimate: Decimal = ZERO,
        settlements_in_horizon: int = 0,
        participation_fraction: Decimal = ZERO,
    ) -> CostEstimate:
        """Entrée + sortie (sortie du côté opposé). Le funding n'est compté qu'une fois, sur la détention."""
        entry = self.estimate(
            side=side,
            contracts=contracts,
            best_bid=best_bid,
            best_ask=best_ask,
            opposite_levels=opposite_levels_entry,
            liquidity=entry_liquidity,
            funding_rate_estimate=funding_rate_estimate,
            settlements_in_horizon=settlements_in_horizon,
            position_sign=side.sign,
            participation_fraction=participation_fraction,
        )
        exit_ = self.estimate(
            side=side.opposite,
            contracts=contracts,
            best_bid=best_bid,
            best_ask=best_ask,
            opposite_levels=opposite_levels_exit,
            liquidity=exit_liquidity,
            funding_rate_estimate=ZERO,
            settlements_in_horizon=0,
            participation_fraction=participation_fraction,
        )
        merged = {c: entry.components.get(c, ZERO) + exit_.components.get(c, ZERO) for c in ALL_COMPONENTS}
        return CostEstimate(
            cost_basis=self.cost_basis,
            reference_price=entry.reference_price,
            components=merged,
            included_in_price=entry.included_in_price,
            stress=self.stress,
            depth_shortfall_contracts=max(entry.depth_shortfall_contracts, exit_.depth_shortfall_contracts),
            notes=entry.notes + exit_.notes,
        )


@dataclass(frozen=True, slots=True)
class ShortfallBreakdown:
    """Implementation shortfall en devise de règlement, positif = coût, sur segments de prix DISJOINTS.

    - ``reference`` : prix de décision (ancre, pas un coût) ;
    - ``delay`` : (mid à l'arrivée − mid à la décision) dans le sens de l'ordre ;
    - ``spread`` : (prix touchable à l'arrivée − mid à l'arrivée) ;
    - ``depth`` : (VWAP exécuté − prix touchable) ;
    - ``fees`` : −fee_cashflow ;
    - ``impact`` : (mid après exécution − mid à l'arrivée), coût porté par le reste de la position,
      rapporté séparément et NON additionné à ``total_executed``.
    """

    ccy: str
    reference: Decimal
    filled_base_qty: Decimal
    delay: Decimal
    spread: Decimal
    depth: Decimal
    fees: Decimal
    impact: Decimal

    @property
    def total_executed(self) -> Decimal:
        return self.delay + self.spread + self.depth + self.fees

    @property
    def total_with_impact(self) -> Decimal:
        return self.total_executed + self.impact


def implementation_shortfall(
    *,
    spec: InstrumentSpec,
    side: Side,
    filled_contracts: Decimal,
    decision_mid: Decimal,
    arrival_mid: Decimal,
    arrival_touch: Decimal,
    executed_vwap: Decimal,
    fee_cashflow_total: Decimal,
    post_trade_mid: Decimal | None = None,
) -> ShortfallBreakdown:
    """Décomposition sans chevauchement : chaque terme couvre un segment de prix distinct."""
    qty = dec(filled_contracts, field="filled_contracts") * spec.base_units_per_contract
    sign = side.sign
    d_mid = dec(decision_mid, field="decision_mid")
    a_mid = dec(arrival_mid, field="arrival_mid")
    a_touch = dec(arrival_touch, field="arrival_touch")
    vwap = dec(executed_vwap, field="executed_vwap")
    delay = sign * (a_mid - d_mid) * qty
    spread = sign * (a_touch - a_mid) * qty
    depth = sign * (vwap - a_touch) * qty
    fees = -dec(fee_cashflow_total, field="fee_cashflow_total")
    impact = (
        ZERO if post_trade_mid is None else sign * (dec(post_trade_mid, field="post_trade_mid") - a_mid) * qty
    )
    return ShortfallBreakdown(
        ccy=spec.settle_ccy,
        reference=d_mid,
        filled_base_qty=qty,
        delay=delay,
        spread=spread,
        depth=depth,
        fees=fees,
        impact=impact,
    )
