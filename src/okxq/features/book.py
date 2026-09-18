"""Reconstruction du carnet à partir de ``book.snapshot`` / ``book.update`` (docs/event_schemas.md).

Une quantité d'un ``book.update`` REMPLACE le niveau ; ``0`` le supprime. Un update reçu avant tout
snapshot est ignoré (le carnet n'est pas reconstructible) et compté dans ``dropped_updates``. Ce module est
partagé par le batch et l'incrémental : la parité (T16) porte donc sur le fenêtrage, pas sur le carnet.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from okxq.features.market_events import BookRec
from okxq.features.microstructure import book_invalid_reason
from okxq.features.state import BookState


class LiveBook:
    def __init__(self, inst_id: str, *, max_levels: int = 5) -> None:
        self.inst_id = inst_id
        self.max_levels = max_levels
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._has_snapshot = False
        self.last_ts: datetime | None = None
        self.last_available_at: datetime | None = None
        self.seq_id: int | None = None
        self.dropped_updates = 0

    def apply(self, rec: BookRec) -> BookState | None:
        """Applique l'événement et retourne l'état résultant (None si inexploitable)."""
        if rec.is_snapshot:
            self._bids = {}
            self._asks = {}
            self._has_snapshot = True
        elif not self._has_snapshot:
            self.dropped_updates += 1
            return None
        for px, qty in rec.bids:
            if qty == 0:
                self._bids.pop(px, None)
            else:
                self._bids[px] = qty
        for px, qty in rec.asks:
            if qty == 0:
                self._asks.pop(px, None)
            else:
                self._asks[px] = qty
        self.last_ts = rec.ts
        self.last_available_at = rec.available_at
        self.seq_id = rec.seq_id
        return self.state()

    def state(self) -> BookState | None:
        if not self._has_snapshot or self.last_ts is None or self.last_available_at is None:
            return None
        bids = sorted(self._bids.items(), key=lambda kv: -kv[0])[: self.max_levels]
        asks = sorted(self._asks.items(), key=lambda kv: kv[0])[: self.max_levels]
        return BookState(
            ts=self.last_ts,
            available_at=self.last_available_at,
            bid_px=np.array([p for p, _ in bids], dtype=float),
            bid_qty=np.array([q for _, q in bids], dtype=float),
            ask_px=np.array([p for p, _ in asks], dtype=float),
            ask_qty=np.array([q for _, q in asks], dtype=float),
            seq_id=self.seq_id,
        )


def history_point(book: BookState, v: float) -> tuple[float, float, float] | None:
    """(mid, spread relatif, notionnel L1 deux côtés) d'un carnet VALIDE ; None si invalide."""
    if book_invalid_reason(book) is not None:
        return None
    bid1, ask1 = float(book.bid_px[0]), float(book.ask_px[0])
    mid = (bid1 + ask1) / 2.0
    depth = (bid1 * float(book.bid_qty[0]) + ask1 * float(book.ask_qty[0])) * v
    return mid, (ask1 - bid1) / mid, depth
