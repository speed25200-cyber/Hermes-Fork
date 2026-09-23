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


EPOCH = pd.Timestamp("2020-01-06", tz="UTC")  # a Monday: reselection days are calendar-anchored


def daily_membership(daily_qv: pd.DataFrame, daily_alive: pd.DataFrame, cfg: UniverseConfig) -> pd.DataFrame:
    """Daily (day x symbol) membership. Day ``D`` uses data up to the end of ``D-1`` only.

    Reselection happens on calendar days (every ``reselect_every_days`` from a fixed Monday), so research
    and live select on the same dates whatever history each happens to hold.
    """
    lb = cfg.liquidity_lookback_days
    adv = daily_qv.rolling(lb, min_periods=max(1, lb // 2)).mean().shift(1)
    age = daily_alive.cumsum().shift(1).fillna(0)
    eligible = (age >= cfg.min_history_days) & daily_alive.shift(1, fill_value=False)
    excluded = np.array([is_excluded(s, cfg) for s in daily_qv.columns])
    score = adv.where(eligible).to_numpy(dtype=np.float64, copy=True)
    score[:, excluded] = np.nan
    days = daily_qv.index
    anchor = ((days - EPOCH).days % cfg.reselect_every_days) == 0
    out = np.zeros(score.shape, dtype=bool)
    current = np.zeros(score.shape[1], dtype=bool)
    started = False
    for t in range(len(days)):
        if anchor[t] or not started:
            row = score[t]
            valid = np.isfinite(row)
            current = np.zeros_like(current)
            if valid.any():
                order = np.argsort(-np.where(valid, row, -np.inf))
                current[order[: min(cfg.top_n, int(valid.sum()))]] = True
                started = True
        out[t] = current
    return pd.DataFrame(out, index=days, columns=daily_qv.columns)


def daily_activity(panel: Panel) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily quote volume and 'traded that day' flags aggregated from any base bar."""
    qv = panel["quote_volume"].resample("1D").sum(min_count=1)
    alive = panel["close"].resample("1D").count() > 0
    return qv, alive


def universe_mask(
    panel: Panel,
    cfg: UniverseConfig,
    bars_per_day: int | None = None,
    daily_qv: pd.DataFrame | None = None,
    daily_alive: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Boolean (bar x symbol) membership, causal: bars of day ``D`` use data up to the end of ``D-1``.

    ``daily_qv``/``daily_alive`` may be supplied from a longer daily history (the live engine keeps only a
    few weeks of base bars but needs months for liquidity and listing age); otherwise they are aggregated
    from the panel itself. ``bars_per_day`` is accepted for backward compatibility and unused.
    """
    del bars_per_day
    close = panel["close"]
    if daily_qv is None or daily_alive is None:
        daily_qv, daily_alive = daily_activity(panel)
    day_of_bar = close.index.floor("D")
    # Day D only reads D-1, so it needs a row even when its own daily bar is not closed yet (live: today).
    days = daily_qv.index
    if len(day_of_bar) and (len(days) == 0 or days[-1] < day_of_bar[-1]):
        first = days[0] if len(days) else day_of_bar[0]
        days = pd.date_range(first, day_of_bar[-1], freq="1D")
    daily_qv = daily_qv.reindex(index=days, columns=close.columns)
    daily_alive = daily_alive.reindex(index=days, columns=close.columns).fillna(False).astype(bool)
    mem = daily_membership(daily_qv, daily_alive, cfg)
    m = mem.reindex(day_of_bar).fillna(False).to_numpy(dtype=bool)
    mask = pd.DataFrame(m, index=close.index, columns=close.columns)
    # A member that stops trading (delisting, halt) leaves immediately.
    return mask & close.notna()


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
