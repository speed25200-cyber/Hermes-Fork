"""État de marché point-in-time (§35) : l'entrée unique du moteur de features.

Un ``PointInTimeMarketState`` ne contient que des données dont ``available_at <= cutoff_at`` ; la
construction le vérifie et lève ``CausalityError`` sinon. Les prix et quantités sont convertis en
flottants à cette frontière : les features sont des grandeurs STATISTIQUES, jamais des montants
comptables (les montants restent en ``Decimal`` dans le domaine).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import CausalityError
from okxq.domain.events import JevEvaluation


def to_epoch_s(dt: datetime) -> float:
    return ensure_utc(dt).timestamp()


def _check_sorted(ts: np.ndarray, what: str) -> None:
    if ts.size > 1 and bool(np.any(np.diff(ts) < 0)):
        raise ValueError(f"{what} : horodatages non triés")


@dataclass(frozen=True, slots=True)
class BookState:
    """Carnet à la coupure : niveaux triés (bids décroissants, asks croissants)."""

    ts: datetime
    available_at: datetime
    bid_px: np.ndarray
    bid_qty: np.ndarray
    ask_px: np.ndarray
    ask_qty: np.ndarray
    seq_id: int | None = None

    @property
    def is_empty(self) -> bool:
        return self.bid_px.size == 0 or self.ask_px.size == 0


@dataclass(frozen=True, slots=True)
class MidHistory:
    """Historique des mises à jour de carnet dans la fenêtre : mid, spread, profondeur L1 (pour RV, régimes)."""

    ts_s: np.ndarray
    mid: np.ndarray
    rel_spread: np.ndarray
    depth_notional: np.ndarray

    def __post_init__(self) -> None:
        _check_sorted(self.ts_s, "book_history")

    @property
    def size(self) -> int:
        return int(self.ts_s.size)


@dataclass(frozen=True, slots=True)
class TradeWindow:
    """Trades de la fenêtre glissante, triés par horodatage. ``side_sign`` : +1 taker achète, -1 taker vend."""

    ts_s: np.ndarray
    price: np.ndarray
    qty: np.ndarray
    side_sign: np.ndarray

    def __post_init__(self) -> None:
        _check_sorted(self.ts_s, "trades")

    @property
    def size(self) -> int:
        return int(self.ts_s.size)


@dataclass(frozen=True, slots=True)
class CandleWindow:
    """Bougies CLÔTURÉES (confirm:"1"), triées par ouverture, sans trou requis (les trous sont détectés)."""

    open_ts_s: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray  # contrats
    volume_quote: np.ndarray  # notionnel USDT
    bar_s: int = 60

    def __post_init__(self) -> None:
        _check_sorted(self.open_ts_s, "candles")

    @property
    def size(self) -> int:
        return int(self.open_ts_s.size)

    @property
    def last_close_ts_s(self) -> float | None:
        if self.size == 0:
            return None
        return float(self.open_ts_s[-1]) + self.bar_s

    def tail(self, n: int) -> CandleWindow:
        return CandleWindow(
            self.open_ts_s[-n:],
            self.open[-n:],
            self.high[-n:],
            self.low[-n:],
            self.close[-n:],
            self.volume[-n:],
            self.volume_quote[-n:],
            self.bar_s,
        )


@dataclass(frozen=True, slots=True)
class Candle:
    open_ts: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    volume_quote: float
    confirmed: bool


@dataclass(frozen=True, slots=True)
class PricePoint:
    ts: datetime
    available_at: datetime
    value: float


@dataclass(frozen=True, slots=True)
class FundingRecord:
    """Un événement ``funding`` : estimation (``settled=False``) ou montant réglé (``settled=True``)."""

    ts: datetime
    available_at: datetime
    funding_rate: float
    next_funding_rate: float | None
    funding_time: datetime
    next_funding_time: datetime
    settled: bool
    realized_rate: float | None


@dataclass(frozen=True, slots=True)
class OiWindow:
    ts_s: np.ndarray
    oi_contracts: np.ndarray

    def __post_init__(self) -> None:
        _check_sorted(self.ts_s, "open_interest")

    @property
    def size(self) -> int:
        return int(self.ts_s.size)


@dataclass(frozen=True, slots=True)
class AnnouncementMeta:
    """Métadonnées SEULES d'une annonce (variante B, §40) : aucune sémantique, aucun texte."""

    document_id: str
    first_seen_at: datetime
    available_at: datetime
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PointInTimeMarketState:
    instrument: str
    cutoff_at: datetime
    book: BookState | None = None
    book_history: MidHistory | None = None
    trades: TradeWindow | None = None
    closed_candles: CandleWindow | None = None
    intrabar: Candle | None = None
    mark: PricePoint | None = None
    index: PricePoint | None = None
    funding: Sequence[FundingRecord] = ()
    open_interest: OiWindow | None = None
    announcements: Sequence[AnnouncementMeta] = ()
    announcement_feed_available: bool = False  # False = flux inconnu → features méta MISSING (pas de 0)
    jev_evaluations: Sequence[JevEvaluation] = ()
    peers: Mapping[str, CandleWindow] = field(default_factory=dict)
    eligible_instruments: Sequence[str] = ()
    base_units_per_contract: float = 1.0  # v (§45) : sert aux notionnels de profondeur ; 1.0 = non renseigné

    def __post_init__(self) -> None:
        if not math.isfinite(self.base_units_per_contract) or self.base_units_per_contract <= 0:
            raise ValueError("base_units_per_contract doit être un flottant strictement positif")
        cutoff = ensure_utc(self.cutoff_at, field="cutoff_at")
        object.__setattr__(self, "cutoff_at", cutoff)
        for label, point in (
            ("book", self.book),
            ("intrabar", self.intrabar),
            ("mark", self.mark),
            ("index", self.index),
        ):
            if point is not None and ensure_utc(point.available_at) > cutoff:
                raise CausalityError(
                    f"{label} disponible après la coupure", instrument=self.instrument, field=label
                )
        for rec in self.funding:
            if ensure_utc(rec.available_at) > cutoff:
                raise CausalityError("funding disponible après la coupure", instrument=self.instrument)
        for ann in self.announcements:
            if ensure_utc(ann.available_at) > cutoff:
                raise CausalityError("annonce disponible après la coupure", instrument=self.instrument)
        for ev in self.jev_evaluations:
            if ev.features_committed_at is not None and ev.features_committed_at > cutoff:
                raise CausalityError("évaluation JEV engagée après la coupure", instrument=self.instrument)
        cutoff_s = cutoff.timestamp()
        if self.trades is not None and self.trades.size and float(self.trades.ts_s[-1]) > cutoff_s:
            raise CausalityError("trade postérieur à la coupure", instrument=self.instrument)
        if self.book_history is not None and self.book_history.size:
            if float(self.book_history.ts_s[-1]) > cutoff_s:
                raise CausalityError("mise à jour de carnet postérieure à la coupure")
        for name, cw in [("self", self.closed_candles), *list(self.peers.items())]:
            if (
                cw is not None
                and cw.size
                and cw.last_close_ts_s is not None
                and cw.last_close_ts_s > cutoff_s
            ):
                raise CausalityError("bougie clôturée après la coupure : non disponible", instrument=name)
        if self.open_interest is not None and self.open_interest.size:
            if float(self.open_interest.ts_s[-1]) > cutoff_s:
                raise CausalityError("open interest postérieur à la coupure")

    @property
    def cutoff_s(self) -> float:
        return self.cutoff_at.timestamp()


def empty_candles(bar_s: int = 60) -> CandleWindow:
    e = np.zeros(0, dtype=float)
    return CandleWindow(e, e, e, e, e, e, e, bar_s)


def empty_trades() -> TradeWindow:
    e = np.zeros(0, dtype=float)
    return TradeWindow(e, e, e, e)
