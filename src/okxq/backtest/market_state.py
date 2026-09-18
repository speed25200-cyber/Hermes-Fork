"""État de marché reconstruit depuis le flux d'événements (docs/event_schemas.md).

Carnets (snapshot + updates avec contrôle de séquence), trades, bougies (partielles et clôturées), mark,
funding, open interest, métadonnées d'instruments. Aucune logique de stratégie ; l'état est ce que le
système a réellement reçu, dans l'ordre ``(available_at, ingest_seq)``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from okxq.accounting.funding import FundingObservation, funding_rate_known_at, parse_funding_event
from okxq.domain.errors import BookInvalidError, DataQualityError, SequenceGapError
from okxq.domain.events import EventEnvelope
from okxq.domain.instruments import InstrumentSpec, InstrumentState, derive_base_units_per_contract
from okxq.domain.money import Side, dec

__all__ = [
    "Applied",
    "BookView",
    "Candle",
    "MarketState",
    "OrderBook",
    "Trade",
    "spec_from_instrument_event",
]

Level = tuple[Decimal, Decimal]


def ms_to_dt(ms: object, *, field_name: str = "ts_ms") -> datetime:
    try:
        return datetime.fromtimestamp(int(str(ms)) / 1000, tz=UTC)
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"{field_name} invalide : {ms!r}") from exc


def spec_from_instrument_event(
    payload: Mapping[str, Any], *, observed_at: datetime, provenance: str
) -> InstrumentSpec:
    """Événement ``instrument`` (clés snake_case du contrat partagé) → ``InstrumentSpec`` (règles §45)."""
    inst_id = str(payload["inst_id"])
    if str(payload.get("inst_type", "")) != "SWAP":
        raise DataQualityError("inst_type non SWAP", inst_id=inst_id)
    base = inst_id.split("-")[0]
    v = derive_base_units_per_contract(
        ct_val=str(payload["ct_val"]),
        ct_val_ccy=str(payload["ct_val_ccy"]),
        ct_type=str(payload["ct_type"]),
        ct_mult=str(payload.get("ct_mult", "1") or "1"),
        base_ccy=base,
        settle_ccy=str(payload["settle_ccy"]),
    )
    list_time = payload.get("list_time_ms")
    valid_from = (
        ms_to_dt(list_time, field_name="list_time_ms") if list_time not in (None, "") else observed_at
    )
    lever = payload.get("lever")
    return InstrumentSpec(
        inst_id=inst_id,
        valid_from=min(valid_from, observed_at),
        observed_at=observed_at,
        settle_ccy=str(payload["settle_ccy"]),
        base_ccy=base,
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=v,
        tick_size=dec(str(payload["tick_sz"]), field="tick_sz"),
        lot_size=dec(str(payload["lot_sz"]), field="lot_sz"),
        min_size=dec(str(payload["min_sz"]), field="min_sz"),
        state=InstrumentState.parse(str(payload.get("state", "unknown"))),
        provenance=provenance,
        max_leverage=dec(str(lever), field="lever") if lever not in (None, "") else None,
    )


@dataclass(frozen=True, slots=True)
class Trade:
    inst_id: str
    trade_id: str
    price: Decimal
    contracts: Decimal
    taker_side: Side
    ts: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class Candle:
    inst_id: str
    open_ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vol_contracts: Decimal
    confirmed: bool
    available_at: datetime


@dataclass(frozen=True, slots=True)
class BookView:
    inst_id: str
    ts: datetime | None
    version: int
    valid: bool
    bids: tuple[Level, ...]  # décroissants
    asks: tuple[Level, ...]  # croissants

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2


@dataclass(slots=True)
class OrderBook:
    inst_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    seq_id: int | None = None
    ts: datetime | None = None
    version: int = 0
    valid: bool = False
    gaps: int = 0

    def apply_snapshot(self, payload: Mapping[str, Any]) -> list[tuple[str, Decimal]]:
        self.bids = {dec(p): dec(q) for p, q in payload["bids"]}
        self.asks = {dec(p): dec(q) for p, q in payload["asks"]}
        self.seq_id = int(str(payload["seq_id"]))
        self.ts = ms_to_dt(payload["ts_ms"])
        self.version += 1
        self.valid = True
        self._check_crossed()
        return [("bids", p) for p in self.bids] + [("asks", p) for p in self.asks]

    def apply_update(self, payload: Mapping[str, Any]) -> list[tuple[str, Decimal]]:
        """Remplacement par niveau ; ``prev_seq_id`` doit égaler le dernier ``seq_id`` sinon le carnet est invalide."""
        prev = int(str(payload["prev_seq_id"]))
        seq = int(str(payload["seq_id"]))
        if self.seq_id is None or prev != self.seq_id:
            self.valid = False
            self.gaps += 1
            raise SequenceGapError(
                "trou de séquence dans le carnet", inst_id=self.inst_id, expected=self.seq_id, got=prev
            )
        replaced: list[tuple[str, Decimal]] = []
        for side_name, levels in (("bids", self.bids), ("asks", self.asks)):
            for raw_price, raw_qty in payload.get(side_name, []):
                price, qty = dec(raw_price), dec(raw_qty)
                if qty == 0:
                    levels.pop(price, None)
                else:
                    levels[price] = qty
                replaced.append((side_name, price))
        self.seq_id = seq
        self.ts = ms_to_dt(payload["ts_ms"])
        self.version += 1
        self._check_crossed()
        return replaced

    def _check_crossed(self) -> None:
        if self.bids and self.asks and max(self.bids) >= min(self.asks):
            self.valid = False
            raise BookInvalidError("carnet croisé", inst_id=self.inst_id)

    def bid_levels(self) -> tuple[Level, ...]:
        return tuple(sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True))

    def ask_levels(self) -> tuple[Level, ...]:
        return tuple(sorted(self.asks.items(), key=lambda kv: kv[0]))

    def view(self) -> BookView:
        return BookView(self.inst_id, self.ts, self.version, self.valid, self.bid_levels(), self.ask_levels())


@dataclass(frozen=True, slots=True)
class Applied:
    """Ce que l'application d'un événement a changé (consommé par l'exchange virtuel)."""

    event_type: str
    inst_id: str | None
    replaced_levels: tuple[tuple[str, Decimal], ...] = ()
    trade: Trade | None = None
    candle: Candle | None = None
    mark_price: Decimal | None = None
    funding: FundingObservation | None = None
    spec: InstrumentSpec | None = None


class MarketState:
    """Vue reconstruite ; lève ``DataQualityError`` sur un événement mal formé, sans jamais l'imputer."""

    def __init__(self, *, trade_window_seconds: int = 60, max_closed_candles: int = 600) -> None:
        self.specs: dict[str, InstrumentSpec] = {}
        self.books: dict[str, OrderBook] = {}
        self.last_trade: dict[str, Trade] = {}
        self.recent_trades: dict[str, deque[Trade]] = {}
        self.closed_candles: dict[str, deque[Candle]] = {}
        self.partial_candle: dict[str, Candle] = {}
        self.marks: dict[str, Decimal] = {}
        self.mark_ts: dict[str, datetime] = {}
        self.index_prices: dict[str, Decimal] = {}
        self.last_prices: dict[str, Decimal] = {}
        self.notional_volume_24h: dict[str, Decimal] = {}
        self.price_limits: dict[str, tuple[Decimal, Decimal]] = {}
        self.funding: list[FundingObservation] = []
        self.open_interest: dict[str, Decimal] = {}
        self.events_applied = 0
        self.last_available_at: datetime | None = None
        self._trade_window = trade_window_seconds
        self._max_candles = max_closed_candles

    # --- application ---------------------------------------------------------------------------------

    def apply(self, envelope: EventEnvelope) -> Applied:
        if self.last_available_at is not None and envelope.available_at < self.last_available_at:
            raise DataQualityError("événement hors ordre (available_at recule)", event_id=envelope.event_id)
        self.last_available_at = envelope.available_at
        self.events_applied += 1
        kind = envelope.event_type
        p = envelope.payload
        inst_id = str(p.get("inst_id", "")) or None
        if kind == "instrument":
            spec = spec_from_instrument_event(
                p, observed_at=envelope.receive_ts, provenance=f"event:{envelope.source}"
            )
            previous = self.specs.get(spec.inst_id)
            if previous is not None:
                spec = spec.with_version(previous.version + 1, envelope.receive_ts)
            self.specs[spec.inst_id] = spec
            return Applied(kind, spec.inst_id, spec=spec)
        if inst_id is None:
            raise DataQualityError("événement sans inst_id", event_type=kind)
        if kind == "book.snapshot":
            book = self.books.setdefault(inst_id, OrderBook(inst_id))
            return Applied(kind, inst_id, tuple(book.apply_snapshot(p)))
        if kind == "book.update":
            existing = self.books.get(inst_id)
            if existing is None:
                raise SequenceGapError("update sans snapshot préalable", inst_id=inst_id)
            return Applied(kind, inst_id, tuple(existing.apply_update(p)))
        if kind == "trade":
            trade = Trade(
                inst_id=inst_id,
                trade_id=str(p["trade_id"]),
                price=dec(str(p["price"]), field="price"),
                contracts=dec(str(p["qty_contracts"]), field="qty_contracts"),
                taker_side=Side(str(p["side"])),
                ts=ms_to_dt(p["ts_ms"]),
                available_at=envelope.available_at,
            )
            self.last_trade[inst_id] = trade
            window = self.recent_trades.setdefault(inst_id, deque())
            window.append(trade)
            horizon = trade.ts.timestamp() - self._trade_window
            while window and window[0].ts.timestamp() < horizon:
                window.popleft()
            return Applied(kind, inst_id, trade=trade)
        if kind == "candle.1m":
            candle = Candle(
                inst_id=inst_id,
                open_ts=ms_to_dt(p["ts_ms"]),
                open=dec(str(p["open"])),
                high=dec(str(p["high"])),
                low=dec(str(p["low"])),
                close=dec(str(p["close"])),
                vol_contracts=dec(str(p["vol_contracts"])),
                confirmed=str(p.get("confirm", "0")) == "1",
                available_at=envelope.available_at,
            )
            if candle.confirmed:
                closed = self.closed_candles.setdefault(inst_id, deque(maxlen=self._max_candles))
                closed.append(candle)
                self.partial_candle.pop(inst_id, None)
            else:
                self.partial_candle[inst_id] = candle
            return Applied(kind, inst_id, candle=candle)
        if kind == "mark_price":
            mark = dec(str(p["mark_px"]), field="mark_px")
            if mark <= 0:
                raise DataQualityError("mark non positif", inst_id=inst_id)
            self.marks[inst_id] = mark
            self.mark_ts[inst_id] = ms_to_dt(p["ts_ms"])
            return Applied(kind, inst_id, mark_price=mark)
        if kind == "funding":
            obs = parse_funding_event(envelope)
            self.funding.append(obs)
            return Applied(kind, inst_id, funding=obs)
        if kind == "open_interest":
            self.open_interest[inst_id] = dec(str(p["oi_contracts"]), field="oi_contracts")
            return Applied(kind, inst_id)
        if kind == "index_price":
            self.index_prices[inst_id] = dec(str(p["idx_px"]), field="idx_px")
            return Applied(kind, inst_id)
        if kind == "ticker":
            # volCcy24h est en devise de BASE : le notionnel exige une multiplication par le prix.
            last = p.get("last")
            base_volume = p.get("vol_ccy_24h_base")
            if last:
                self.last_prices[inst_id] = dec(str(last), field="last")
            if last and base_volume:
                self.notional_volume_24h[inst_id] = dec(str(base_volume), field="vol_ccy_24h_base") * dec(
                    str(last), field="last"
                )
            return Applied(kind, inst_id)
        if kind == "price_limit":
            buy, sell = p.get("buy_limit"), p.get("sell_limit")
            if buy and sell:
                self.price_limits[inst_id] = (
                    dec(str(buy), field="buy_limit"),
                    dec(str(sell), field="sell_limit"),
                )
            return Applied(kind, inst_id)
        raise DataQualityError("type d'événement inconnu", event_type=kind)

    # --- lectures ------------------------------------------------------------------------------------

    def book_view(self, inst_id: str) -> BookView | None:
        book = self.books.get(inst_id)
        return None if book is None else book.view()

    def book_views(self) -> dict[str, BookView]:
        return {i: b.view() for i, b in self.books.items()}

    def funding_known_at(self, inst_id: str, cutoff: datetime) -> FundingObservation | None:
        return funding_rate_known_at(self.funding, inst_id, cutoff)

    def settlement_times(self, inst_id: str) -> tuple[datetime, ...]:
        """Instants de règlement DÉCLARÉS par le contrat (lus dans les événements funding)."""
        times: set[datetime] = set()
        for obs in self.funding:
            if obs.inst_id == inst_id:
                times.add(obs.funding_time)
                times.add(obs.next_funding_time)
        return tuple(sorted(times))

    def observed_volume(self, inst_id: str) -> Decimal:
        return sum((t.contracts for t in self.recent_trades.get(inst_id, ())), Decimal(0))

    def closed(self, inst_id: str) -> Sequence[Candle]:
        return tuple(self.closed_candles.get(inst_id, ()))
