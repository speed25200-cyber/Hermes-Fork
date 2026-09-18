"""Features de microstructure (§8, §35) : carnet, trades, mises à jour de quotes.

Formules (toutes documentées dans ``docs/strategy_research.md``) :
- ``spread = ask1 - bid1`` ; ``relative_spread = spread / mid`` ; ``mid = (ask1 + bid1) / 2`` ;
- ``microprice = (ask1 * bid_qty1 + bid1 * ask_qty1) / (bid_qty1 + ask_qty1)`` ;
- ``imbalance_lk = (Σ bid_qty[:k] - Σ ask_qty[:k]) / (Σ bid_qty[:k] + Σ ask_qty[:k])`` ;
- ``imbalance_bps_b`` : même formule sur le NOTIONNEL des niveaux à moins de ``b`` bps du mid ;
- profondeur en notionnel USDT : ``qty_contracts * v * price`` (``v = base_units_per_contract``) ;
- ``depth_slope`` : pente OLS du notionnel cumulé (deux côtés) en fonction de la distance au mid en bps ;
- ``depth_concentration`` : part du niveau 1 dans le notionnel des 5 premiers niveaux (deux côtés) ;
- trades : volumes agressifs par côté taker, imbalance, volume signé, taux d'arrivée par seconde ;
- ``rv_short`` : racine de la somme des log-rendements carrés du mid sur les mises à jour de la fenêtre ;
- régimes : ratio de la valeur courante à la médiane de la fenêtre (1 = normal).

Invalidité (§35) : quantité nulle ou négative, prix non positif, carnet croisé ou niveaux non triés →
toutes les features d'état de carnet sont INVALID avec raison. Un carnet absent est MISSING ; un carnet
plus vieux que ``max_age_s`` est STALE.
"""

from __future__ import annotations

import math

import numpy as np

from okxq.features.definitions import (
    FeatureDefinition,
    FeatureResult,
    MissingPolicy,
    TemporalSemantics,
    all_invalid,
    all_missing,
    invalid,
    missing,
    ok,
    stale,
)
from okxq.features.state import BookState, MidHistory, PointInTimeMarketState, TradeWindow

GROUP = "microstructure"
VERSION = "micro-v1"
BOOK_MAX_AGE_S = 5
TRADE_WINDOW_S = 60
HISTORY_WINDOW_S = 300
DEPTH_BANDS_BPS = (10, 25)
MAX_LEVELS = 5


def _d(
    name: str,
    semantics: TemporalSemantics,
    lookback_s: int,
    unit: str,
    normalization: str = "none",
    description: str = "",
    missing_policy: MissingPolicy = MissingPolicy.MASK,
    max_age_s: int | None = None,
) -> FeatureDefinition:
    return FeatureDefinition(
        name=name,
        version=VERSION,
        source="book" if semantics is TemporalSemantics.BOOK_STATE else "trades/book_updates",
        temporal_semantics=semantics,
        lookback_s=lookback_s,
        normalization=normalization,
        unit=unit,
        missing_policy=missing_policy,
        max_age_s=max_age_s,
        group=GROUP,
        description=description,
    )


_B = TemporalSemantics.BOOK_STATE
_T = TemporalSemantics.TRADE_WINDOW

