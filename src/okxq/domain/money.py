"""Monnaie et quantités à unités explicites (§27, §45).

Règles :
- les montants monétaires sont des ``Decimal`` ; les flottants sont refusés à l'entrée (``dec()``) sauf
  conversion explicite ``dec_from_float`` qui fixe le nombre de décimales ;
- une ``Money`` porte sa devise ; deux devises ne s'additionnent pas ;
- les arrondis ont une DIRECTION nommée : jamais d'arrondi « au plus proche » sur une taille de risque.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Self

from okxq.domain.errors import RoundingError, UnitError

ZERO = Decimal(0)
ONE = Decimal(1)

MONEY_PLACES = 8  # NUMERIC(28, 8) en base (§44)


def dec(value: str | int | Decimal, *, field: str = "value") -> Decimal:
    """Convertit sans passer par un flottant. Refuse NaN, infini et les floats."""
    if isinstance(value, bool):
        raise UnitError(f"{field} : booléen reçu pour un nombre", field=field)
    if isinstance(value, float):
        raise UnitError(
            f"{field} : flottant refusé ; utiliser dec_from_float(x, places) explicitement", field=field
        )
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise UnitError(f"{field} : nombre invalide {value!r}", field=field) from exc
    if not d.is_finite():
        raise UnitError(f"{field} : valeur non finie {value!r}", field=field)
    return d


def dec_from_float(value: float, places: int, *, field: str = "value") -> Decimal:
    """Conversion explicite d'un flottant (sorties de modèles) avec quantification déclarée."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UnitError(f"{field} : attendu un flottant", field=field)
    if not math.isfinite(value):
        raise UnitError(f"{field} : valeur non finie", field=field)
    return Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def quantize_money(amount: Decimal, places: int = MONEY_PLACES) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def is_multiple_of(value: Decimal, step: Decimal) -> bool:
    if step <= 0:
        raise UnitError("pas d'arrondi non positif", step=str(step))
    return (value / step) % 1 == 0


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Arrondi vers zéro sur une grille (augmentation de risque : jamais vers le haut)."""
    if step <= 0:
        raise UnitError("pas d'arrondi non positif", step=str(step))
    q = (abs(value) / step).to_integral_value(rounding=ROUND_DOWN) * step
    return -q if value < 0 else q


def round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise UnitError("pas d'arrondi non positif", step=str(step))
    q = (abs(value) / step).to_integral_value(rounding=ROUND_UP) * step
    return -q if value < 0 else q


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


def round_price_passive(price: Decimal, tick: Decimal, side: Side) -> Decimal:
    """Prix passif : un achat s'arrondit vers le BAS, une vente vers le HAUT — on ne franchit pas le carnet."""
    return round_down_to_step(price, tick) if side is Side.BUY else round_up_to_step(price, tick)


def round_price_aggressive_within_limit(price: Decimal, tick: Decimal, side: Side, limit: Decimal) -> Decimal:
    """Limite agressive : arrondi vers l'exécution, mais jamais au-delà de la limite préapprouvée (§45)."""
    rounded = round_up_to_step(price, tick) if side is Side.BUY else round_down_to_step(price, tick)
    if side is Side.BUY and rounded > limit:
        rounded = round_down_to_step(limit, tick)
        if rounded > limit:
            raise RoundingError("limite d'achat infranchissable après arrondi", price=str(price))
    if side is Side.SELL and rounded < limit:
        rounded = round_up_to_step(limit, tick)
        if rounded < limit:
            raise RoundingError("limite de vente infranchissable après arrondi", price=str(price))
    if rounded <= 0:
        raise RoundingError("aucun prix admissible strictement positif sous la limite", price=str(price))
    return rounded


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    ccy: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", dec(self.amount, field="amount"))
        if not self.ccy or self.ccy != self.ccy.upper():
            raise UnitError("devise vide ou non normalisée", ccy=self.ccy)

    @classmethod
    def zero(cls, ccy: str) -> Money:
        return cls(ZERO, ccy)

    @classmethod
    def of(cls, amount: str | int | Decimal, ccy: str) -> Money:
        return cls(dec(amount), ccy)

    def _check(self, other: Money) -> None:
        if other.ccy != self.ccy:
            raise UnitError("devises différentes", left=self.ccy, right=other.ccy)

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount + other.amount, self.ccy)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount - other.amount, self.ccy)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.ccy)

    def __mul__(self, factor: Decimal | int) -> Money:
        return Money(self.amount * Decimal(factor), self.ccy)

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.amount <= other.amount

    def is_zero(self) -> bool:
        return self.amount == 0

    def quantized(self, places: int = MONEY_PLACES) -> Self:
        return type(self)(quantize_money(self.amount, places), self.ccy)

    def __str__(self) -> str:
        return f"{format(self.amount, 'f')} {self.ccy}"


@dataclass(frozen=True, slots=True)
class Contracts:
    """Nombre SIGNÉ de contrats (positif = long/achat, négatif = short/vente)."""

    value: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", dec(self.value, field="contracts"))

    @property
    def abs(self) -> Decimal:
        return abs(self.value)

    @property
    def sign(self) -> int:
        return 0 if self.value == 0 else (1 if self.value > 0 else -1)

    def __add__(self, other: Contracts) -> Contracts:
        return Contracts(self.value + other.value)

    def __neg__(self) -> Contracts:
        return Contracts(-self.value)

    def is_zero(self) -> bool:
        return self.value == 0


@dataclass(frozen=True, slots=True)
class BaseQty:
    """Quantité SIGNÉE en unité de base (ex. BTC), jamais confondue avec des contrats ou un notionnel."""

    value: Decimal
    ccy: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", dec(self.value, field="base_qty"))
        if not self.ccy:
            raise UnitError("devise de base vide")

    @property
    def sign(self) -> int:
        return 0 if self.value == 0 else (1 if self.value > 0 else -1)

    def __add__(self, other: BaseQty) -> BaseQty:
        if other.ccy != self.ccy:
            raise UnitError("unités de base différentes", left=self.ccy, right=other.ccy)
        return BaseQty(self.value + other.value, self.ccy)

    def __neg__(self) -> BaseQty:
        return BaseQty(-self.value, self.ccy)

    def is_zero(self) -> bool:
        return self.value == 0
