"""Lecture des événements de marché (contrat ``docs/event_schemas.md``) vers des enregistrements typés.

Tous les nombres du payload sont des chaînes : ils passent par ``dec()`` (refus de NaN/inf/float) puis sont
convertis en flottants pour les features. Les horodatages ``*_ms`` deviennent des datetimes UTC.
Un événement mal formé lève ``DataQualityError`` : aucun parser permissif (AGENTS.md §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from okxq.domain.errors import DataQualityError
from okxq.domain.events import EventEnvelope
from okxq.domain.money import dec
from okxq.features.state import FundingRecord

MARKET_EVENT_TYPES = frozenset(
    {
        "instrument",
        "book.snapshot",
        "book.update",
        "trade",
        "candle.1m",
        "mark_price",
        "index_price",
        "funding",
        "open_interest",
    }
)


def ms_to_dt(value: Any, *, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000.0, tz=UTC)
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"{field} : horodatage ms invalide {value!r}", field=field) from exc


def num(value: Any, *, field: str) -> float:
    if value is None:
        raise DataQualityError(f"{field} absent", field=field)
    return float(dec(str(value), field=field))


def _levels(raw: Any, *, field: str) -> list[tuple[float, float]]:
    if not isinstance(raw, list):
        raise DataQualityError(f"{field} : liste de niveaux attendue", field=field)
    out: list[tuple[float, float]] = []
    for lvl in raw:
        if not isinstance(lvl, list | tuple) or len(lvl) < 2:
            raise DataQualityError(f"{field} : niveau [prix, qty] attendu", field=field)
        out.append((num(lvl[0], field=f"{field}.price"), num(lvl[1], field=f"{field}.qty")))
    return out


@dataclass(frozen=True, slots=True)
class InstrumentRec:
    inst_id: str
    base_units_per_contract: float
    tick_size: float
    available_at: datetime


@dataclass(frozen=True, slots=True)
class BookRec:
    inst_id: str
    ts: datetime
    available_at: datetime
    is_snapshot: bool
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    seq_id: int | None


@dataclass(frozen=True, slots=True)
class TradeRec:
    inst_id: str
    ts: datetime
    available_at: datetime
    price: float
    qty: float
    side_sign: int


@dataclass(frozen=True, slots=True)
class CandleRec:
    inst_id: str
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
class PriceRec:
    inst_id: str
    kind: str  # mark | index
    ts: datetime
    available_at: datetime
    value: float


@dataclass(frozen=True, slots=True)
class FundingRec:
    inst_id: str
    record: FundingRecord


@dataclass(frozen=True, slots=True)
class OiRec:
    inst_id: str
    ts: datetime
    available_at: datetime
    oi_contracts: float


ParsedEvent = InstrumentRec | BookRec | TradeRec | CandleRec | PriceRec | FundingRec | OiRec


def parse_envelope(env: EventEnvelope) -> ParsedEvent | None:
    """Traduit une enveloppe en enregistrement typé ; ``None`` pour un type hors périmètre marché."""
    p = env.payload
    t = env.event_type
    if t not in MARKET_EVENT_TYPES:
        return None
    inst = str(p.get("inst_id", ""))
    if not inst:
        raise DataQualityError("inst_id absent", event_type=t)
    if t == "instrument":
        if (
            str(p.get("ct_type")) != "linear"
            or str(p.get("settle_ccy")) != "USDT"
            or str(p.get("ct_mult", "1")) != "1"
        ):
            raise DataQualityError("instrument non linéaire USDT ctMult=1 : hors périmètre", inst_id=inst)
        return InstrumentRec(
            inst,
            num(p.get("ct_val"), field="ct_val"),
            num(p.get("tick_sz"), field="tick_sz"),
            env.available_at,
        )
    if t in ("book.snapshot", "book.update"):
        seq = p.get("seq_id")
        return BookRec(
            inst,
            ms_to_dt(p.get("ts_ms"), field="ts_ms"),
            env.available_at,
            t == "book.snapshot",
            _levels(p.get("bids", []), field="bids"),
            _levels(p.get("asks", []), field="asks"),
            int(str(seq)) if seq not in (None, "") else None,
        )
    if t == "trade":
        side = str(p.get("side"))
        if side not in ("buy", "sell"):
            raise DataQualityError(f"side de trade invalide {side!r}", inst_id=inst)
        return TradeRec(
            inst,
            ms_to_dt(p.get("ts_ms"), field="ts_ms"),
            env.available_at,
            num(p.get("price"), field="price"),
            num(p.get("qty_contracts"), field="qty_contracts"),
            1 if side == "buy" else -1,
        )
    if t == "candle.1m":
        confirm = str(p.get("confirm"))
        if confirm not in ("0", "1"):
            raise DataQualityError(f"confirm invalide {confirm!r}", inst_id=inst)
        return CandleRec(
            inst,
            ms_to_dt(p.get("ts_ms"), field="ts_ms"),
            env.available_at,
            num(p.get("open"), field="open"),
            num(p.get("high"), field="high"),
            num(p.get("low"), field="low"),
            num(p.get("close"), field="close"),
            num(p.get("vol_contracts"), field="vol_contracts"),
            num(p.get("vol_quote"), field="vol_quote"),
            confirm == "1",
        )
    if t == "mark_price":
        return PriceRec(
            inst,
            "mark",
            ms_to_dt(p.get("ts_ms"), field="ts_ms"),
            env.available_at,
            num(p.get("mark_px"), field="mark_px"),
        )
    if t == "index_price":
        return PriceRec(
            inst,
            "index",
            ms_to_dt(p.get("ts_ms"), field="ts_ms"),
            env.available_at,
            num(p.get("idx_px"), field="idx_px"),
        )
    if t == "funding":
        settled = p.get("settled")
        if not isinstance(settled, bool):
            raise DataQualityError("funding.settled doit être un booléen", inst_id=inst)
        realized = p.get("realized_rate")
        nfr = p.get("next_funding_rate")
        return FundingRec(
            inst,
            FundingRecord(
                ts=ms_to_dt(p.get("funding_time_ms"), field="funding_time_ms")
                if env.exchange_ts is None
                else env.exchange_ts,
                available_at=env.available_at,
                funding_rate=num(p.get("funding_rate"), field="funding_rate"),
                next_funding_rate=num(nfr, field="next_funding_rate") if nfr not in (None, "") else None,
                funding_time=ms_to_dt(p.get("funding_time_ms"), field="funding_time_ms"),
                next_funding_time=ms_to_dt(p.get("next_funding_time_ms"), field="next_funding_time_ms"),
                settled=settled,
                realized_rate=num(realized, field="realized_rate") if realized not in (None, "") else None,
            ),
        )
    if t == "open_interest":
        return OiRec(
            inst,
            ms_to_dt(p.get("ts_ms"), field="ts_ms"),
            env.available_at,
            num(p.get("oi_contracts"), field="oi_contracts"),
        )
    return None  # pragma: no cover - couvert par MARKET_EVENT_TYPES


def sort_key(env: EventEnvelope) -> tuple[datetime, int]:
    """Ordre de replay : ``(available_at, ingest_seq)`` (docs/event_schemas.md)."""
    return (env.available_at, env.ingest_seq)
