"""Mappings entre les noms/unités OKX et le domaine (§45, §46).

Référence : https://app.okx.com/docs-v5/en/ — vérifiée le 2026-09-18. Toute unité documentée ici est
reprise dans ``docs/data_dictionary.md``. Aucune conversion n'est faite « au jugé » : une unité non
listée est refusée par ``unit_of``.
"""

from __future__ import annotations

from typing import Final

from okxq.domain.errors import UnitError

VERIFIED_AT: Final = "2026-09-18"
DOCS_URL: Final = "https://app.okx.com/docs-v5/en/"

#: canal WebSocket public → famille d'événement interne (docs/event_schemas.md)
CHANNEL_TO_EVENT_TYPE: Final[dict[str, str]] = {
    "books": "book",
    "books-l2-tbt": "book",
    "books50-l2-tbt": "book",
    "books5": "book",
    "bbo-tbt": "book",
    "trades": "trade",
    "mark-price": "mark_price",
    "index-tickers": "index_price",
    "funding-rate": "funding",
    "open-interest": "open_interest",
    "instruments": "instrument",
    "tickers": "ticker",
}

#: canaux de carnet et leur profondeur maximale documentée
BOOK_CHANNEL_DEPTH: Final[dict[str, int]] = {
    "books": 400,
    "books-l2-tbt": 400,
    "books50-l2-tbt": 50,
    "books5": 5,
    "bbo-tbt": 1,
}

#: canaux de carnet séquencés (snapshot puis updates avec seqId/prevSeqId)
SEQUENCED_BOOK_CHANNELS: Final[frozenset[str]] = frozenset({"books", "books-l2-tbt", "books50-l2-tbt"})

#: canaux servis par l'endpoint WebSocket « business » (bougies) plutôt que « public »
BUSINESS_CHANNEL_PREFIXES: Final[tuple[str, ...]] = ("candle", "mark-price-candle", "index-candle")

#: canaux PRIVÉS : jamais souscrits par le collecteur public (séparation des secrets, principe 9)
PRIVATE_CHANNELS: Final[frozenset[str]] = frozenset(
    {
        "account",
        "positions",
        "balance_and_position",
        "orders",
        "orders-algo",
        "algo-advance",
        "liquidation-warning",
        "account-greeks",
        "fills",
    }
)

#: barres de bougies supportées (nom OKX → suffixe interne)
CANDLE_BARS: Final[dict[str, str]] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1H": "1h",
    "2H": "2h",
    "4H": "4h",
    "1D": "1d",
}

BAR_MILLISECONDS: Final[dict[str, int]] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

#: unités des champs OKX utilisés (SWAP linéaire USDT) : « contracts » = nombre de contrats,
#: « base » = devise de base (ctValCcy), « quote » = USDT, « rate » = fraction sans unité.
FIELD_UNITS: Final[dict[str, str]] = {
    "sz": "contracts",
    "vol": "contracts",
    "vol24h": "contracts",
    "volCcy": "base",
    "volCcy24h": "base",
    "volCcyQuote": "quote",
    "oi": "contracts",
    "oiCcy": "base",
    "oiUsd": "usd",
    "px": "quote",
    "last": "quote",
    "bidPx": "quote",
    "askPx": "quote",
    "bidSz": "contracts",
    "askSz": "contracts",
    "markPx": "quote",
    "idxPx": "quote",
    "fundingRate": "rate",
    "realizedRate": "rate",
    "settFundingRate": "rate",
    "nextFundingRate": "rate",
    "ctVal": "base",
    "tickSz": "quote",
    "lotSz": "contracts",
    "minSz": "contracts",
}


def unit_of(okx_field: str) -> str:
    """Unité documentée d'un champ OKX ; refuse un champ non répertorié plutôt que de deviner."""
    try:
        return FIELD_UNITS[okx_field]
    except KeyError as exc:
        raise UnitError(f"unité inconnue pour le champ OKX {okx_field!r}", field=okx_field) from exc


def candle_channel(bar: str) -> str:
    """``"1m"`` → ``"candle1m"`` (canal WebSocket) ; le suffixe interne est ``CANDLE_BARS[bar]``."""
    if bar not in CANDLE_BARS:
        raise UnitError(f"barre de bougie non supportée : {bar!r}", bar=bar)
    return f"candle{bar}"


def candle_event_type(channel: str) -> str:
    """``"candle1m"`` → ``"candle.1m"``. Refuse un canal non répertorié."""
    if not channel.startswith("candle"):
        raise UnitError(f"canal de bougie inattendu : {channel!r}", channel=channel)
    bar = channel.removeprefix("candle")
    if bar not in CANDLE_BARS:
        raise UnitError(f"barre de bougie non supportée : {bar!r}", bar=bar)
    return f"candle.{CANDLE_BARS[bar]}"


def bar_for_event_type(event_type: str) -> str:
    """``"candle.1m"`` → ``"1m"`` (nom OKX), pour la détection de trous."""
    suffix = event_type.removeprefix("candle.")
    for okx_bar, internal in CANDLE_BARS.items():
        if internal == suffix:
            return okx_bar
    raise UnitError(f"type de bougie inconnu : {event_type!r}", event_type=event_type)


def event_type_for_channel(channel: str) -> str:
    if channel.startswith("candle"):
        return candle_event_type(channel)
    try:
        return CHANNEL_TO_EVENT_TYPE[channel]
    except KeyError as exc:
        raise UnitError(f"canal OKX non répertorié : {channel!r}", channel=channel) from exc


def is_business_channel(channel: str) -> bool:
    return channel.startswith(BUSINESS_CHANNEL_PREFIXES)


def index_id_for_swap(inst_id: str) -> str:
    """``BTC-USDT-SWAP`` → indice ``BTC-USDT`` (canal ``index-tickers``, ``instId`` = nom d'indice)."""
    if not inst_id.endswith("-SWAP"):
        raise UnitError("identifiant de swap attendu", inst_id=inst_id)
    return inst_id.removesuffix("-SWAP")


def swap_id_for_index(index_id: str) -> str:
    return f"{index_id}-SWAP"


def taker_side(okx_side: str) -> str:
    """Convention OKX vérifiée (fixture ``ws_trades.json``) : ``side`` du canal trades = côté du TAKER."""
    side = okx_side.lower()
    if side not in ("buy", "sell"):
        raise UnitError(f"côté de trade inconnu : {okx_side!r}", side=okx_side)
    return side