BOOK_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    _d("spread", _B, 0, "price", "none", "ask1 - bid1", MissingPolicy.STALE_BOUNDED, BOOK_MAX_AGE_S),
    _d("relative_spread", _B, 0, "fraction", "by_mid", "spread / mid", MissingPolicy.STALE_BOUNDED, 5),
    _d("mid", _B, 0, "price", "none", "(ask1 + bid1) / 2", MissingPolicy.STALE_BOUNDED, 5),
    _d(
        "microprice",
        _B,
        0,
        "price",
        "none",
        "prix pondéré par les quantités L1",
        MissingPolicy.STALE_BOUNDED,
        5,
    ),
    _d("microprice_mid_divergence", _B, 0, "fraction", "by_mid", "(microprice - mid) / mid"),
    _d("imbalance_l1", _B, 0, "ratio", "signed_ratio", "déséquilibre niveau 1"),
    _d("imbalance_l5", _B, 0, "ratio", "signed_ratio", "déséquilibre 5 niveaux"),
    _d("imbalance_bps_10", _B, 0, "ratio", "signed_ratio", "déséquilibre notionnel à ±10 bps"),
    _d("imbalance_bps_25", _B, 0, "ratio", "signed_ratio", "déséquilibre notionnel à ±25 bps"),
    _d("depth_bid_bps_10", _B, 0, "usdt", "none", "notionnel bid à moins de 10 bps"),
    _d("depth_ask_bps_10", _B, 0, "usdt", "none", "notionnel ask à moins de 10 bps"),
    _d("depth_slope", _B, 0, "usdt_per_bps", "none", "pente OLS du notionnel cumulé vs distance"),
    _d("depth_concentration", _B, 0, "fraction", "none", "part du niveau 1 dans les 5 niveaux"),
)

TRADE_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    _d(
        "trade_imbalance_60s",
        _T,
        TRADE_WINDOW_S,
        "ratio",
        "signed_ratio",
        "(achats - ventes)/(achats + ventes)",
    ),
    _d("aggressive_buy_volume_60s", _T, TRADE_WINDOW_S, "contracts", "none", "volume taker acheteur"),
    _d("aggressive_sell_volume_60s", _T, TRADE_WINDOW_S, "contracts", "none", "volume taker vendeur"),
    _d("signed_volume_60s", _T, TRADE_WINDOW_S, "contracts", "none", "achats - ventes"),
    _d("trade_arrival_rate_60s", _T, TRADE_WINDOW_S, "count_per_s", "none", "trades par seconde"),
    _d(
        "quote_update_rate_60s",
        _T,
        TRADE_WINDOW_S,
        "count_per_s",
        "none",
        "mises à jour de carnet par seconde",
    ),
    _d(
        "rv_short_300s",
        _T,
        HISTORY_WINDOW_S,
        "fraction",
        "none",
        "volatilité réalisée du mid (non annualisée)",
    ),
    _d("mid_momentum_60s", _T, TRADE_WINDOW_S, "fraction", "none", "mid_now / mid_(t-60s) - 1"),
    _d("spread_regime_300s", _T, HISTORY_WINDOW_S, "ratio", "by_median", "spread relatif / médiane 300 s"),
    _d("liquidity_regime_300s", _T, HISTORY_WINDOW_S, "ratio", "by_median", "profondeur L1 / médiane 300 s"),
)

DEFINITIONS: tuple[FeatureDefinition, ...] = BOOK_DEFINITIONS + TRADE_DEFINITIONS
BOOK_NAMES = [d.name for d in BOOK_DEFINITIONS]
TRADE_NAMES = [d.name for d in TRADE_DEFINITIONS]


def book_invalid_reason(book: BookState) -> str | None:
    """Retourne la raison d'invalidité, ou None si le carnet est exploitable."""
    if book.is_empty:
        return "empty_side"
    for arr, label in ((book.bid_px, "bid_px"), (book.ask_px, "ask_px")):
        if bool(np.any(~np.isfinite(arr))) or bool(np.any(arr <= 0)):
            return f"non_positive_{label}"
    for arr, label in ((book.bid_qty, "bid_qty"), (book.ask_qty, "ask_qty")):
        if bool(np.any(~np.isfinite(arr))) or bool(np.any(arr <= 0)):
            return f"non_positive_{label}"
    if book.bid_px.size > 1 and bool(np.any(np.diff(book.bid_px) >= 0)):
        return "bids_not_descending"
    if book.ask_px.size > 1 and bool(np.any(np.diff(book.ask_px) <= 0)):
        return "asks_not_ascending"
    if float(book.bid_px[0]) >= float(book.ask_px[0]):
        return "crossed_book"
    return None


