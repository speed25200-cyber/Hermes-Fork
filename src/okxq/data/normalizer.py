"""Normalisation des messages OKX vers ``EventEnvelope`` (§5, docs/event_schemas.md).

Ce module est la SEULE frontière où un message brut de fournisseur devient un événement interne. Il
applique trois règles qui ne se négocient pas :

1. **Les horloges ne se confondent pas.** ``exchange_ts`` est l'heure économique déclarée par OKX ;
   ``receive_ts`` est l'heure à laquelle NOTRE système a appris l'événement, lue sur l'horloge injectée ;
   ``available_at`` est l'heure à partir de laquelle il est utilisable (après normalisation). Une date
   absente reste ``None`` : jamais l'heure de collecte en substitut silencieux (§5, T58).
2. **Tous les nombres sortent en chaînes.** Aucun flottant n'entre dans un payload : la précision
   décimale d'un prix ou d'une quantité doit survivre à la sérialisation.
3. **``ingest_seq`` est monotone par source.** C'est lui qui départage deux événements de même
   horodatage, selon l'ordre de réception réellement enregistré (§34) — pas une causalité supposée
   entre canaux indépendants.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from okxq.domain.clocks import Clock, ensure_utc
from okxq.domain.errors import DataQualityError
from okxq.domain.events import EventEnvelope
from okxq.domain.ids import new_id, payload_hash
from okxq.exchange.okx.mappings import (
    CHANNEL_TO_EVENT_TYPE,
    PRIVATE_CHANNELS,
    candle_event_type,
    taker_side,
)

__all__ = ["SCHEMA_VERSION", "Normalizer", "ms_to_datetime"]

SCHEMA_VERSION: Final = 1

#: Chemins REST publics reconnus → type d'événement interne.
REST_PATH_TO_EVENT: Final[dict[str, str]] = {
    "public/instruments": "instrument",
    "public/open-interest": "open_interest",
    "public/funding-rate-history": "funding",
    "public/mark-price": "mark_price",
    "public/price-limit": "price_limit",
    "market/books": "book.snapshot",
    "market/history-candles": "candle",
    "market/history-trades": "trade",
    "market/tickers": "ticker",
}


def ms_to_datetime(value: Any, *, field_name: str = "ts") -> datetime | None:
    """Millisecondes epoch → datetime UTC. Une valeur absente ou vide rend ``None``, jamais « maintenant »."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value, field=field_name)
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        raise DataQualityError(f"{field_name} invalide : {value!r}", field=field_name) from exc


def _s(value: Any) -> str:
    """Nombre → chaîne, sans passer par un flottant."""
    return "" if value is None else str(value)


def _levels(raw: Any) -> list[list[str]]:
    """Niveaux OKX ``[px, sz, liqOrders, numOrders]`` → ``[[px, sz], ...]`` en chaînes."""
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise DataQualityError("niveaux de carnet mal formés")
    out: list[list[str]] = []
    for row in raw:
        if not isinstance(row, Sequence) or isinstance(row, str | bytes) or len(row) < 2:
            raise DataQualityError("niveau de carnet mal formé", level=repr(row))
        out.append([_s(row[0]), _s(row[1])])
    return out


