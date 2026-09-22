"""Dataset preparation: discover the universe without hindsight, download, cache the panel."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from hermes.config import DataConfig
from hermes.data.binance_archive import BinanceArchive
from hermes.data.panel import Panel, clean_panel
from hermes.data.synthetic import make_synthetic_panel
from hermes.data.universe import candidates_from_daily, is_excluded

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
    key = hashlib.sha1(
        json.dumps([symbols, cfg.bar, cfg.start, str(end), cfg.include_metrics, cfg.include_premium]).encode()
    ).hexdigest()[:10]
    directory = Path(cfg.cache_dir) / "panels" / f"{cfg.bar}_{key}"
    if (directory / "_panel.json").exists():
        return clean_panel(Panel.load(directory))
    panel = archive.build_panel(symbols, cfg.bar, start, end, cfg.include_premium, cfg.include_metrics)
    panel.save(directory)
    return clean_panel(panel)
