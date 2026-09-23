"""Dataset preparation: discover the universe without hindsight, download, cache the panel."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hermes.config import BAR_MINUTES, DataConfig
from hermes.data.binance_archive import BinanceArchive
from hermes.data.panel import INTRABAR_FIELDS, Panel, clean_panel, resample_panel
from hermes.data.synthetic import make_synthetic_panel
from hermes.data.universe import candidates_from_daily, daily_activity, is_excluded
from hermes.data.venue import OkxListing, activity_windows

log = logging.getLogger(__name__)


def _dates(cfg: DataConfig) -> tuple[date, date]:
    start = date.fromisoformat(cfg.start)
    end = date.fromisoformat(cfg.end) if cfg.end else datetime.now(UTC).date()
    return start, end


def daily_volume_panel(archive: BinanceArchive, symbols: list[str], start: date, end: date) -> pd.DataFrame:
    cols = {}
    for i, sym in enumerate(symbols):
        k = archive.klines(sym, "1d", start, end)
        if len(k):
            cols[sym] = k["quote_volume"]
        if i % 50 == 0:
            log.info("daily volumes %d/%d", i, len(symbols))
    df = pd.DataFrame(cols).sort_index()
    return df.asfreq("1D")


def discover_candidates(cfg: DataConfig, archive: BinanceArchive) -> list[str]:
    """Every contract that was in the point-in-time top-N at some date (cached)."""
    start, end = _dates(cfg)
    u = cfg.universe
    key = hashlib.sha1(
        json.dumps([cfg.start, str(end), u.model_dump(mode="json")], sort_keys=True).encode()
    ).hexdigest()[:10]
    cache = Path(cfg.cache_dir) / f"candidates_{key}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    symbols = list(u.symbols) or [s for s in archive.list_symbols(u.quote) if not is_excluded(s, u)]
    daily = daily_volume_panel(archive, symbols, start, end)
    if u.venue == "okx":
        # Rank among the contracts OKX listed at the time. The venue's top-N lies within Binance's top-2N as
        # long as fewer than N unlisted contracts outrank it (checked below); only those are probed.
        wide = candidates_from_daily(daily, u, top_n=2 * u.top_n)
        listed = OkxListing(cfg.cache_dir).calendar(activity_windows(daily[wide]))
        cands = candidates_from_daily(daily[wide], u, listed=listed)
        on = listed.reindex(index=daily.index, columns=wide).fillna(False).astype(bool)
        n_listed = (daily[wide].notna() & on).sum(axis=1).iloc[90:]  # after the first liquidity window
        if (short := int((n_listed < u.top_n).sum())) > 0:
            log.warning("OKX universe: %d days with < %d listed names in Binance's top-%d", short, u.top_n, 2 * u.top_n)
    else:
        cands = candidates_from_daily(daily, u)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(cands))
    daily.to_parquet(Path(cfg.cache_dir) / f"daily_volume_{key}.parquet")
    return cands


def load_panel(cfg: DataConfig, seed: int = 0) -> Panel:
    if cfg.source == "synthetic":
        return make_synthetic_panel(bar=cfg.bar, seed=seed)
    start, end = _dates(cfg)
    archive = BinanceArchive(cfg.cache_dir, workers=cfg.download_workers)
    symbols = list(cfg.universe.symbols) or discover_candidates(cfg, archive)
    src_bar = cfg.source_bar or cfg.bar
    key = hashlib.sha1(
        json.dumps(
            [
                symbols,
                cfg.bar,
                src_bar,
                cfg.start,
                str(end),
                cfg.include_metrics,
                cfg.include_premium,
                cfg.intrabar,
                cfg.intrabar_start,
                cfg.universe.venue,
                "v3",  # fields vwap_first (bounded by the bar's range) and venue_listed
            ]
        ).encode()
    ).hexdigest()[:10]
    directory = Path(cfg.cache_dir) / "panels" / f"{cfg.bar}_{key}"
    if (directory / "_panel.json").exists():
        return trim_to_funding(clean_panel(Panel.load(directory)))
    panel = archive.build_panel(symbols, src_bar, start, end, cfg.include_premium, cfg.include_metrics)
    panel = with_execution_fields(panel, cfg)
    if src_bar != cfg.bar:
        panel = resample_panel(panel, cfg.bar)
    if cfg.intrabar and BAR_MINUTES[cfg.bar] > 1:
        ib_start = date.fromisoformat(cfg.intrabar_start) if cfg.intrabar_start else start
        fields: dict[str, dict[str, pd.Series]] = {c: {} for c in INTRABAR_FIELDS}
        for i, sym in enumerate(panel.symbols):
            agg = archive.intrabar(sym, cfg.bar, ib_start, end)
            for c in INTRABAR_FIELDS:
                if c in agg:
                    fields[c][sym] = agg[c]
            if i % 25 == 0:
                log.info("intrabar %d/%d", i, len(panel.symbols))
        extra = {
            c: pd.DataFrame(v).reindex(index=panel.index, columns=panel.symbols).astype("float32")
            for c, v in fields.items()
        }
        panel = panel.with_fields(extra)
    panel.save(directory)
    return trim_to_funding(clean_panel(panel))


def with_execution_fields(panel: Panel, cfg: DataConfig) -> Panel:
    """Fields the backtest needs to trade like the live engine, added at the source bar.

    * ``vwap_first``: the bar's volume-weighted average price; after resampling, that of its **first**
      source bar. A decision taken at close(t) is filled at ``vwap_first[t+1]`` -- the live broker trades in
      the minutes after the close, not at the last print the signal was computed from (on 10 October 2025
      that print was mid-crash and prices rebounded within minutes).
    * ``venue_listed``: 1 on the days OKX listed the contract (``UniverseConfig.venue == "okx"``).
    """
    fields = {}
    vol = panel["volume"].astype("float64")
    vwap = panel["quote_volume"] / vol.where(vol > 0)
    fields["vwap_first"] = vwap.where((vwap >= panel["low"]) & (vwap <= panel["high"])).astype("float32")
    if cfg.universe.venue == "okx":
        qv, _ = daily_activity(panel)
        listed = OkxListing(cfg.cache_dir).calendar(activity_windows(qv))
        days = panel.index.floor("D")
        cal = listed.reindex(index=days, columns=panel.symbols).fillna(False).to_numpy(dtype="float32")
        fields["venue_listed"] = pd.DataFrame(cal, index=panel.index, columns=panel.symbols)
    return panel.with_fields(fields)


def trim_to_funding(panel: Panel) -> Panel:
    """Cut the panel where the funding history ends.

    Funding archives are monthly only: the current month has klines (daily archives) but no funding yet.
    Keeping those weeks would give the model a zero carry and label returns gross of funding there -- a
    different strategy from the one that trades live. They are dropped instead: the panel ends with the bar of
    the last known settlement.
    """
    if "funding_rate" not in panel:
        return panel
    has = panel["funding_rate"].notna().any(axis=1).to_numpy()
    if not has.any():
        return panel
    # The next settlement after the last known one (e.g. 00:00 on the 1st, in next month's archive) belongs
    # to a bar that would otherwise look settlement-free: the panel ends with the last settled bar.
    last = panel.index[np.nonzero(has)[0][-1]]
    if panel.index[-1] <= last:
        return panel
    keep = int(panel.index.searchsorted(last, side="right"))
    log.info("panel trimmed to the funding history: %s -> %s", panel.index[-1], panel.index[keep - 1])
    out = panel.iloc(slice(0, keep))
    out.meta["trimmed_to_funding"] = str(panel.index[keep - 1])
    return out