class Normalizer:
    """Fabrique d'enveloppes. Une instance par source (un flux, un fichier, un fournisseur)."""

    def __init__(
        self,
        clock: Clock,
        *,
        source: str,
        schema_version: int = SCHEMA_VERSION,
        id_factory: Callable[[int], str] | None = None,
    ) -> None:
        """``id_factory`` permet à un générateur de fixtures d'attribuer des identifiants DÉTERMINISTES.

        En exploitation, l'identifiant reste aléatoire et triable par temps (``new_id``) : deux
        ingestions distinctes du même fait ne doivent pas partager un identifiant d'événement.
        """
        self._clock = clock
        self.source = source
        self.schema_version = schema_version
        self._seq = 0
        self._id_factory = id_factory
        self.rejected: list[dict[str, str]] = []

    # --- fabrique ---------------------------------------------------------------------------------------

    def _envelope(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        exchange_ts: datetime | None,
        available_delay_ms: int = 0,
    ) -> EventEnvelope:
        receive_ts = ensure_utc(self._clock.now_utc())
        self._seq += 1
        available_at = receive_ts
        if available_delay_ms:
            available_at = receive_ts.fromtimestamp(
                receive_ts.timestamp() + available_delay_ms / 1000, tz=UTC
            )
        return EventEnvelope(
            event_id=self._id_factory(self._seq) if self._id_factory is not None else new_id("evt"),
            event_type=event_type,
            schema_version=self.schema_version,
            source=self.source,
            exchange_ts=exchange_ts,
            receive_ts=receive_ts,
            available_at=available_at,
            ingest_seq=self._seq,
            payload_hash=payload_hash(payload),
            payload=payload,
        )

    def _reject(self, reason: str, detail: str) -> None:
        """Un message refusé est COMPTÉ et nommé : un rejet silencieux masque une divergence de contrat."""
        self.rejected.append({"reason": reason, "detail": detail[:300]})

    # --- WebSocket --------------------------------------------------------------------------------------

    def normalize_ws(self, message: Mapping[str, Any]) -> list[EventEnvelope]:
        """Message WebSocket public ``{arg:{channel,instId}, action?, data:[...]}`` → enveloppes."""
        arg = message.get("arg")
        if not isinstance(arg, Mapping):
            self._reject("ARG_ABSENT", repr(message)[:200])
            return []
        channel = str(arg.get("channel", ""))
        if channel in PRIVATE_CHANNELS:
            # Le collecteur public ne doit jamais voir un canal privé : c'est une erreur de câblage.
            raise DataQualityError("canal privé reçu par le normaliseur public", channel=channel)
        data = message.get("data")
        if not isinstance(data, Sequence) or isinstance(data, str | bytes):
            self._reject("DATA_ABSENT", f"channel={channel}")
            return []
        inst_id = str(arg.get("instId", "")) or None
        action = str(message.get("action", "")) or None

        if channel.startswith("candle"):
            return self._candles_ws(channel, inst_id, data)
        family = CHANNEL_TO_EVENT_TYPE.get(channel)
        if family is None:
            self._reject("CANAL_INCONNU", channel)
            return []
        out: list[EventEnvelope] = []
        for row in data:
            if not isinstance(row, Mapping):
                self._reject("LIGNE_INVALIDE", f"channel={channel}")
                continue
            try:
                envelope = self._one_ws(channel, family, inst_id, action, row)
            except DataQualityError as exc:
                self._reject("PAYLOAD_INVALIDE", f"{channel}: {exc}")
                continue
            if envelope is not None:
                out.append(envelope)
        return out

    def _one_ws(
        self,
        channel: str,
        family: str,
        inst_id: str | None,
        action: str | None,
        row: Mapping[str, Any],
    ) -> EventEnvelope | None:
        inst = str(row.get("instId", "")) or inst_id
        if family == "book":
            if inst is None:
                raise DataQualityError("carnet sans instrument", channel=channel)
            # books5/bbo-tbt republient tout le top N : ce sont des snapshots, quoi que dise `action`.
            is_snapshot = action == "snapshot" or channel in ("books5", "bbo-tbt")
            payload = {
                "inst_id": inst,
                "channel": channel,
                "seq_id": _s(row.get("seqId")) or None,
                "prev_seq_id": _s(row.get("prevSeqId")) or None,
                "checksum": _s(row.get("checksum")) or None,
                "ts_ms": _s(row.get("ts")),
                "bids": _levels(row.get("bids")),
                "asks": _levels(row.get("asks")),
            }
            return self._envelope(
                "book.snapshot" if is_snapshot else "book.update",
                payload,
                exchange_ts=ms_to_datetime(row.get("ts")),
            )
        if family == "trade":
            payload = {
                "inst_id": inst,
                "trade_id": _s(row.get("tradeId")),
                "price": _s(row.get("px")),
                "qty_contracts": _s(row.get("sz")),
                "side": taker_side(_s(row.get("side"))),
                "ts_ms": _s(row.get("ts")),
            }
            return self._envelope("trade", payload, exchange_ts=ms_to_datetime(row.get("ts")))
        if family == "mark_price":
            payload = {"inst_id": inst, "mark_px": _s(row.get("markPx")), "ts_ms": _s(row.get("ts"))}
            return self._envelope("mark_price", payload, exchange_ts=ms_to_datetime(row.get("ts")))
        if family == "index_price":
            payload = {"inst_id": inst, "idx_px": _s(row.get("idxPx")), "ts_ms": _s(row.get("ts"))}
            return self._envelope("index_price", payload, exchange_ts=ms_to_datetime(row.get("ts")))
        if family == "funding":
            return self._funding(inst, row)
        if family == "open_interest":
            payload = {
                "inst_id": inst,
                "oi_contracts": _s(row.get("oi")),
                "oi_base": _s(row.get("oiCcy")),
                "ts_ms": _s(row.get("ts")),
            }
            return self._envelope("open_interest", payload, exchange_ts=ms_to_datetime(row.get("ts")))
        if family == "instrument":
            return self._instrument(row)
        if family == "ticker":
            payload = {
                "inst_id": inst,
                "last": _s(row.get("last")),
                "bid_px": _s(row.get("bidPx")),
                "ask_px": _s(row.get("askPx")),
                # volCcy24h est en devise de BASE : le notionnel exige une multiplication par le prix.
                "vol_ccy_24h_base": _s(row.get("volCcy24h")),
                "vol_24h_contracts": _s(row.get("vol24h")),
                "ts_ms": _s(row.get("ts")),
            }
            return self._envelope("ticker", payload, exchange_ts=ms_to_datetime(row.get("ts")))
        self._reject("FAMILLE_INCONNUE", family)
        return None

    def _funding(self, inst: str | None, row: Mapping[str, Any]) -> EventEnvelope:
        """Funding : l'échéance est LUE dans le contrat, jamais supposée (§11).

        ``settled`` distingue la valeur estimée disponible avant règlement du montant réellement réglé.
        """
        realized = _s(row.get("realizedRate")) or _s(row.get("settFundingRate"))
        # `settled` disait « un taux réglé est présent dans le message », alors qu'il doit dire « CE
        # taux, à CETTE échéance, a été réglé ». Or le flux d'OKX livre dans le même message
        # l'échéance À VENIR (`fundingTime`, dans le futur) et un taux déjà réglé de la période
        # précédente. L'enregistrement produit était donc un « taux réglé » dont l'heure de règlement
        # n'était pas encore arrivée : la plateforme le refusait, à juste titre, et toutes les
        # décisions échouaient sur `CAUSALITY_VIOLATION`.
        #
        # On ne marque donc réglé que si l'échéance portée par l'enregistrement est PASSÉE au moment
        # de l'observation. Sinon, c'est une estimation — ce qu'elle est réellement.
        #
        # Le choix est volontairement conservateur : au pire on renonce à un taux réglé et une
        # feature se dégrade visiblement ; au mieux on n'affirme jamais un règlement qui n'a pas eu
        # lieu. L'inverse fabriquerait une information, ce qui est toujours plus grave.
        echeance = ms_to_datetime(row.get("fundingTime"))
        observe_a = ms_to_datetime(row.get("ts")) or echeance
        regle = bool(realized) and echeance is not None and observe_a is not None and echeance <= observe_a
        payload = {
            "inst_id": inst,
            "funding_rate": _s(row.get("fundingRate")),
            "next_funding_rate": _s(row.get("nextFundingRate")) or None,
            "funding_time_ms": _s(row.get("fundingTime")),
            "next_funding_time_ms": _s(row.get("nextFundingTime")) or None,
            "settled": regle,
            "realized_rate": realized if regle else None,
        }
        return self._envelope(
            "funding", payload, exchange_ts=ms_to_datetime(row.get("ts") or row.get("fundingTime"))
        )

    def _instrument(self, row: Mapping[str, Any]) -> EventEnvelope:
        payload = {
            "inst_id": _s(row.get("instId")),
            "inst_type": _s(row.get("instType")),
            "ct_val": _s(row.get("ctVal")),
            "ct_val_ccy": _s(row.get("ctValCcy")),
            "ct_type": _s(row.get("ctType")),
            "ct_mult": _s(row.get("ctMult")) or "1",
            "settle_ccy": _s(row.get("settleCcy")),
            "tick_sz": _s(row.get("tickSz")),
            "lot_sz": _s(row.get("lotSz")),
            "min_sz": _s(row.get("minSz")),
            "state": _s(row.get("state")),
            "lever": _s(row.get("lever")) or None,
            "list_time_ms": _s(row.get("listTime")) or None,
        }
        return self._envelope("instrument", payload, exchange_ts=ms_to_datetime(row.get("listTime")))

    def _candles_ws(self, channel: str, inst_id: str | None, data: Sequence[Any]) -> list[EventEnvelope]:
        event_type = candle_event_type(channel)
        out: list[EventEnvelope] = []
        for row in data:
            try:
                out.append(self._candle(event_type, inst_id, row))
            except DataQualityError as exc:
                self._reject("BOUGIE_INVALIDE", f"{channel}: {exc}")
        return out

    def _candle(self, event_type: str, inst_id: str | None, row: Any) -> EventEnvelope:
        """Bougie OKX ``[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]``.

        ``confirm`` distingue une bougie CLÔTURÉE (``"1"``) d'une bougie en cours (``"0"``) : confondre
        les deux ferait fuir la clôture dans une feature intrabougie (T16).
        """
        if not isinstance(row, Sequence) or isinstance(row, str | bytes) or len(row) < 6:
            raise DataQualityError("bougie mal formée", row=repr(row)[:120])
        confirm = _s(row[8]) if len(row) > 8 else "0"
        if confirm not in ("0", "1"):
            raise DataQualityError("champ confirm inattendu", confirm=confirm)
        payload = {
            "inst_id": inst_id,
            "ts_ms": _s(row[0]),
            "open": _s(row[1]),
            "high": _s(row[2]),
            "low": _s(row[3]),
            "close": _s(row[4]),
            "vol_contracts": _s(row[5]),
            "vol_base": _s(row[6]) if len(row) > 6 else "",
            "vol_quote": _s(row[7]) if len(row) > 7 else "",
            "confirm": confirm,
        }
        return self._envelope(event_type, payload, exchange_ts=ms_to_datetime(row[0]))

    # --- REST -------------------------------------------------------------------------------------------

    def normalize_rest(
        self, path: str, data: Sequence[Any], *, inst_id: str | None = None, bar: str = "1m"
    ) -> list[EventEnvelope]:
        """Réponse REST publique → enveloppes. ``path`` est le chemin sans ``/api/v5/``."""
        key = path.strip("/").removeprefix("api/v5/")
        event = REST_PATH_TO_EVENT.get(key)
        if event is None:
            self._reject("CHEMIN_REST_INCONNU", key)
            return []
        out: list[EventEnvelope] = []
        for row in data:
            try:
                if event == "instrument":
                    out.append(self._instrument(row))
                elif event == "candle":
                    out.append(self._candle(f"candle.{bar}", inst_id, row))
                elif event == "book.snapshot":
                    if not isinstance(row, Mapping) or inst_id is None:
                        raise DataQualityError("snapshot REST sans instrument")
                    payload = {
                        "inst_id": inst_id,
                        # La provenance REST est portée dans le canal : un snapshot REST n'est PAS
                        # raccordable à des incréments WebSocket (§47, T13).
                        "channel": "rest:books",
                        "seq_id": _s(row.get("seqId")) or None,
                        "prev_seq_id": None,
                        "checksum": None,
                        "ts_ms": _s(row.get("ts")),
                        "bids": _levels(row.get("bids")),
                        "asks": _levels(row.get("asks")),
                    }
                    out.append(
                        self._envelope("book.snapshot", payload, exchange_ts=ms_to_datetime(row.get("ts")))
                    )
                elif event == "trade":
                    if not isinstance(row, Mapping):
                        raise DataQualityError("trade REST mal formé")
                    payload = {
                        "inst_id": str(row.get("instId", "")) or inst_id,
                        "trade_id": _s(row.get("tradeId")),
                        "price": _s(row.get("px")),
                        "qty_contracts": _s(row.get("sz")),
                        "side": taker_side(_s(row.get("side"))),
                        "ts_ms": _s(row.get("ts")),
                    }
                    out.append(self._envelope("trade", payload, exchange_ts=ms_to_datetime(row.get("ts"))))
                elif event == "funding":
                    if not isinstance(row, Mapping):
                        raise DataQualityError("funding REST mal formé")
                    out.append(self._funding(str(row.get("instId", "")) or inst_id, row))
                elif event == "open_interest":
                    if not isinstance(row, Mapping):
                        raise DataQualityError("open interest REST mal formé")
                    payload = {
                        "inst_id": str(row.get("instId", "")) or inst_id,
                        "oi_contracts": _s(row.get("oi")),
                        "oi_base": _s(row.get("oiCcy")),
                        "ts_ms": _s(row.get("ts")),
                    }
                    out.append(
                        self._envelope("open_interest", payload, exchange_ts=ms_to_datetime(row.get("ts")))
                    )
                elif event == "mark_price":
                    if not isinstance(row, Mapping):
                        raise DataQualityError("mark price REST mal formé")
                    payload = {
                        "inst_id": str(row.get("instId", "")) or inst_id,
                        "mark_px": _s(row.get("markPx")),
                        "ts_ms": _s(row.get("ts")),
                    }
                    out.append(
                        self._envelope("mark_price", payload, exchange_ts=ms_to_datetime(row.get("ts")))
                    )
                elif event == "price_limit":
                    if not isinstance(row, Mapping):
                        raise DataQualityError("price limit REST mal formé")
                    payload = {
                        "inst_id": str(row.get("instId", "")) or inst_id,
                        "buy_limit": _s(row.get("buyLmt")),
                        "sell_limit": _s(row.get("sellLmt")),
                        "ts_ms": _s(row.get("ts")),
                    }
                    out.append(
                        self._envelope("price_limit", payload, exchange_ts=ms_to_datetime(row.get("ts")))
                    )
                elif event == "ticker":
                    if not isinstance(row, Mapping):
                        raise DataQualityError("ticker REST mal formé")
                    payload = {
                        "inst_id": str(row.get("instId", "")) or inst_id,
                        "last": _s(row.get("last")),
                        "bid_px": _s(row.get("bidPx")),
                        "ask_px": _s(row.get("askPx")),
                        "vol_ccy_24h_base": _s(row.get("volCcy24h")),
                        "vol_24h_contracts": _s(row.get("vol24h")),
                        "ts_ms": _s(row.get("ts")),
                    }
                    out.append(self._envelope("ticker", payload, exchange_ts=ms_to_datetime(row.get("ts"))))
            except DataQualityError as exc:
                self._reject("PAYLOAD_INVALIDE", f"{key}: {exc}")
        return out
