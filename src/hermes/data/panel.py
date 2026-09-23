"""The market panel: one wide ``DataFrame`` (time x symbol) per field.

Conventions, relied upon by every other module:

* The index is a UTC ``DatetimeIndex`` of bar **open** times, regular (no missing bars; absent data is NaN).
* A bar's values are known at its **close** (``open + bar``). A feature computed on row ``t`` therefore uses
  information available at ``close(t)`` and nothing later. Decisions taken at ``close(t)`` are executed during
  bar ``t+1``.
* ``funding_rate[t]`` is the sum of funding rates *settled* at instants in ``(open(t), close(t)]``; a position
  held during bar ``t`` pays ``weight * funding_rate[t]`` (longs pay positive funding).
* ``premium[t]`` is the close of the premium index (perp vs. index basis) over bar ``t``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

PRICE_FIELDS = ("open", "high", "low", "close")
CORE_FIELDS = (*PRICE_FIELDS, "volume", "quote_volume", "trades", "taker_buy_quote")
OPTIONAL_FIELDS = ("funding_rate", "premium", "oi_value", "ls_top", "ls_account", "taker_ls_ratio")
INTRABAR_FIELDS = ("ib_rv", "ib_bv", "ib_rskew", "ib_flow_last", "ib_flow_std", "ib_vwap", "ib_upfrac")

BAR_TO_OFFSET = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1D",
}


@dataclass
class Panel:
    fields: dict[str, pd.DataFrame]
    bar: str = "1h"
    meta: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = [f for f in CORE_FIELDS if f not in self.fields]
        if missing:
            raise ValueError(f"panel is missing core fields: {missing}")
        ref = self.fields["close"]
        if not isinstance(ref.index, pd.DatetimeIndex) or ref.index.tz is None:
            raise ValueError("panel index must be a tz-aware DatetimeIndex (UTC)")
        if not ref.index.is_monotonic_increasing or ref.index.has_duplicates:
            raise ValueError("panel index must be strictly increasing")
        for name, df in self.fields.items():
            if not df.index.equals(ref.index) or not df.columns.equals(ref.columns):
                self.fields[name] = df.reindex(index=ref.index, columns=ref.columns)

    # -- accessors -------------------------------------------------------------------------------------
    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.fields[name]

    def __contains__(self, name: object) -> bool:
        return name in self.fields

    def get(self, name: str) -> pd.DataFrame | None:
        return self.fields.get(name)

    def __iter__(self) -> Iterator[str]:
        return iter(self.fields)

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.fields["close"].index  # type: ignore[return-value]

    @property
    def symbols(self) -> list[str]:
        return list(self.fields["close"].columns)

    @property
    def bar_delta(self) -> pd.Timedelta:
        return pd.Timedelta(BAR_TO_OFFSET[self.bar])

    @property
    def shape(self) -> tuple[int, int]:
        return self.fields["close"].shape

    # -- transformations ---------------------------------------------------------------------------------
    def _map(self, fn) -> Panel:  # type: ignore[no-untyped-def]
        return Panel({k: fn(v) for k, v in self.fields.items()}, bar=self.bar, meta=dict(self.meta))

    def loc(self, start: object = None, end: object = None) -> Panel:
        return self._map(lambda df: df.loc[start:end])

    def iloc(self, sl: slice) -> Panel:
        return self._map(lambda df: df.iloc[sl])

    def tail(self, n: int) -> Panel:
        return self._map(lambda df: df.iloc[-n:])

    def subset(self, symbols: Iterable[str]) -> Panel:
        cols = [s for s in symbols if s in self.fields["close"].columns]
        return self._map(lambda df: df[cols])

    def with_fields(self, extra: Mapping[str, pd.DataFrame]) -> Panel:
        merged = dict(self.fields)
        merged.update(extra)
        return Panel(merged, bar=self.bar, meta=dict(self.meta))

    def returns(self) -> pd.DataFrame:
        """Simple close-to-close returns; row t is the return earned over bar t."""
        close = self.fields["close"]
        return close / close.shift(1) - 1.0

    # -- persistence -------------------------------------------------------------------------------------
    def save(self, directory: str | Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name, df in self.fields.items():
            df.astype("float32").to_parquet(directory / f"{name}.parquet")
        (directory / "_panel.json").write_text(json.dumps({"bar": self.bar}))

    @classmethod
    def load(cls, directory: str | Path) -> Panel:
        directory = Path(directory)
        # Plain JSON: pandas.read_json would parse "1h" as a timestamp.
        bar = json.loads((directory / "_panel.json").read_text())["bar"]
        fields = {p.stem: pd.read_parquet(p) for p in sorted(directory.glob("*.parquet"))}
        for df in fields.values():
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
        return cls(fields, bar=str(bar))

    # -- construction ------------------------------------------------------------------------------------
    @classmethod
    def from_long(cls, frames: Mapping[str, pd.DataFrame], bar: str) -> Panel:
        """Build a panel from per-symbol frames indexed by bar open time with field columns."""
        offset = BAR_TO_OFFSET[bar]
        starts = [f.index.min() for f in frames.values() if len(f)]
        ends = [f.index.max() for f in frames.values() if len(f)]
        if not starts:
            raise ValueError("no data")
        index = pd.date_range(min(starts), max(ends), freq=offset, tz="UTC")
        symbols = sorted(frames)
        all_fields = sorted({c for f in frames.values() for c in f.columns})
        out: dict[str, pd.DataFrame] = {}
        for name in all_fields:
            cols = {}
            for sym in symbols:
                f = frames[sym]
                if name in f.columns:
                    s = f[name]
                    s = s[~s.index.duplicated(keep="last")]
                    cols[sym] = s.reindex(index)
                else:
                    cols[sym] = pd.Series(np.nan, index=index)
            out[name] = pd.DataFrame(cols, index=index, dtype="float32")
        for name in CORE_FIELDS:
            if name not in out:
                out[name] = pd.DataFrame(np.nan, index=index, columns=symbols)
        return cls(out, bar=bar)


def resample_panel(panel: Panel, bar: str) -> Panel:
    """Aggregate a panel to a coarser bar with the correct rule per field."""
    offset = BAR_TO_OFFSET[bar]
    rules = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "quote_volume": "sum",
        "trades": "sum",
        "taker_buy_quote": "sum",
        "funding_rate": "sum",
        "premium": "last",
        "oi_value": "last",
        "ls_top": "last",
        "ls_account": "last",
        "taker_ls_ratio": "last",
    }
    out = {}
    for name, df in panel.fields.items():
        if name in INTRABAR_FIELDS:
            continue  # not additive: recomputed from 1-minute data for the new bar (hermes.data.intrabar)
        how = rules.get(name, "last")
        r = df.resample(offset, label="left", closed="left")
        agg = getattr(r, how)(min_count=1) if how in ("sum",) else getattr(r, how)()
        out[name] = agg
    return Panel(out, bar=bar, meta=dict(panel.meta))


def clean_panel(panel: Panel, max_gap: int | None = None) -> Panel:
    """Fill short gaps (exchange maintenance, missing archive rows, a failed live request) with a flat bar.

    A missing bar inside a contract's life would otherwise look like a delisting and force an exit and a
    costly re-entry one bar later. The rule is **causal**, identical in research and live: after a listed
    contract's last bar, its price is carried forward for at most ``max_gap`` bars (default: 45 minutes, at
    least 3 bars), activity fields set to zero. Whether the series later resumes plays no role -- that would
    be information from the future. Before listing nothing is filled.
    """
    if max_gap is None:
        max_gap = max(3, round(45 * 60 / pd.Timedelta(BAR_TO_OFFSET[panel.bar]).total_seconds()))
    close = panel["close"]
    filled_close = close.ffill(limit=max_gap)
    fill = close.isna() & filled_close.notna()
    if not fill.to_numpy().any():
        return panel
    fields = dict(panel.fields)
    fields["close"] = close.where(~fill, filled_close)
    for name in ("open", "high", "low"):
        fields[name] = panel[name].where(~fill, fields["close"])
    for name in ("volume", "quote_volume", "trades", "taker_buy_quote"):
        fields[name] = panel[name].where(~fill, 0.0)
    for name in ("premium", "oi_value", "ls_top", "ls_account", "taker_ls_ratio"):
        if name in fields:
            fields[name] = panel[name].where(~fill, panel[name].ffill(limit=max_gap))
    out = Panel(fields, bar=panel.bar, meta=dict(panel.meta))
    out.meta["filled_bars"] = int(fill.to_numpy().sum())
    return out
