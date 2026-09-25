"""Live market data with the *same* fields and conventions as the research panel.

Signals are computed from Binance USDT-M public data (klines with aggressor volume, funding, premium index)
exactly as in research; orders are routed to OKX. Train/serve parity is the point: a feature that is
computed differently live than in the backtest is a different strategy.

Only **closed** bars are ever returned. The feed keeps a rolling in-memory history and fetches just the new
bars on each update.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
import numpy as np
import pandas as pd

from hermes.config import BAR_MINUTES
from hermes.data.intrabar import intrabar_aggregates
from hermes.data.panel import BAR_TO_OFFSET, Panel, clean_panel
from hermes.execution.okx.instruments import base_asset

log = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"
SPOT_API = "https://api.binance.com"
WEIGHT_BUDGET = 1800  # of Binance's 2400 per minute and IP: headroom for the other processes on the host
# Positioning snapshots (the live side of the archives' "metrics"): endpoint and value per panel field. Binance
# serves the last 30 days of 5-minute snapshots, the archives' own granularity.
POSITIONING = {
    "ls_top": ("/futures/data/topLongShortPositionRatio", "longShortRatio"),
    "ls_account": ("/futures/data/globalLongShortAccountRatio", "longShortRatio"),
    "oi_value": ("/futures/data/openInterestHist", "sumOpenInterestValue"),
}
POSITIONING_FIELDS = {"ls_top": "ls_top", "ls_account": "ls_account", "oi": "oi_value"}
POSITIONING_DAYS = 29.5  # within the 30 days served
SNAPSHOT_MS = 300_000
# The API stamps a snapshot 5 minutes after the archives' create_time for the same values (measured on BTC, ETH,
# SOL: 100 % of rows match at +5 min, 2-16 % at +0).
API_STAMP_LAG_MS = 300_000
POSITIONING_RPM = 150  # /futures/data allows 1000 requests per 5 minutes and IP


def kline_weight(limit: int) -> int:
    """Binance USD-M weight of a klines request."""
    return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10


@dataclass
class DailyHistory:
    """Closed daily bars from the live feed (today's unfinished bar is never included)."""

    quote_volume: pd.DataFrame
    alive: pd.DataFrame
    close: pd.DataFrame


def bar_ms(bar: str) -> int:
    return BAR_MINUTES[bar] * 60_000


class BinanceLiveFeed:
    def __init__(
        self,
        bar: str = "15m",
        history_bars: int = 2880,
        client: httpx.AsyncClient | None = None,
        concurrency: int = 6,
        intrabar_minutes: int = 0,
        positioning: tuple[str, ...] = (),
    ):
        """``intrabar_minutes`` > 0 also keeps that much 1-minute history to compute the intrabar fields;
        ``positioning`` names the families (``features.positioning``) whose snapshots are fetched."""
        self.bar = bar
        self.positioning = [POSITIONING_FIELDS[p] for p in positioning]
        self.pos: dict[str, pd.DataFrame] = {}
        self._pos_calls: list[float] = []
        self._pos_sem = asyncio.Semaphore(1)  # one /futures/data request at a time: the rate limit is shared
        self.history_bars = history_bars
        self.intrabar_minutes = intrabar_minutes if BAR_MINUTES[bar] > 1 else 0
        self.http = client or httpx.AsyncClient(base_url=FAPI, timeout=15.0)
        self.sem = asyncio.Semaphore(concurrency)
        self.frames: dict[str, pd.DataFrame] = {}
        self.m1: dict[str, pd.DataFrame] = {}
        self._daily: dict[str, pd.DataFrame] = {}
        self._daily_day = ""
        self._weight_minute = 0
        self._weight_used = 0
        self.last_update = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict[str, object], weight: int = 1) -> list:  # type: ignore[type-arg]
        """GET with Binance's IP weight budget respected: throttled answers are waited out, never mistaken
        for an empty result (which would silently truncate a history)."""
        async with self.sem:
            for attempt in range(6):
                await self._spend(weight)
                try:
                    r = await self.http.get(path, params=params)
                except httpx.HTTPError:
                    if attempt == 5:
                        raise
                    await asyncio.sleep(1 + attempt)
                    continue
                used = r.headers.get("x-mbx-used-weight-1m")
                if used is not None and used.isdigit():
                    self._weight_used = max(self._weight_used, int(used))
                if r.status_code in (418, 429):
                    retry = r.headers.get("retry-after")
                    wait = float(retry) if retry and retry.replace(".", "", 1).isdigit() else 5.0 * (attempt + 1)
                    log.warning("Binance throttled %s (%s): waiting %.0fs", path, r.status_code, wait)
                    await asyncio.sleep(min(wait, 120.0))
                    continue
                if r.status_code >= 500 and attempt < 5:
                    await asyncio.sleep(1 + attempt)
                    continue
                r.raise_for_status()
                return r.json()  # type: ignore[no-any-return]
        raise RuntimeError(f"Binance request {path} still throttled after retries")

    async def _spend(self, weight: int) -> None:
        """Client-side view of the 1-minute weight window; waits for the next window near the limit."""
        minute = int(time.time() // 60)
        if minute != self._weight_minute:
            self._weight_minute, self._weight_used = minute, 0
        if self._weight_used + weight > WEIGHT_BUDGET:
            await asyncio.sleep(60.5 - time.time() % 60)
            self._weight_minute, self._weight_used = int(time.time() // 60), 0
        self._weight_used += weight

    async def perp_listings(self) -> dict[str, int]:
        """USDT-margined perpetuals trading on Binance with their launch time (ms), from exchangeInfo."""
        info: dict = await self._get("/fapi/v1/exchangeInfo", {}, weight=1)  # type: ignore[assignment,type-arg]
        return {
            str(s["symbol"]): int(s["onboardDate"])
            for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("underlyingType", "COIN") == "COIN"  # not the stock, commodity or index perpetuals
            and s.get("onboardDate")
        }

    async def first_trade(self, symbol: str) -> pd.Timestamp | None:
        """Open of the contract's first 1-minute candle (its real start of trading)."""
        rows = await self._get("/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "startTime": 0, "limit": 1})
        return pd.Timestamp(int(rows[0][0]), unit="ms", tz="UTC") if rows else None

    async def spot_first_open(self, symbol: str) -> pd.Timestamp | None:
        """Open of the first daily candle of the token's oldest Binance spot market (USDT, FDUSD, USDC, BTC, BNB or
        TRY quote, under the base with and without a 1000/1M prefix), or None when Binance has none."""
        raw = symbol[:-4] if symbol.endswith("USDT") else symbol
        first: pd.Timestamp | None = None
        for base in dict.fromkeys((base_asset(symbol), raw)):  # 1000PEPE's spot market is PEPE's
            for quote in ("USDT", "FDUSD", "USDC", "BTC", "BNB", "TRY"):
                try:
                    rows = await self._get(
                        f"{SPOT_API}/api/v3/klines",
                        {"symbol": base + quote, "interval": "1d", "startTime": 0, "limit": 1},
                        weight=2,
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 400:  # no such market
                        continue
                    raise
                if rows:
                    t = pd.Timestamp(int(rows[0][0]), unit="ms", tz="UTC")
                    first = t if first is None or t < first else first
        return first

    async def top_symbols(self, n: int) -> list[str]:
        """Current most traded USDT perpetuals (24h quote volume)."""
        data = await self._get("/fapi/v1/ticker/24hr", {}, weight=40)
        rows = [(d["symbol"], float(d.get("quoteVolume") or 0)) for d in data if d["symbol"].endswith("USDT")]
        rows.sort(key=lambda x: -x[1])
        return [s for s, _ in rows[:n]]

    async def _klines(
        self, kind: str, symbol: str, start_ms: int | None, limit: int, interval: str | None = None
    ) -> pd.DataFrame:
        path = "/fapi/v1/klines" if kind == "klines" else "/fapi/v1/premiumIndexKlines"
        params: dict[str, object] = {"symbol": symbol, "interval": interval or self.bar, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        rows = await self._get(path, params, weight=kline_weight(limit))
        if not rows:
            return pd.DataFrame()
        a = np.array([[float(x) for x in r[:11]] for r in rows])
        now_ms = time.time() * 1000
        a = a[a[:, 6] < now_ms]  # closed bars only (close_time in the past)
        idx = pd.to_datetime(a[:, 0], unit="ms", utc=True)
        if kind == "klines":
            return pd.DataFrame(
                {
                    "open": a[:, 1],
                    "high": a[:, 2],
                    "low": a[:, 3],
                    "close": a[:, 4],
                    "volume": a[:, 5],
                    "quote_volume": a[:, 7],
                    "trades": a[:, 8],
                    "taker_buy_quote": a[:, 10],
                },
                index=idx,
            )
        return pd.DataFrame({"premium": a[:, 4]}, index=idx)

    async def _funding(self, symbol: str, start_ms: int) -> pd.Series:
        """Every settlement since ``start_ms``, in pages of 1000 (a contract settling hourly has 1600+ in the
        history window)."""
        rows: list = []  # type: ignore[type-arg]
        s = start_ms
        for _ in range(20):
            page = await self._get("/fapi/v1/fundingRate", {"symbol": symbol, "startTime": s, "limit": 1000})
            if not page:
                break
            rows += page
            last = max(int(r["fundingTime"]) for r in page)
            if len(page) < 1000 or last < s:
                break
            s = last + 1
        if not rows:
            return pd.Series(dtype=float)
        ts = pd.to_datetime([int(r["fundingTime"]) for r in rows], unit="ms", utc=True).round("1min")
        off = pd.Timedelta(BAR_TO_OFFSET[self.bar])
        idx = ts.ceil(off) - off
        return pd.Series([float(r["fundingRate"]) for r in rows], index=idx).groupby(level=0).sum()

    async def _history(self, kind: str, symbol: str, start: int, interval: str) -> pd.DataFrame:
        step = bar_ms(interval) if interval != "1d" else 86_400_000
        now_ms = int(time.time() * 1000)
        parts, s = [], start
        while s < now_ms - step:
            k = await self._klines(kind, symbol, s, 1500, interval)
            if k.empty:
                break
            parts.append(k)
            s = int(k.index[-1].value // 1_000_000) + step
            if len(k) < 1500:
                break
        return pd.concat(parts) if parts else pd.DataFrame()

    async def _refresh_m1(self, symbol: str) -> None:
        have = self.m1.get(symbol)
        now_ms = int(time.time() * 1000)
        if have is None or have.empty:
            k = await self._history("klines", symbol, now_ms - self.intrabar_minutes * 60_000, "1m")
        else:
            last = int(have.index[-1].value // 1_000_000)
            k = await self._history("klines", symbol, last - 120_000, "1m")
        if k.empty:
            return
        merged = k if have is None else pd.concat([have, k])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self.m1[symbol] = merged.iloc[-self.intrabar_minutes :]

    async def daily(self, symbols: list[str], days: int = 200) -> DailyHistory:
        """Months of closed daily bars (quote volume, traded flag, close) for the universe and the ES check.

        Fetched once per UTC day and symbol; a symbol whose fetch failed is retried on the next call instead
        of being cached as 'no history' (which would exclude it for the whole day).
        """
        today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        if self._daily_day != today:
            self._daily, self._daily_day = {}, today
        missing = [s for s in symbols if s not in self._daily]
        if missing:
            start = int(time.time() * 1000) - days * 86_400_000
            res = await asyncio.gather(
                *(self._history("klines", s, start, "1d") for s in missing), return_exceptions=True
            )
            for s, k in zip(missing, res):
                if isinstance(k, BaseException):
                    log.warning("daily history %s failed: %s", s, k)
                    continue
                self._daily[s] = k
        qv, alive, close = {}, {}, {}
        for s in symbols:
            k = self._daily.get(s)
            if k is None or k.empty:
                continue
            qv[s], alive[s], close[s] = k["quote_volume"], k["close"].notna(), k["close"]
        qdf = pd.DataFrame(qv).sort_index().reindex(columns=symbols)
        adf = pd.DataFrame(alive).sort_index().reindex(columns=symbols).fillna(False).astype(bool)
        cdf = pd.DataFrame(close).sort_index().reindex(columns=symbols)
        return DailyHistory(qdf, adf, cdf)

    async def _refresh_symbol(self, symbol: str) -> None:
        step = bar_ms(self.bar)
        have = self.frames.get(symbol)
        now_ms = int(time.time() * 1000)
        if have is None or have.empty:
            start = now_ms - self.history_bars * step
            parts = []
            s = start
            while s < now_ms - step:
                k = await self._klines("klines", symbol, s, 1500)
                if k.empty:
                    break
                parts.append(k)
                s = int(k.index[-1].value // 1_000_000) + step
                if len(k) < 1500:
                    break
            k = pd.concat(parts) if parts else pd.DataFrame()
            prem_parts = []
            s = start
            while s < now_ms - step and not k.empty:
                p = await self._klines("premium", symbol, s, 1500)
                if p.empty:
                    break
                prem_parts.append(p)
                s = int(p.index[-1].value // 1_000_000) + step
                if len(p) < 1500:
                    break
            prem = pd.concat(prem_parts) if prem_parts else pd.DataFrame(columns=["premium"])
        else:
            last = int(have.index[-1].value // 1_000_000)
            k = await self._klines("klines", symbol, last - 2 * step, 50)
            prem = await self._klines("premium", symbol, last - 2 * step, 50)
        if k.empty:
            return
        k["premium"] = prem["premium"].reindex(k.index) if not prem.empty else np.nan
        fund_start = int(k.index[0].value // 1_000_000)
        f = await self._funding(symbol, fund_start)
        k["funding_rate"] = f.reindex(k.index)
        merged = k if have is None else pd.concat([have, k])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        # Funding rows may arrive after the kline: refresh funding over the whole recent window.
        if have is not None and not f.empty:
            merged.loc[f.index.intersection(merged.index), "funding_rate"] = f.reindex(
                f.index.intersection(merged.index)
            )
        self.frames[symbol] = merged.iloc[-self.history_bars :]

    async def _pos_rate(self) -> None:
        """Client-side budget of the /futures/data endpoints (their own per-IP request limit)."""
        now = time.time()
        self._pos_calls = [t for t in self._pos_calls if now - t < 60.0]
        if len(self._pos_calls) >= POSITIONING_RPM:
            await asyncio.sleep(60.0 - (now - self._pos_calls[0]) + 0.05)
        self._pos_calls.append(time.time())

    async def _snapshots(self, field: str, symbol: str, start_ms: int) -> pd.Series:
        """5-minute positioning snapshots from ``start_ms``, indexed by the archives' create_time.

        Binance answers a range of more than 500 snapshots with the *latest* 500, so the history is read in
        bounded windows of 500 (startTime and endTime), advancing by the window whatever a page holds. The
        windows are contiguous: the start is off the 5-minute grid, and stepping a whole snapshot past a window's
        end skipped the one grid snapshot in between, a bar's closing value every ~41 h one time in six."""
        path, key = POSITIONING[field]
        now_ms = int(time.time() * 1000)
        values: dict[int, float] = {}
        s = max(start_ms, now_ms - int(POSITIONING_DAYS * 86_400_000))  # older than 30 days: refused
        while s <= now_ms:
            e = min(s + 499 * SNAPSHOT_MS, now_ms)
            async with self._pos_sem:
                await self._pos_rate()
                rows = await self._get(
                    path, {"symbol": symbol, "period": "5m", "limit": 500, "startTime": s, "endTime": e}
                )
            for r in rows or []:
                values[int(r["timestamp"]) - API_STAMP_LAG_MS] = float(r[key])
            s = e + 1
        if not values:
            return pd.Series(dtype=float)
        idx = pd.to_datetime(list(values), unit="ms", utc=True)
        return pd.Series(list(values.values()), index=idx).sort_index()

    def set_positioning(self, families: tuple[str, ...] | list[str]) -> None:
        """Follow a new champion's positioning families (hot reload)."""
        self.positioning = [POSITIONING_FIELDS[p] for p in families]
        self.pos = {}

    async def _refresh_positioning(self, symbol: str) -> None:
        now_ms = int(time.time() * 1000)
        have = self.pos.get(symbol)
        cols = {}
        for field in self.positioning:
            old = have[field].dropna() if have is not None and field in have else pd.Series(dtype=float)
            start = (
                int(old.index[-1].value // 1_000_000) - 2 * SNAPSHOT_MS
                if len(old)
                else now_ms - int(POSITIONING_DAYS * 86_400_000)
            )
            new = await self._snapshots(field, symbol, start)
            both = pd.concat([old, new])
            cols[field] = both[~both.index.duplicated(keep="last")].sort_index()
        df = pd.DataFrame(cols)
        self.pos[symbol] = df[df.index >= pd.Timestamp(now_ms - 30 * 86_400_000, unit="ms", tz="UTC")]

    def _positioning_bars(self, symbol: str) -> pd.DataFrame | None:
        """Snapshots on the bar grid exactly as the archives are: the last one in each bar's (open, close]."""
        df = self.pos.get(symbol)
        if df is None or df.empty:
            return None
        off = pd.Timedelta(BAR_TO_OFFSET[self.bar])
        out = df.copy()
        out.index = out.index.round("1min").ceil(off) - off
        return out.groupby(level=0).last()

    async def update(self, symbols: list[str]) -> Panel:
        results = await asyncio.gather(*(self._refresh_symbol(s) for s in symbols), return_exceptions=True)
        for s, r in zip(symbols, results):
            if isinstance(r, BaseException):
                log.warning("feed %s failed: %s", s, r)
        frames = {s: self.frames[s] for s in symbols if s in self.frames and len(self.frames[s])}
        if not frames:
            raise RuntimeError("live feed returned no data")
        if self.positioning:
            res = await asyncio.gather(*(self._refresh_positioning(s) for s in frames), return_exceptions=True)
            for s, r in zip(list(frames), res):
                if isinstance(r, BaseException):
                    log.warning("positioning feed %s failed: %s", s, r)
                bars = self._positioning_bars(s)
                frames[s] = frames[s].drop(columns=self.positioning, errors="ignore")
                for field in self.positioning:
                    frames[s][field] = bars[field].reindex(frames[s].index) if bars is not None else np.nan
        if self.intrabar_minutes:
            res = await asyncio.gather(*(self._refresh_m1(s) for s in frames), return_exceptions=True)
            for s, r in zip(list(frames), res):
                if isinstance(r, BaseException):
                    log.warning("1m feed %s failed: %s", s, r)
                    continue
                m1 = self.m1.get(s)
                if m1 is not None and len(m1):
                    agg = intrabar_aggregates(m1, self.bar)
                    frames[s] = frames[s].join(agg, how="left")
        self.last_update = time.time()
        panel = clean_panel(Panel.from_long(frames, self.bar))
        panel.meta["source"] = "binance_live"
        return panel

    @staticmethod
    def last_closed_bar(bar: str, now: pd.Timestamp | None = None) -> pd.Timestamp:
        now = now or pd.Timestamp.now(tz="UTC")
        off = pd.Timedelta(BAR_TO_OFFSET[bar])
        return now.floor(off) - off
