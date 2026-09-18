"""Assemblage BATCH des états point-in-time et calcul vectoriel des features (recherche, polars).

``BatchStateBuilder`` ordonne les événements par ``(available_at, ingest_seq)``, reconstruit les carnets
une fois, puis sert des ``PointInTimeMarketState`` par (instrument, coupure) en n'utilisant que ce dont
``available_at <= cutoff``. ``compute_batch`` applique ``FeatureEngine.compute`` (la référence) et rend un
``polars.DataFrame`` : une colonne par feature (nullable) et une colonne ``<feature>__mask``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import polars as pl

from okxq.domain.clocks import ensure_utc
from okxq.domain.events import EventEnvelope, JevEvaluation, QualityFlag
from okxq.features.book import LiveBook, history_point
from okxq.features.market_events import (
    BookRec,
    CandleRec,
    FundingRec,
    InstrumentRec,
    OiRec,
    PriceRec,
    TradeRec,
    parse_envelope,
    sort_key,
)
from okxq.features.registry import FeatureComputation, FeatureEngine
from okxq.features.state import (
    AnnouncementMeta,
    BookState,
    Candle,
    CandleWindow,
    FundingRecord,
    MidHistory,
    OiWindow,
    PointInTimeMarketState,
    PricePoint,
    TradeWindow,
    empty_candles,
    empty_trades,
)

HISTORY_WINDOW_S = 300
TRADE_LOOKBACK_S = 300
CANDLE_BARS = 61
OI_LOOKBACK_S = 4200
MAX_TRADE_TAIL = 20000
FUNDING_KEEP = 16


@dataclass
class _Series:
    """Tableaux triés par ``available_at`` (sélection point-in-time en O(log n))."""

    v: float = 1.0
    v_available_at: datetime | None = None
    book_states: list[BookState] = field(default_factory=list)
    book_avail_s: list[float] = field(default_factory=list)
    book_ts_s: list[float] = field(default_factory=list)
    book_hist: list[tuple[float, float, float] | None] = field(default_factory=list)
    trade_avail_s: list[float] = field(default_factory=list)
    trade_ts_s: list[float] = field(default_factory=list)
    trade_px: list[float] = field(default_factory=list)
    trade_qty: list[float] = field(default_factory=list)
    trade_side: list[int] = field(default_factory=list)
    candles: list[CandleRec] = field(default_factory=list)
    intrabars: list[CandleRec] = field(default_factory=list)
    marks: list[PriceRec] = field(default_factory=list)
    indexes: list[PriceRec] = field(default_factory=list)
    funding: list[FundingRecord] = field(default_factory=list)
    oi: list[OiRec] = field(default_factory=list)
    # tableaux numpy figés après ``freeze``
    _book_avail: np.ndarray | None = None
    _trade_avail: np.ndarray | None = None
    _candle_avail: np.ndarray | None = None
    _candle_open: np.ndarray | None = None
    _cols: dict[str, np.ndarray] = field(default_factory=dict)

    def freeze(self) -> None:
        self._book_avail = np.array(self.book_avail_s, dtype=float)
        self._trade_avail = np.array(self.trade_avail_s, dtype=float)
        self.candles.sort(key=lambda c: (c.open_ts, c.available_at))
        self._candle_avail = np.array([c.available_at.timestamp() for c in self.candles], dtype=float)
        self._candle_open = np.array([c.open_ts.timestamp() for c in self.candles], dtype=float)
        self._cols = {
            "open": np.array([c.open for c in self.candles], dtype=float),
            "high": np.array([c.high for c in self.candles], dtype=float),
            "low": np.array([c.low for c in self.candles], dtype=float),
            "close": np.array([c.close for c in self.candles], dtype=float),
            "volume": np.array([c.volume for c in self.candles], dtype=float),
            "volume_quote": np.array([c.volume_quote for c in self.candles], dtype=float),
            "trade_ts": np.array(self.trade_ts_s, dtype=float),
            "trade_px": np.array(self.trade_px, dtype=float),
            "trade_qty": np.array(self.trade_qty, dtype=float),
            "trade_side": np.array(self.trade_side, dtype=float),
            "book_ts": np.array(self.book_ts_s, dtype=float),
        }

    def closed_candles(self, cutoff_s: float, bars: int = CANDLE_BARS) -> CandleWindow:
        assert self._candle_avail is not None and self._candle_open is not None
        sel = (self._candle_avail <= cutoff_s) & (self._candle_open + 60.0 <= cutoff_s)
        idx = np.nonzero(sel)[0]
        if idx.size == 0:
            return empty_candles()
        # dédoublonnage par ouverture : une correction crée un nouvel événement, on garde le dernier disponible
        opens = self._candle_open[idx]
        keep = np.ones(idx.size, dtype=bool)
        keep[:-1] = opens[:-1] != opens[1:]
        idx = idx[keep][-bars:]
        c = self._cols
        return CandleWindow(
            self._candle_open[idx],
            c["open"][idx],
            c["high"][idx],
            c["low"][idx],
            c["close"][idx],
            c["volume"][idx],
            c["volume_quote"][idx],
            60,
        )

    def book_at(self, cutoff_s: float) -> BookState | None:
        assert self._book_avail is not None
        n = int(np.searchsorted(self._book_avail, cutoff_s, side="right"))
        return self.book_states[n - 1] if n > 0 else None

    def history(self, cutoff_s: float) -> MidHistory | None:
        assert self._book_avail is not None
        n = int(np.searchsorted(self._book_avail, cutoff_s, side="right"))
        if n == 0:
            return None
        ts = self._cols["book_ts"][:n]
        sel = np.nonzero((ts > cutoff_s - HISTORY_WINDOW_S) & (ts <= cutoff_s))[0]
        pts = [(ts[i], self.book_hist[i]) for i in sel if self.book_hist[i] is not None]
        pts.sort(key=lambda t: t[0])
        if not pts:
            return MidHistory(*(np.zeros(0) for _ in range(4)))
        arr = np.array([[t, h[0], h[1], h[2]] for t, h in pts if h is not None], dtype=float)
        return MidHistory(arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3])

    def trades(self, cutoff_s: float) -> TradeWindow:
        assert self._trade_avail is not None
        n = int(np.searchsorted(self._trade_avail, cutoff_s, side="right"))
        if n == 0:
            return empty_trades()
        lo = max(0, n - MAX_TRADE_TAIL)
        ts = self._cols["trade_ts"][lo:n]
        sel = np.nonzero((ts > cutoff_s - TRADE_LOOKBACK_S) & (ts <= cutoff_s))[0]
        order = sel[np.argsort(ts[sel], kind="stable")]
        return TradeWindow(
            ts[order],
            self._cols["trade_px"][lo:n][order],
            self._cols["trade_qty"][lo:n][order],
            self._cols["trade_side"][lo:n][order],
        )


def _last_available(items: Sequence[PriceRec], cutoff: datetime) -> PricePoint | None:
    best: PriceRec | None = None
    for it in items:
        if it.available_at <= cutoff and (
            best is None or (it.available_at, it.ts) >= (best.available_at, best.ts)
        ):
            best = it
    return PricePoint(best.ts, best.available_at, best.value) if best else None


class BatchStateBuilder:
    def __init__(self, events: Iterable[EventEnvelope], *, max_levels: int = 5) -> None:
        self._series: dict[str, _Series] = defaultdict(_Series)
        books: dict[str, LiveBook] = {}
        ordered = sorted(events, key=sort_key)
        self.first_available_at: datetime | None = ordered[0].available_at if ordered else None
        self.last_available_at: datetime | None = ordered[-1].available_at if ordered else None
        self.event_count = len(ordered)
        for env in ordered:
            rec = parse_envelope(env)
            if rec is None:
                continue
            s = self._series[rec.inst_id]
            if isinstance(rec, InstrumentRec):
                s.v = rec.base_units_per_contract
                s.v_available_at = rec.available_at
            elif isinstance(rec, BookRec):
                lb = books.setdefault(rec.inst_id, LiveBook(rec.inst_id, max_levels=max_levels))
                st = lb.apply(rec)
                if st is not None:
                    s.book_states.append(st)
                    s.book_avail_s.append(st.available_at.timestamp())
                    s.book_ts_s.append(st.ts.timestamp())
                    s.book_hist.append(history_point(st, s.v))
            elif isinstance(rec, TradeRec):
                s.trade_avail_s.append(rec.available_at.timestamp())
                s.trade_ts_s.append(rec.ts.timestamp())
                s.trade_px.append(rec.price)
                s.trade_qty.append(rec.qty)
                s.trade_side.append(rec.side_sign)
            elif isinstance(rec, CandleRec):
                (s.candles if rec.confirmed else s.intrabars).append(rec)
            elif isinstance(rec, PriceRec):
                (s.marks if rec.kind == "mark" else s.indexes).append(rec)
            elif isinstance(rec, FundingRec):
                s.funding.append(rec.record)
            elif isinstance(rec, OiRec):
                s.oi.append(rec)
        for s in self._series.values():
            s.freeze()
        self.dropped_book_updates = {k: b.dropped_updates for k, b in books.items() if b.dropped_updates}

    @property
    def instruments(self) -> list[str]:
        return sorted(self._series)

    def candles_for(self, inst: str, cutoff: datetime, bars: int = CANDLE_BARS) -> CandleWindow:
        s = self._series.get(inst)
        return s.closed_candles(cutoff.timestamp(), bars) if s is not None else empty_candles()

    def price_path(self, inst: str, start: datetime, end: datetime, *, step_s: int = 60) -> pl.DataFrame:
        """Chemin de prix pour les labels : grille ``step_s`` ; mid/bid/ask du dernier carnet VALIDE dont
        ``ts <= grille`` ; high/low/volume de la bougie clôturée se terminant à la grille ; ``available_at`` =
        disponibilité réelle de l'observation la plus tardive utilisée (le carnet ou la bougie)."""
        s = self._series[inst]
        assert s._candle_open is not None and s._candle_avail is not None
        start_s, end_s = ensure_utc(start).timestamp(), ensure_utc(end).timestamp()
        book_ts = s._cols["book_ts"]
        order = np.argsort(book_ts, kind="stable")
        sorted_ts = book_ts[order]
        candle_close = s._candle_open + 60.0
        rows: list[dict[str, object]] = []
        t = start_s
        while t <= end_s:
            n = int(np.searchsorted(sorted_ts, t, side="right"))
            found = None
            for j in range(n - 1, max(-1, n - 50), -1):
                i = int(order[j])
                h = s.book_hist[i]
                if h is not None:
                    found = (i, h)
                    break
            if found is not None:
                i, h = found
                st = s.book_states[i]
                c_idx = np.nonzero(candle_close == t)[0]
                if c_idx.size:
                    ci = int(c_idx[-1])
                    high, low, vol = (
                        float(s._cols["high"][ci]),
                        float(s._cols["low"][ci]),
                        float(s._cols["volume"][ci]),
                    )
                    avail = max(st.available_at.timestamp(), float(s._candle_avail[ci]))
                else:
                    high = low = h[0]
                    vol = 0.0
                    avail = st.available_at.timestamp()
                rows.append(
                    {
                        "ts": datetime.fromtimestamp(t, tz=UTC),
                        "available_at": datetime.fromtimestamp(avail, tz=UTC),
                        "mid": h[0],
                        "bid": float(st.bid_px[0]),
                        "ask": float(st.ask_px[0]),
                        "high": max(high, h[0]),
                        "low": min(low, h[0]),
                        "volume": vol,
                    }
                )
            t += step_s
        schema: dict[str, pl.DataType] = {
            "ts": pl.Datetime("us", "UTC"),
            "available_at": pl.Datetime("us", "UTC"),
            "mid": pl.Float64(),
            "bid": pl.Float64(),
            "ask": pl.Float64(),
            "high": pl.Float64(),
            "low": pl.Float64(),
            "volume": pl.Float64(),
        }
        return pl.DataFrame(rows, schema=schema)

    def build(
        self,
        inst: str,
        cutoff: datetime,
        *,
        eligible: Sequence[str] | None = None,
        peers: Sequence[str] | None = None,
        announcements: Sequence[AnnouncementMeta] = (),
        announcement_feed_available: bool = False,
        jev_evaluations: Sequence[JevEvaluation] = (),
    ) -> PointInTimeMarketState:
        cutoff = ensure_utc(cutoff)
        cs = cutoff.timestamp()
        s = self._series[inst]
        peer_names = [p for p in (peers if peers is not None else self.instruments) if p != inst]
        intrabar: Candle | None = None
        for c in s.intrabars:
            if c.available_at <= cutoff and c.open_ts.timestamp() <= cs < c.open_ts.timestamp() + 60:
                if intrabar is None or c.available_at >= intrabar.available_at:
                    intrabar = Candle(
                        c.open_ts,
                        c.available_at,
                        c.open,
                        c.high,
                        c.low,
                        c.close,
                        c.volume,
                        c.volume_quote,
                        False,
                    )
        funding = [f for f in s.funding if f.available_at <= cutoff][-FUNDING_KEEP:]
        oi_recs = [o for o in s.oi if o.available_at <= cutoff and o.ts.timestamp() > cs - OI_LOOKBACK_S]
        oi_recs.sort(key=lambda o: o.ts)
        oi = (
            OiWindow(
                np.array([o.ts.timestamp() for o in oi_recs]), np.array([o.oi_contracts for o in oi_recs])
            )
            if oi_recs
            else None
        )
        v = s.v if (s.v_available_at is not None and s.v_available_at <= cutoff) else 1.0
        return PointInTimeMarketState(
            instrument=inst,
            cutoff_at=cutoff,
            book=s.book_at(cs),
            book_history=s.history(cs),
            trades=s.trades(cs),
            closed_candles=s.closed_candles(cs),
            intrabar=intrabar,
            mark=_last_available(s.marks, cutoff),
            index=_last_available(s.indexes, cutoff),
            funding=funding,
            open_interest=oi,
            announcements=[a for a in announcements if a.available_at <= cutoff],
            announcement_feed_available=announcement_feed_available,
            jev_evaluations=[
                e
                for e in jev_evaluations
                if e.features_committed_at is not None and e.features_committed_at <= cutoff
            ],
            peers={p: self.candles_for(p, cutoff) for p in peer_names},
            eligible_instruments=list(eligible) if eligible is not None else self.instruments,
            base_units_per_contract=v,
        )


