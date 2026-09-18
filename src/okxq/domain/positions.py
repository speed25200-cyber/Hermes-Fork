"""Position linéaire : quantité signée, coût d'ouverture exact, scission réduction/fermeture/réouverture (§38).

Le prix moyen d'entrée est DÉRIVÉ (``open_cost / qty``) et ne sert qu'à l'affichage : la comptabilité
travaille sur ``open_cost`` (somme signée exacte des notionnels d'entrée), ce qui garantit l'identité
``flux de trésorerie + valeur marquée = PnL réalisé + PnL latent`` sans erreur de division.

Fonctions pures : le ledger et le simulateur les appellent, elles ne persistent rien.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_EVEN, Decimal

from okxq.domain.errors import LedgerError
from okxq.domain.money import ZERO, dec

# Le coût libéré au prorata est quantifié à 12 décimales (précision NUMERIC des quantités/prix, §44) :
# il devient un décimal fini, toutes les soustractions suivantes sont exactes, et l'erreur de prorata
# (≤ 1e-12) reste dans open_cost jusqu'à la fermeture totale, où elle est réalisée exactement.
_COST_QUANTUM = Decimal(1).scaleb(-12)


@dataclass(frozen=True, slots=True)
class Position:
    inst_id: str
    base_ccy: str
    settle_ccy: str
    signed_base_qty: Decimal = ZERO
    open_cost: Decimal = ZERO  # Σ signée des notionnels d'entrée encore ouverts (long > 0, short < 0)
    closed_base_qty: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    version: int = 0
    average_entry_price: Decimal = field(init=False)

    def __post_init__(self) -> None:
        qty = self.signed_base_qty
        avg = abs(self.open_cost / qty) if qty != 0 else ZERO
        object.__setattr__(self, "average_entry_price", avg)

    @property
    def is_flat(self) -> bool:
        return self.signed_base_qty == 0

    @property
    def side_sign(self) -> int:
        return 0 if self.signed_base_qty == 0 else (1 if self.signed_base_qty > 0 else -1)

    def cost_basis(self) -> Decimal:
        return abs(self.open_cost)

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        """``signed_base_quantity * mark − open_cost`` = ``signed_base_quantity * (mark − average_entry)`` (§38)."""
        if self.signed_base_qty == 0:
            return ZERO
        return self.signed_base_qty * dec(mark_price, field="mark_price") - self.open_cost

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

    - même sens : le coût d'ouverture s'ajoute exactement ;
    - sens opposé : réduit puis ferme, réalise le PnL sur la part fermée (coût libéré au prorata), et
      ouvre le reliquat de l'autre côté au prix du fill (T39).
    """
    q = dec(signed_qty, field="signed_qty")
    p = dec(price, field="price")
    if q == 0:
        raise LedgerError("fill de quantité nulle", inst_id=position.inst_id)
    if p <= 0:
        raise LedgerError("prix de fill non positif", inst_id=position.inst_id)

    cur = position.signed_base_qty
    if cur == 0 or (cur > 0) == (q > 0):
        new_pos = replace(
            position,
            signed_base_qty=cur + q,
            open_cost=position.open_cost + q * p,
            version=position.version + 1,
        )
        return new_pos, FillSplit(reduce_qty=ZERO, open_qty=abs(q), flipped=False, realized_pnl=ZERO)

    side = 1 if cur > 0 else -1
    reduce_qty = min(abs(cur), abs(q))
    if reduce_qty == abs(cur):
        released_cost = position.open_cost  # fermeture totale : le coût libéré est exactement le coût ouvert
    else:
        released_cost = (position.open_cost * reduce_qty / abs(cur)).quantize(
            _COST_QUANTUM, rounding=ROUND_HALF_EVEN
        )
    realized = side * reduce_qty * p - released_cost
    remaining_cur = abs(cur) - reduce_qty
    open_qty = abs(q) - reduce_qty
    if remaining_cur > 0:
        new_pos = replace(
            position,
            signed_base_qty=side * remaining_cur,
            open_cost=position.open_cost - released_cost,
            closed_base_qty=position.closed_base_qty + reduce_qty,
            realized_pnl=position.realized_pnl + realized,
            version=position.version + 1,
        )
        return new_pos, FillSplit(reduce_qty=reduce_qty, open_qty=ZERO, flipped=False, realized_pnl=realized)
    if open_qty > 0:
        new_signed = -side * open_qty
        new_pos = replace(
            position,
            signed_base_qty=new_signed,
            open_cost=new_signed * p,
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
        open_cost=ZERO,
        closed_base_qty=position.closed_base_qty + reduce_qty,
        realized_pnl=position.realized_pnl + realized,
        version=position.version + 1,
    )
    return new_pos, FillSplit(reduce_qty=reduce_qty, open_qty=ZERO, flipped=False, realized_pnl=realized)
