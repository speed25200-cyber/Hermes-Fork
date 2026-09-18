"""Fills simulés (§33) : agressifs contre le carnet observable, passifs selon le modèle de file.

- Un ordre agressif (IOC, market, limite qui croise) consomme les niveaux OPPOSÉS du carnet observable à
  son arrivée, dans la limite de sa taille, de sa limite de prix et de la profondeur : jamais de fill
  « au dernier prix » quand la profondeur manque (T29) ; le reliquat d'un IOC est annulé.
- ``DepthReservation`` retire localement la capacité déjà consommée par un ordre précédent tant que le
  carnet n'a pas republié le niveau : deux ordres ne consomment pas le même volume (T30).
- Un post-only qui croiserait à l'arrivée est REJETÉ (raison documentée), sans frais maker artificiels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from okxq.domain.errors import OrderStateError
from okxq.domain.money import ZERO, Side, dec
from okxq.domain.reasons import ReasonCode

__all__ = [
    "POST_ONLY_WOULD_CROSS",
    "AggressiveResult",
    "DepthReservation",
    "LevelFill",
    "book_side_for",
    "match_aggressive",
    "post_only_would_cross",
]

POST_ONLY_WOULD_CROSS = "POST_ONLY_WOULD_CROSS"

Level = tuple[Decimal, Decimal]


def book_side_for(order_side: Side) -> str:
    """Côté du carnet consommé par un ordre : un achat mange les ``asks``, une vente les ``bids``."""
    return "asks" if order_side is Side.BUY else "bids"


@dataclass(frozen=True, slots=True)
class LevelFill:
    price: Decimal
    contracts: Decimal


@dataclass(frozen=True, slots=True)
class AggressiveResult:
    fills: tuple[LevelFill, ...]
    filled: Decimal
    remaining: Decimal
    vwap: Decimal | None
    reason: str | None  # LIQUIDITY_INSUFFICIENT quand la profondeur/limite borne le fill

    @property
    def notional(self) -> Decimal:
        return sum((f.price * f.contracts for f in self.fills), ZERO)


@dataclass(slots=True)
class DepthReservation:
    """Capacité consommée localement par (instrument, côté du carnet, prix) jusqu'à republication du niveau."""

    _consumed: dict[tuple[str, str, Decimal], Decimal] = field(default_factory=dict)

    def available(self, inst_id: str, book_side: str, price: Decimal, displayed: Decimal) -> Decimal:
        used = self._consumed.get((inst_id, book_side, price), ZERO)
        return max(displayed - used, ZERO)

    def reserve(self, inst_id: str, book_side: str, price: Decimal, qty: Decimal) -> None:
        key = (inst_id, book_side, price)
        self._consumed[key] = self._consumed.get(key, ZERO) + qty

    def release_level(self, inst_id: str, book_side: str, price: Decimal) -> None:
        """Le carnet a republié ce niveau : la quantité affichée redevient la vérité observable."""
        self._consumed.pop((inst_id, book_side, price), None)

    def release_instrument(self, inst_id: str) -> None:
        for key in [k for k in self._consumed if k[0] == inst_id]:
            del self._consumed[key]

    def consumed(self, inst_id: str, book_side: str, price: Decimal) -> Decimal:
        return self._consumed.get((inst_id, book_side, price), ZERO)


def match_aggressive(
    *,
    inst_id: str,
    side: Side,
    contracts: Decimal,
    price_limit: Decimal | None,
    opposite_levels: Sequence[Level],
    reservation: DepthReservation | None = None,
) -> AggressiveResult:
    """Consomme les niveaux opposés (triés du meilleur au pire) dans la limite de prix et de la profondeur."""
    remaining = dec(contracts, field="contracts")
    if remaining <= 0:
        raise OrderStateError("quantité agressive non positive", inst_id=inst_id)
    book_side = book_side_for(side)
    fills: list[LevelFill] = []
    for price, displayed in opposite_levels:
        if remaining <= 0:
            break
        if price_limit is not None and (
            (side is Side.BUY and price > price_limit) or (side is Side.SELL and price < price_limit)
        ):
            break
        available = (
            reservation.available(inst_id, book_side, price, displayed)
            if reservation is not None
            else displayed
        )
        if available <= 0:
            continue
        take = min(available, remaining)
        fills.append(LevelFill(price, take))
        if reservation is not None:
            reservation.reserve(inst_id, book_side, price, take)
        remaining -= take
    filled = sum((f.contracts for f in fills), ZERO)
    notional = sum((f.price * f.contracts for f in fills), ZERO)
    vwap = notional / filled if filled > 0 else None
    reason = ReasonCode.LIQUIDITY_INSUFFICIENT.value if remaining > 0 else None
    return AggressiveResult(tuple(fills), filled, remaining, vwap, reason)


def post_only_would_cross(
    side: Side, price: Decimal, best_bid: Decimal | None, best_ask: Decimal | None
) -> bool:
    """Un post-only acheteur à un prix ≥ meilleur ask (ou vendeur ≤ meilleur bid) croiserait : rejet."""
    if side is Side.BUY:
        return best_ask is not None and price >= best_ask
    return best_bid is not None and price <= best_bid
