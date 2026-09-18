"""Position linéaire : quantité signée, prix moyen, coût de base, scission réduction/fermeture/réouverture (§38).

Fonctions pures : le ledger et le simulateur les appellent, elles ne persistent rien.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from okxq.domain.errors import LedgerError
from okxq.domain.money import ZERO, dec


@dataclass(frozen=True, slots=True)
class Position:
    inst_id: str
    base_ccy: str
    settle_ccy: str
    signed_base_qty: Decimal = ZERO
    average_entry_price: Decimal = ZERO
    closed_base_qty: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    version: int = 0

    @property
    def is_flat(self) -> bool:
        return self.signed_base_qty == 0

    @property
    def side_sign(self) -> int:
        return 0 if self.signed_base_qty == 0 else (1 if self.signed_base_qty > 0 else -1)

    def cost_basis(self) -> Decimal:
        return abs(self.signed_base_qty) * self.average_entry_price

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        """``signed_base_quantity * (mark - average_entry)`` (§38)."""
        if self.signed_base_qty == 0:
            return ZERO
        return self.signed_base_qty * (dec(mark_price, field="mark_price") - self.average_entry_price)

    def signed_notional(self, reference_price: Decimal) -> Decimal:
        return self.signed_base_qty * dec(reference_price, field="reference_price")


@dataclass(frozen=True, slots=True)
class FillSplit:
    """Décomposition d'un fill : part qui réduit/ferme, part qui ouvre (éventuellement de l'autre côté)."""

    reduce_qty: Decimal  # quantité absolue qui réduit la position existante
    open_qty: Decimal  # quantité absolue qui ouvre (même côté ou côté opposé après flip)
    flipped: bool
    realized_pnl: Decimal


def apply_fill(position: Position, signed_qty: Decimal, price: Decimal) -> tuple[Position, FillSplit]:
    """Applique une exécution signée (en unité de base) à une position linéaire.

    - même sens : moyenne pondérée du prix d'entrée ;
    - sens opposé : réduit puis ferme, réalise le PnL sur la part fermée, et ouvre le reliquat de l'autre
      côté au prix du fill (T39).
    """
    q = dec(signed_qty, field="signed_qty")
    p = dec(price, field="price")
    if q == 0:
        raise LedgerError("fill de quantité nulle", inst_id=position.inst_id)
    if p <= 0:
        raise LedgerError("prix de fill non positif", inst_id=position.inst_id)

    cur = position.signed_base_qty
    if cur == 0 or (cur > 0) == (q > 0):
        new_qty = cur + q
        new_avg = (abs(cur) * position.average_entry_price + abs(q) * p) / abs(new_qty)
        new_pos = replace(
            position, signed_base_qty=new_qty, average_entry_price=new_avg, version=position.version + 1
        )
        return new_pos, FillSplit(reduce_qty=ZERO, open_qty=abs(q), flipped=False, realized_pnl=ZERO)

    reduce_qty = min(abs(cur), abs(q))
    side = 1 if cur > 0 else -1
    realized = side * reduce_qty * (p - position.average_entry_price)
    remaining_cur = abs(cur) - reduce_qty
    open_qty = abs(q) - reduce_qty
    if remaining_cur > 0:
        new_pos = replace(
            position,
            signed_base_qty=side * remaining_cur,
            closed_base_qty=position.closed_base_qty + reduce_qty,
            realized_pnl=position.realized_pnl + realized,
            version=position.version + 1,
        )
        return new_pos, FillSplit(reduce_qty=reduce_qty, open_qty=ZERO, flipped=False, realized_pnl=realized)
    if open_qty > 0:
        new_pos = replace(
            position,
            signed_base_qty=-side * open_qty,
            average_entry_price=p,
            closed_base_qty=position.closed_base_qty + reduce_qty,
            realized_pnl=position.realized_pnl + realized,
            version=position.version + 1,
        )
        return new_pos, FillSplit(
            reduce_qty=reduce_qty, open_qty=open_qty, flipped=True, realized_pnl=realized
        )
    new_pos = replace(
        position,
        signed_base_qty=ZERO,
        average_entry_price=ZERO,
        closed_base_qty=position.closed_base_qty + reduce_qty,
        realized_pnl=position.realized_pnl + realized,
        version=position.version + 1,
    )
    return new_pos, FillSplit(reduce_qty=reduce_qty, open_qty=ZERO, flipped=False, realized_pnl=realized)