def computations_to_frame(rows: Sequence[FeatureComputation]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    names = rows[0].vector.names
    data: dict[str, list[object]] = {
        "instrument": [r.vector.instrument for r in rows],
        "cutoff_at": [r.vector.cutoff_at for r in rows],
        "available_at": [r.vector.available_at for r in rows],
        "schema_hash": [r.vector.schema_hash for r in rows],
    }
    for i, name in enumerate(names):
        data[name] = [r.vector.values[i] for r in rows]
        data[f"{name}__mask"] = [r.vector.masks[i].value for r in rows]
    schema: dict[str, pl.DataType] = {
        "instrument": pl.String(),
        "cutoff_at": pl.Datetime("us", "UTC"),
        "available_at": pl.Datetime("us", "UTC"),
        "schema_hash": pl.String(),
    }
    for name in names:
        schema[name] = pl.Float64()
        schema[f"{name}__mask"] = pl.String()
    return pl.DataFrame(data, schema=schema)


def compute_batch(
    engine: FeatureEngine,
    builder: BatchStateBuilder,
    cutoffs: Sequence[datetime],
    *,
    instruments: Sequence[str] | None = None,
    eligible_at: Mapping[datetime, Sequence[str]] | None = None,
    jev_by_instrument: Mapping[str, Sequence[JevEvaluation]] | None = None,
    announcements_by_instrument: Mapping[str, Sequence[AnnouncementMeta]] | None = None,
    announcement_feed_available: bool = False,
) -> tuple[pl.DataFrame, list[FeatureComputation]]:
    insts = list(instruments) if instruments is not None else builder.instruments
    out: list[FeatureComputation] = []
    for cutoff in cutoffs:
        eligible = list(eligible_at[cutoff]) if eligible_at is not None and cutoff in eligible_at else insts
        for inst in insts:
            state = builder.build(
                inst,
                cutoff,
                eligible=eligible,
                peers=insts,
                jev_evaluations=(jev_by_instrument or {}).get(inst, ()),
                announcements=(announcements_by_instrument or {}).get(inst, ()),
                announcement_feed_available=announcement_feed_available,
            )
            out.append(engine.compute(state))
    return computations_to_frame(out), out


def mask_summary(frame: pl.DataFrame) -> dict[str, dict[str, int]]:
    """Comptage des masques par feature (diagnostic de qualité, jamais une imputation)."""
    summary: dict[str, dict[str, int]] = {}
    for col in frame.columns:
        if col.endswith("__mask"):
            counts = frame[col].value_counts()
            summary[col[: -len("__mask")]] = {
                str(k): int(v) for k, v in zip(counts[col].to_list(), counts["count"].to_list(), strict=True)
            }
    for flag in QualityFlag:
        for d in summary.values():
            d.setdefault(flag.value, 0)
    return summary
