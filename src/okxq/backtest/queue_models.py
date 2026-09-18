"""Position dans la file d'un ordre passif (§33.2) : hypothèses pessimiste et centrale.

Règle commune : SEUL le volume de trades imprimé à notre prix, côté opposé (un taker vendeur consomme
les bids), fait avancer la file. Les annulations d'autres participants ne nous avancent jamais (on ne
sait pas si elles étaient devant ou derrière nous). Un trade au-delà de notre prix ne nous remplit pas
non plus : le balayage d'un niveau produit ses propres impressions à ce prix.

- PESSIMISTIC : à l'arrivée, toute la quantité affichée au niveau est devant nous ;
- CENTRAL : la moitié de la quantité affichée est devant nous.

La différence entre les deux est une SENSIBILITÉ mesurable (volume nécessaire avant premier fill).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from okxq.domain.errors import OrderStateError
from okxq.domain.money import ZERO, Side, dec

__all__ = ["QueueAssumption", "QueueState", "initial_ahead"]


class QueueAssumption(StrEnum):
    PESSIMISTIC = "pessimistic"
    CENTRAL = "central"


def initial_ahead(displayed_qty: Decimal, assumption: QueueAssumption) -> Decimal:
    shown = dec(displayed_qty, field="displayed_qty")
    if shown < 0:
        raise OrderStateError("quantité affichée négative")
    if assumption is QueueAssumption.PESSIMISTIC:
        return shown
    return shown / 2


@dataclass(slots=True)
class QueueState:
    inst_id: str
    side: Side
    price: Decimal
    remaining: Decimal
    ahead: Decimal
    assumption: QueueAssumption
    filled: Decimal = ZERO
    traded_at_price: Decimal = field(default=ZERO)  # volume observé à notre prix depuis l'arrivée

    @property
    def is_done(self) -> bool:
        return self.remaining <= 0

    @property
    def volume_needed_for_first_fill(self) -> Decimal:
        """Sensibilité : volume qui doit encore s'imprimer à notre prix avant notre premier contrat."""
        return max(self.ahead, ZERO)

    def on_trade(self, price: Decimal, qty: Decimal, taker_side: Side) -> Decimal:
        """Applique un trade observé ; renvoie la quantité qui NOUS est exécutée (0 le plus souvent)."""
        if self.is_done or price != self.price or taker_side is self.side:
            return ZERO
        volume = dec(qty, field="qty")
        if volume <= 0:
            return ZERO
        self.traded_at_price += volume
        if self.ahead > 0:
            consumed = min(self.ahead, volume)
            self.ahead -= consumed
            volume -= consumed
        fill = min(volume, self.remaining)
        if fill <= 0:
            return ZERO
        self.remaining -= fill
        self.filled += fill
        return fill

    def cancel_remaining(self) -> Decimal:
        left = self.remaining
        self.remaining = ZERO
        return left
