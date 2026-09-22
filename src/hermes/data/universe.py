"""Point-in-time tradable universe.

The universe at date ``d`` is the ``top_n`` contracts by trailing dollar volume computed from data strictly
before ``d``, among contracts listed for at least ``min_history_days``. Membership is refreshed every
``reselect_every_days`` and held constant in between (turnover of the universe itself costs money).
Selecting "the coins that moved" with hindsight is the single most common way crypto backtests lie; this
module makes that impossible by construction.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hermes.config import UniverseConfig
from hermes.data.panel import Panel

STABLE_OR_INDEX = frozenset(
    {"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "BTCDOM", "DEFI", "EUR", "GBP", "USDE", "USD1", "RLUSD", "PYUSD"}
)
# Perpetuals on equities, ETFs and commodities: their underlying market closes at night and on weekends,
# their drivers are not crypto's, and they did not exist for most of the history. Excluded from a crypto model.
TRADFI = frozenset(
    {
        "XAU",
        "XAG",
        "PAXG",
        "XAUT",
        "XPT",
        "XPD",
        "WTI",
        "BRENT",
        "NATGAS",
        "COPPER",
        "TSLA",
        "NVDA",
        "MSTR",
        "QQQ",
        "SPY",
        "SOXL",
        "SOXS",
        "INTC",
        "MRVL",
        "SAMSUNG",
        "SKHYNIX",
        "SKHY",
        "CRCL",
        "EWY",
        "EWJ",
        "SNDK",
        "NBIS",
        "COIN",
        "AAPL",
        "AMZN",
        "GOOGL",
        "GOOG",
        "META",
        "MSFT",
        "HOOD",
        "PLTR",
        "AMD",
        "NFLX",
        "BABA",
        "TSM",
        "AVGO",
        "ORCL",
        "GME",
        "AMC",
        "MU",
        "SMCI",
        "ARM",
        "UBER",
        "DIS",
        "JPM",
        "IWM",
        "TLT",
        "GLD",
        "SLV",
        "USO",
        "UVXY",
        "TQQQ",
        "SQQQ",
        "NVDL",
        "TSLL",
        "MARA",
        "RIOT",
        "CRWV",
        "CRM",
    }
)


def base_asset(symbol: str, quote: str = "USDT") -> str:
    base = symbol[: -len(quote)] if symbol.endswith(quote) else symbol
    for prefix in ("1000000", "1000", "1M"):
        if base.startswith(prefix) and len(base) > len(prefix) + 1 and not base[len(prefix)].isdigit():
            return base[len(prefix) :]
    return base


def is_excluded(symbol: str, cfg: UniverseConfig) -> bool:
    if symbol in cfg.exclude or not symbol.isascii():
        return True
    base = symbol[: -len(cfg.quote)] if symbol.endswith(cfg.quote) else symbol
    return base in STABLE_OR_INDEX or base in TRADFI or base_asset(symbol, cfg.quote) in TRADFI


def universe_mask(panel: Panel, cfg: UniverseConfig, bars_per_day: int) -> pd.DataFrame:
    """Boolean (time x symbol) membership, causal: row ``t`` only uses data up to ``close(t-1)``."""
    qv = panel["quote_volume"]
    close = panel["close"]
    lookback = cfg.liquidity_lookback_days * bars_per_day
    # Trailing dollar volume known at the close of the previous bar.
    adv = qv.rolling(lookback, min_periods=max(1, lookback // 2)).mean().shift(1)
    alive = close.notna()
    age = alive.cumsum().shift(1).fillna(0)
    eligible = (age >= cfg.min_history_days * bars_per_day) & alive.shift(1, fill_value=False)
    eligible &= ~pd.DataFrame(
        np.broadcast_to([is_excluded(s, cfg) for s in qv.columns], qv.shape), index=qv.index, columns=qv.columns
    )
    score = adv.where(eligible)

    step = cfg.reselect_every_days * bars_per_day
    mask = pd.DataFrame(False, index=qv.index, columns=qv.columns)
    idx = np.arange(len(qv.index))
    anchors = idx[::step]
    current = np.zeros(qv.shape[1], dtype=bool)
    values = score.to_numpy()
    out = np.zeros(qv.shape, dtype=bool)
    anchor_set = set(anchors.tolist())
    for t in idx:
        if t in anchor_set:
            row = values[t]
            valid = np.isfinite(row)
            current = np.zeros_like(current)
            if valid.any():
                order = np.argsort(-np.where(valid, row, -np.inf))
                k = min(cfg.top_n, int(valid.sum()))
                current[order[:k]] = True
        out[t] = current
    mask.iloc[:, :] = out
    # A member that stops trading (delisting, halt) leaves immediately.
    return mask & alive


def candidates_from_daily(daily_quote_volume: pd.DataFrame, cfg: UniverseConfig) -> list[str]:
    """Symbols that were ever in the point-in-time top-N, from a cheap daily-volume panel.

    Used to decide which contracts deserve an intraday download: everything that could have been selected
    at some date, which includes contracts delisted later (no survivorship bias).
    """
    lookback = cfg.liquidity_lookback_days
    adv = daily_quote_volume.rolling(lookback, min_periods=max(1, lookback // 2)).mean().shift(1)
    age = daily_quote_volume.notna().cumsum().shift(1).fillna(0)
    adv = adv.where(age >= cfg.min_history_days)
    keep = [c for c in adv.columns if not is_excluded(c, cfg)]
    adv = adv[keep]
    ranks = adv.rank(axis=1, ascending=False)
    ever = (ranks <= cfg.top_n).any(axis=0)
    return sorted(ever[ever].index.tolist())
