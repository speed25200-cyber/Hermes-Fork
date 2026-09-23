"""Downloader for Binance USDT-M futures public archives (https://data.binance.vision).

Why this source: it is free, complete back to 2019/2020, keeps **delisted** contracts (no survivorship bias),
and its klines carry the aggressor side (``taker_buy_quote_volume``), i.e. exact signed order flow without
tick data. Binance is also where most crypto price discovery happens, so its flow is the best signal
source even when orders are routed to OKX.

Everything is cached on disk: raw archives under ``<cache>/raw`` (immutable once a month is closed) and a
parsed parquet per symbol and dataset. A re-run only fetches what is new.
"""

from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import httpx
import numpy as np
import pandas as pd

from hermes.data.panel import BAR_TO_OFFSET, Panel

log = logging.getLogger(__name__)

LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDN_URL = "https://data.binance.vision"
# The S3 origin first (reachable from more networks), the CDN as fallback; HERMES_ARCHIVE_MIRROR=cdn reverses
# the order (faster from North America, e.g. GitHub runners).
MIRRORS = (CDN_URL, LIST_URL) if os.environ.get("HERMES_ARCHIVE_MIRROR") == "cdn" else (LIST_URL, CDN_URL)
KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]


def _to_utc_ms(values: pd.Series) -> pd.DatetimeIndex:
    v = pd.to_numeric(values, errors="coerce").astype("float64")
    # Binance moved some archives to microseconds in 2025; accept both.
    v = np.where(v > 1e14, v / 1000.0, v)
    return pd.to_datetime(v, unit="ms", utc=True)


def _read_csv_from_zip(blob: bytes, columns: list[str] | None = None) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        raw = zf.read(name)
    first = raw.split(b"\n", 1)[0].decode("utf-8", "replace")
    has_header = not first.split(",")[0].strip().lstrip("-").replace(".", "").isdigit()
    if columns is not None:
        df = pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)
        if not has_header or len(df.columns) == len(columns):
            df.columns = columns[: len(df.columns)]
        return df
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)


def _months(start: date, end: date) -> list[date]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        out.append(cur)
        cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return out


def _bar_floor_after(ts: pd.DatetimeIndex, bar: str) -> pd.DatetimeIndex:
    """Map an instant T to the open time of the bar whose (open, close] contains T."""
    offset = pd.Timedelta(BAR_TO_OFFSET[bar])
    t = ts.round("1min")
    return t.ceil(offset) - offset


@dataclass
class Periods:
    months: list[date]
    days: list[date]


def split_periods(start: date, end: date, today: date | None = None) -> Periods:
    """Closed months use monthly archives; the current month uses daily archives up to yesterday."""
    today = today or datetime.now(UTC).date()
    last_full_month = date(today.year, today.month, 1) - timedelta(days=1)
    months = [m for m in _months(start, min(end, last_full_month))]
    first_day = max(start, date(today.year, today.month, 1))
    days = []
    d = first_day
    while d <= min(end, today - timedelta(days=1)):
        days.append(d)
        d += timedelta(days=1)
    return Periods(months, days)


