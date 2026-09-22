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

import httpx
import numpy as np
import pandas as pd

from hermes.data.panel import BAR_TO_OFFSET, Panel, clean_panel

log = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"
MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000}


class BinanceLiveFeed:
    def __init__(
        self, bar: str = "1h", history_bars: int = 2400, client: httpx.AsyncClient | None = None, concurrency: int = 6
    ):
        self.bar = bar
        self.history_bars = history_bars
        self.http = client or httpx.AsyncClient(base_url=FAPI, timeout=15.0)
        self.sem = asyncio.Semaphore(concurrency)
        self.frames: dict[str, pd.DataFrame] = {}
        self.last_update = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict[str, object]) -> list:  # type: ignore[type-arg]
        async with self.sem:
            for attempt in range(4):
                try:
                    r = await self.http.get(path, params=params)
                    if r.status_code == 429 or r.status_code == 418:
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    return r.json()  # type: ignore[no-any-return]
                except httpx.HTTPError:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(1 + attempt)
        return []

    async def top_symbols(self, n: int) -> list[str]:
        """Current most traded USDT perpetuals (24h quote volume)."""
        data = await self._get("/fapi/v1/ticker/24hr", {})
        rows = [(d["symbol"], float(d.get("quoteVolume") or 0)) for d in data if d["symbol"].endswith("USDT")]
        rows.sort(key=lambda x: -x[1])
        return [s for s, _ in rows[:n]]

    async def _klines(self, kind: str, symbol: str, start_ms: int | None, limit: int) -> pd.DataFrame:
        path = "/fapi/v1/klines" if kind == "klines" else "/fapi/v1/premiumIndexKlines"
        params: dict[str, object] = {"symbol": symbol, "interval": self.bar, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        rows = await self._get(path, params)
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
        rows = await self._get("/fapi/v1/fundingRate", {"symbol": symbol, "startTime": start_ms, "limit": 1000})
        if not rows:
            return pd.Series(dtype=float)
        ts = pd.to_datetime([int(r["fundingTime"]) for r in rows], unit="ms", utc=True).round("1min")
        off = pd.Timedelta(BAR_TO_OFFSET[self.bar])
        idx = ts.ceil(off) - off
        return pd.Series([float(r["fundingRate"]) for r in rows], index=idx).groupby(level=0).sum()

    async def _refresh_symbol(self, symbol: str) -> None:
        step = MS[self.bar]
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

    async def update(self, symbols: list[str]) -> Panel:
        results = await asyncio.gather(*(self._refresh_symbol(s) for s in symbols), return_exceptions=True)
        for s, r in zip(symbols, results):
            if isinstance(r, BaseException):
                log.warning("feed %s failed: %s", s, r)
        frames = {s: self.frames[s] for s in symbols if s in self.frames and len(self.frames[s])}
        if not frames:
            raise RuntimeError("live feed returned no data")
        self.last_update = time.time()
        panel = clean_panel(Panel.from_long(frames, self.bar))
        panel.meta["source"] = "binance_live"
        return panel

    @staticmethod
    def last_closed_bar(bar: str, now: pd.Timestamp | None = None) -> pd.Timestamp:
        now = now or pd.Timestamp.now(tz="UTC")
        off = pd.Timedelta(BAR_TO_OFFSET[bar])
        return now.floor(off) - off