def _band_notional(px: np.ndarray, qty: np.ndarray, mid: float, band_bps: float, v: float) -> float:
    dist_bps = np.abs(px - mid) / mid * 1e4
    mask = dist_bps <= band_bps
    return float(np.sum(px[mask] * qty[mask] * v))


def _signed_ratio(a: float, b: float) -> FeatureResult:
    tot = a + b
    if tot <= 0:
        return invalid("zero_total")
    return ok((a - b) / tot)


def compute_book_features(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    book = state.book
    if book is None:
        return all_missing(BOOK_NAMES, "no_book")
    age = state.cutoff_s - book.ts.timestamp()
    reason = book_invalid_reason(book)
    if reason is not None:
        return all_invalid(BOOK_NAMES, reason)
    if age > BOOK_MAX_AGE_S:
        return {n: stale(f"book_age_{age:.1f}s") for n in BOOK_NAMES}
    v = state.base_units_per_contract
    bid1, ask1 = float(book.bid_px[0]), float(book.ask_px[0])
    bq1, aq1 = float(book.bid_qty[0]), float(book.ask_qty[0])
    mid = (ask1 + bid1) / 2.0
    spread = ask1 - bid1
    micro = (ask1 * bq1 + bid1 * aq1) / (bq1 + aq1)
    k = MAX_LEVELS
    out: dict[str, FeatureResult] = {
        "spread": ok(spread),
        "relative_spread": ok(spread / mid),
        "mid": ok(mid),
        "microprice": ok(micro),
        "microprice_mid_divergence": ok((micro - mid) / mid),
        "imbalance_l1": _signed_ratio(bq1, aq1),
        "imbalance_l5": _signed_ratio(float(np.sum(book.bid_qty[:k])), float(np.sum(book.ask_qty[:k]))),
    }
    for band in DEPTH_BANDS_BPS:
        bid_n = _band_notional(book.bid_px, book.bid_qty, mid, band, v)
        ask_n = _band_notional(book.ask_px, book.ask_qty, mid, band, v)
        out[f"imbalance_bps_{band}"] = _signed_ratio(bid_n, ask_n)
        if band == 10:
            out["depth_bid_bps_10"] = ok(bid_n)
            out["depth_ask_bps_10"] = ok(ask_n)
    # pente : notionnel cumulé (deux côtés confondus) en fonction de la distance au mid, niveaux ≤ 25 bps
    px = np.concatenate([book.bid_px[:k], book.ask_px[:k]])
    qty = np.concatenate([book.bid_qty[:k], book.ask_qty[:k]])
    dist = np.abs(px - mid) / mid * 1e4
    order = np.argsort(dist, kind="stable")
    dist_sorted = dist[order]
    cum = np.cumsum((px * qty * v)[order])
    sel = dist_sorted <= max(DEPTH_BANDS_BPS)
    if int(np.sum(sel)) >= 2 and float(np.var(dist_sorted[sel])) > 0:
        x = dist_sorted[sel]
        y = cum[sel]
        slope = float(np.cov(x, y, bias=True)[0, 1] / np.var(x))
        out["depth_slope"] = ok(slope)
    else:
        out["depth_slope"] = invalid("insufficient_levels_for_slope")
    top = float(np.sum(px * qty * v))
    l1 = (bid1 * bq1 + ask1 * aq1) * v
    out["depth_concentration"] = ok(l1 / top) if top > 0 else invalid("zero_depth")
    return out


def _window(trades: TradeWindow, start_s: float, end_s: float) -> TradeWindow:
    sel = (trades.ts_s > start_s) & (trades.ts_s <= end_s)
    return TradeWindow(trades.ts_s[sel], trades.price[sel], trades.qty[sel], trades.side_sign[sel])


def _history_window(hist: MidHistory, start_s: float, end_s: float) -> MidHistory:
    sel = (hist.ts_s > start_s) & (hist.ts_s <= end_s)
    return MidHistory(hist.ts_s[sel], hist.mid[sel], hist.rel_spread[sel], hist.depth_notional[sel])


def compute_trade_features(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    out: dict[str, FeatureResult] = {}
    cutoff = state.cutoff_s
    hist = state.book_history
    feed_alive = hist is not None and hist.size > 0 and float(hist.ts_s[-1]) > cutoff - HISTORY_WINDOW_S
    trades = state.trades
    if trades is None or not feed_alive:
        reason = "no_trade_feed" if trades is None else "book_feed_inactive"
        out.update(all_missing(TRADE_NAMES[:6], reason))
    else:
        w = _window(trades, cutoff - TRADE_WINDOW_S, cutoff)
        if bool(np.any(w.qty <= 0)) or bool(np.any(w.price <= 0)):
            out.update(all_invalid(TRADE_NAMES[:5], "non_positive_trade"))
        else:
            buy = float(np.sum(w.qty[w.side_sign > 0]))
            sell = float(np.sum(w.qty[w.side_sign < 0]))
            out["aggressive_buy_volume_60s"] = ok(buy)
            out["aggressive_sell_volume_60s"] = ok(sell)
            out["signed_volume_60s"] = ok(buy - sell)
            out["trade_arrival_rate_60s"] = ok(w.size / TRADE_WINDOW_S)
            out["trade_imbalance_60s"] = (
                _signed_ratio(buy, sell) if w.size > 0 else missing("no_trades_in_window")
            )
        assert hist is not None
        hw = _history_window(hist, cutoff - TRADE_WINDOW_S, cutoff)
        out["quote_update_rate_60s"] = ok(hw.size / TRADE_WINDOW_S)
    if hist is None or hist.size == 0:
        out.update(all_missing(TRADE_NAMES[6:], "no_book_history"))
        return out
    h = _history_window(hist, cutoff - HISTORY_WINDOW_S, cutoff)
    if h.size < 2:
        out["rv_short_300s"] = missing("insufficient_book_updates")
    elif bool(np.any(h.mid <= 0)):
        out["rv_short_300s"] = invalid("non_positive_mid_in_history")
    else:
        lr = np.diff(np.log(h.mid))
        out["rv_short_300s"] = ok(math.sqrt(float(np.sum(lr * lr))))
    # momentum : dernier mid observé à ou avant cutoff - 60 s, contre le dernier mid observé
    before = hist.ts_s <= cutoff - TRADE_WINDOW_S
    if not bool(np.any(before)) or hist.size == 0 or float(hist.ts_s[-1]) > cutoff:
        out["mid_momentum_60s"] = missing("no_mid_60s_ago")
    else:
        ref = float(hist.mid[before][-1])
        now = float(hist.mid[hist.ts_s <= cutoff][-1])
        out["mid_momentum_60s"] = ok(now / ref - 1.0) if ref > 0 else invalid("non_positive_mid")
    if h.size < 3:
        out["spread_regime_300s"] = missing("insufficient_book_updates")
        out["liquidity_regime_300s"] = missing("insufficient_book_updates")
    else:
        med_spread = float(np.median(h.rel_spread))
        med_depth = float(np.median(h.depth_notional))
        cur_spread = float(h.rel_spread[-1])
        cur_depth = float(h.depth_notional[-1])
        out["spread_regime_300s"] = (
            ok(cur_spread / med_spread) if med_spread > 0 else invalid("zero_median_spread")
        )
        out["liquidity_regime_300s"] = (
            ok(cur_depth / med_depth) if med_depth > 0 else invalid("zero_median_depth")
        )
    return out


def compute(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    out = compute_book_features(state)
    out.update(compute_trade_features(state))
    return out