class BinanceArchive:
    def __init__(self, cache_dir: str | Path = "data", workers: int = 16, client: httpx.Client | None = None):
        self.cache = Path(cache_dir)
        self.raw = self.cache / "raw"
        self.parsed = self.cache / "parsed"
        self.workers = workers
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            limits=httpx.Limits(max_connections=workers * 2, max_keepalive_connections=workers),
            follow_redirects=True,
            headers={"User-Agent": "hermes-research/1.0"},
        )
        self._listing_cache: dict[str, set[str] | None] = {}

    # -- low level -----------------------------------------------------------------------------------------
    def _fetch(self, path: str, immutable: bool, persist: bool = True) -> bytes | None:
        """Fetch ``<mirror>/path`` with on-disk caching. Returns ``None`` if the archive does not exist."""
        local = self.raw / path
        missing = local.with_suffix(local.suffix + ".missing")
        if local.exists():
            return local.read_bytes()
        if immutable and missing.exists():
            return None
        url = ""
        for attempt in range(6):
            # Two tries on the S3 origin, then alternate with the CDN.
            url = f"{MIRRORS[0 if attempt < 2 else attempt % len(MIRRORS)]}/{path}"
            try:
                r = self.client.get(url)
                # S3 answers 403 for a missing key of a public bucket, the CDN 404.
                if r.status_code == 404 or (r.status_code == 403 and url.startswith(LIST_URL)):
                    if immutable:
                        missing.parent.mkdir(parents=True, exist_ok=True)
                        missing.touch()
                    return None
                r.raise_for_status()
                if persist:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    tmp = local.with_suffix(".part")
                    tmp.write_bytes(r.content)
                    tmp.replace(local)
                return r.content
            except (httpx.HTTPError, OSError) as exc:  # pragma: no cover - network dependent
                wait = min(2.0**attempt, 20.0)
                log.warning("fetch %s failed (%s), retry in %.0fs", path, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"could not download {url}")

    def _list(self, prefix: str, delimiter: bool = True) -> tuple[list[str], list[str]]:
        prefixes: list[str] = []
        keys: list[str] = []
        marker = ""
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        while True:
            params = {"prefix": prefix}
            if delimiter:
                params["delimiter"] = "/"
            if marker:
                params["marker"] = marker
            r = self.client.get(LIST_URL, params=params)
            r.raise_for_status()
            root = ElementTree.fromstring(r.content)
            prefixes += [e.text or "" for e in root.iter(f"{ns}Prefix") if e.text and e.text != prefix]
            keys += [e.text or "" for e in root.iter(f"{ns}Key")]
            truncated = (root.findtext(f"{ns}IsTruncated") or "false").lower() == "true"
            if not truncated:
                break
            marker = root.findtext(f"{ns}NextMarker") or (keys[-1] if keys else prefixes[-1])
        return prefixes, keys

    def monthly_available(self, prefix: str) -> set[str] | None:
        """``YYYY-MM`` stamps of the monthly archives under ``prefix`` (``None`` if listing fails).

        Listing first avoids thousands of requests for months before a listing or after a delisting.
        """
        if prefix in self._listing_cache:
            return self._listing_cache[prefix]
        try:
            _, keys = self._list(prefix, delimiter=False)
        except httpx.HTTPError:  # pragma: no cover - network dependent
            return None
        stamps = {k.rsplit("-", 2)[-2] + "-" + k.rsplit("-", 1)[-1][:2] for k in keys if k.endswith(".zip")}
        self._listing_cache[prefix] = stamps
        return stamps

    def list_symbols(self, quote: str = "USDT") -> list[str]:
        """Every USDT-M perpetual that ever had monthly klines, delisted ones included."""
        prefixes, _ = self._list("data/futures/um/monthly/klines/")
        syms = [p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes]
        return sorted(s for s in syms if s.endswith(quote) and "_" not in s)

    def _gather(self, paths: list[tuple[str, bool]]) -> list[bytes | None]:
        with ThreadPoolExecutor(self.workers) as ex:
            return list(ex.map(lambda p: self._fetch(*p), paths))

    # -- datasets --------------------------------------------------------------------------------------------
    def _kline_like(self, kind: str, symbol: str, bar: str, start: date, end: date) -> pd.DataFrame:
        periods = split_periods(start, end)
        prefix = f"data/futures/um/monthly/{kind}/{symbol}/{bar}/"
        have = self.monthly_available(prefix)
        paths = [
            (f"{prefix}{symbol}-{bar}-{m:%Y-%m}.zip", True)
            for m in periods.months
            if have is None or f"{m:%Y-%m}" in have
        ] + [
            (f"data/futures/um/daily/{kind}/{symbol}/{bar}/{symbol}-{bar}-{d:%Y-%m-%d}.zip", False)
            for d in periods.days
        ]
        frames = [_read_csv_from_zip(b, KLINE_COLUMNS) for b in self._gather(paths) if b]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        df.index = _to_utc_ms(df["open_time"])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df.apply(pd.to_numeric, errors="coerce")

    def klines(self, symbol: str, bar: str, start: date, end: date) -> pd.DataFrame:
        df = self._kline_like("klines", symbol, bar, start, end)
        if df.empty:
            return df
        out = pd.DataFrame(
            {
                "open": df["open"],
                "high": df["high"],
                "low": df["low"],
                "close": df["close"],
                "volume": df["volume"],
                "quote_volume": df["quote_volume"],
                "trades": df["count"],
                "taker_buy_quote": df["taker_buy_quote_volume"],
            }
        )
        return out.astype("float64")

    def premium(self, symbol: str, bar: str, start: date, end: date) -> pd.Series:
        df = self._kline_like("premiumIndexKlines", symbol, bar, start, end)
        return df["close"].astype("float64").rename("premium") if not df.empty else pd.Series(dtype="float64")

    def funding(self, symbol: str, bar: str, start: date, end: date) -> pd.Series:
        periods = split_periods(start, end)
        prefix = f"data/futures/um/monthly/fundingRate/{symbol}/"
        have = self.monthly_available(prefix)
        paths = [
            (f"{prefix}{symbol}-fundingRate-{m:%Y-%m}.zip", True)
            for m in periods.months
            if have is None or f"{m:%Y-%m}" in have
        ]
        frames = [_read_csv_from_zip(b) for b in self._gather(paths) if b]
        if not frames:
            return pd.Series(dtype="float64")
        df = pd.concat(frames, ignore_index=True)
        df.columns = [str(c).strip().lower() for c in df.columns]
        tcol = "calc_time" if "calc_time" in df.columns else df.columns[0]
        rcol = "last_funding_rate" if "last_funding_rate" in df.columns else df.columns[-1]
        ts = _to_utc_ms(df[tcol])
        s = pd.Series(pd.to_numeric(df[rcol], errors="coerce").to_numpy(), index=_bar_floor_after(ts, bar))
        return s.groupby(level=0).sum().rename("funding_rate")

    def metrics(self, symbol: str, bar: str, start: date, end: date) -> pd.DataFrame:
        days = []
        d = start
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        while d <= min(end, yesterday):
            days.append(d)
            d += timedelta(days=1)
        paths = [(f"data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{d:%Y-%m-%d}.zip", True) for d in days]
        frames = [_read_csv_from_zip(b) for b in self._gather(paths) if b]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        ts = pd.to_datetime(df["create_time"], utc=True)
        out = pd.DataFrame(
            {
                "oi_value": pd.to_numeric(df["sum_open_interest_value"], errors="coerce"),
                "ls_top": pd.to_numeric(df["sum_toptrader_long_short_ratio"], errors="coerce"),
                "ls_account": pd.to_numeric(df["count_long_short_ratio"], errors="coerce"),
                "taker_ls_ratio": pd.to_numeric(df["sum_taker_long_short_vol_ratio"], errors="coerce"),
            }
        )
        out.index = _bar_floor_after(pd.DatetimeIndex(ts), bar)
        return out.groupby(level=0).last()

    def intrabar(self, symbol: str, bar: str, start: date, end: date) -> pd.DataFrame:
        """1-minute microstructure aggregates per base bar (see ``hermes.data.intrabar``).

        The raw 1-minute archives are large (~1.5 MB per contract-month); they are streamed, aggregated and
        discarded, and only the small per-month aggregate is cached.
        """
        from hermes.data.intrabar import intrabar_aggregates

        out_dir = self.parsed / "intrabar" / bar / symbol
        out_dir.mkdir(parents=True, exist_ok=True)
        periods = split_periods(start, end)
        prefix = f"data/futures/um/monthly/klines/{symbol}/1m/"
        have = self.monthly_available(prefix)
        jobs: list[tuple[str, str, bool]] = [
            (f"{m:%Y-%m}", f"{prefix}{symbol}-1m-{m:%Y-%m}.zip", True)
            for m in periods.months
            if have is None or f"{m:%Y-%m}" in have
        ]
        jobs += [
            (f"{d:%Y-%m-%d}", f"data/futures/um/daily/klines/{symbol}/1m/{symbol}-1m-{d:%Y-%m-%d}.zip", True)
            for d in periods.days
        ]

        def one(job: tuple[str, str, bool]) -> pd.DataFrame | None:
            stamp, path, immutable = job
            cached = out_dir / f"{stamp}.parquet"
            if cached.exists():
                return pd.read_parquet(cached)
            blob = self._fetch(path, immutable, persist=False)
            if blob is None:
                return None
            raw = _read_csv_from_zip(blob, KLINE_COLUMNS)
            raw.index = _to_utc_ms(raw["open_time"])
            m1 = pd.DataFrame(
                {
                    "close": pd.to_numeric(raw["close"], errors="coerce"),
                    "volume": pd.to_numeric(raw["volume"], errors="coerce"),
                    "quote_volume": pd.to_numeric(raw["quote_volume"], errors="coerce"),
                    "taker_buy_quote": pd.to_numeric(raw["taker_buy_quote_volume"], errors="coerce"),
                },
                index=raw.index,
            )
            agg = intrabar_aggregates(m1, bar)
            agg.to_parquet(cached)
            return agg

        with ThreadPoolExecutor(max(2, self.workers // 4)) as ex:
            parts = [p for p in ex.map(one, jobs) if p is not None and len(p)]
        if not parts:
            return pd.DataFrame()
        out = pd.concat(parts).sort_index()
        return out[~out.index.duplicated(keep="last")]

    # -- panel -----------------------------------------------------------------------------------------------
    def symbol_frame(
        self, symbol: str, bar: str, start: date, end: date, include_premium: bool, include_metrics: bool
    ) -> pd.DataFrame:
        cache = self.parsed / bar / f"{symbol}.parquet"
        k = self.klines(symbol, bar, start, end)
        if k.empty:
            return k
        k["funding_rate"] = self.funding(symbol, bar, start, end).reindex(k.index)
        if include_premium:
            k["premium"] = self.premium(symbol, bar, start, end).reindex(k.index)
        if include_metrics:  # daily files: only over the contract's trading life (no storm of missing days)
            m = self.metrics(symbol, bar, max(start, k.index[0].date()), min(end, k.index[-1].date()))
            for c in m.columns:
                k[c] = m[c].reindex(k.index)
        cache.parent.mkdir(parents=True, exist_ok=True)
        k.to_parquet(cache)
        return k

    def build_panel(
        self,
        symbols: Iterable[str],
        bar: str,
        start: date,
        end: date,
        include_premium: bool = True,
        include_metrics: bool = False,
    ) -> Panel:
        frames = {}
        lo = pd.Timestamp(start, tz="UTC")
        for i, sym in enumerate(symbols):
            f = self.symbol_frame(sym, bar, start, end, include_premium, include_metrics)
            if len(f):
                # float32 per contract right away: a 15-minute panel of hundreds of contracts must fit in RAM.
                frames[sym] = f.loc[lo:].astype("float32")
            if i % 25 == 0:
                log.info("archive %s (%d) rows=%d", sym, i, len(f))
        panel = Panel.from_long(frames, bar)
        panel.meta["source"] = "binance_archive"
        return panel
