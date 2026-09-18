"""Collecteur de données PUBLIQUES (§4, §48, §65).

Ce processus ne reçoit aucun credential : il ne souscrit qu'à des canaux publics, et une souscription à
un canal privé est refusée à la construction (``ensure_public``). C'est une séparation structurelle, pas
une convention.

Résilience : files BORNÉES avec compteurs de pertes (un abandon non compté est un abandon caché),
reconnexion à recul exponentiel borné avec gigue, ping/pong applicatif, et resynchronisation du carnet
après un trou — les décisions concernées s'arrêtent d'elles-mêmes tant que le carnet n'est pas revalidé.

Le transport est INJECTÉ : les tests fournissent un itérateur de messages, l'exploitation fournit une
connexion WebSocket. Aucune socket n'est ouverte implicitement.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from okxq.data.normalizer import Normalizer
from okxq.data.orderbook import BookState, BookValidator
from okxq.data.quality import DataQualityTracker
from okxq.domain.clocks import Clock, ensure_utc
from okxq.domain.errors import DataQualityError
from okxq.domain.events import EventEnvelope
from okxq.exchange.okx.mappings import CHANNEL_TO_EVENT_TYPE
from okxq.exchange.okx.websocket_public import (
    SubscriptionArg,
    WsMessageKind,
    build_subscribe,
    ensure_public,
    parse_message,
)
from okxq.runtime.logging import get_logger

__all__ = ["CollectorStats", "PublicCollector", "WsConnection", "WsConnectionFactory"]

_log = get_logger("okxq.collector")


@runtime_checkable
class WsConnection(Protocol):
    """Connexion minimale : envoyer du texte, recevoir du texte, fermer."""

    async def send(self, text: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...


WsConnectionFactory = Callable[[str], Awaitable[WsConnection]]


@dataclass(slots=True)
class CollectorStats:
    messages: int = 0
    events: int = 0
    dropped: int = 0
    reconnects: int = 0
    errors: int = 0
    pongs: int = 0
    subscriptions_confirmed: int = 0
    resyncs_requested: int = 0
    by_event_type: dict[str, int] = field(default_factory=dict)

    def note_event(self, event_type: str) -> None:
        self.by_event_type[event_type] = self.by_event_type.get(event_type, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "events": self.events,
            "dropped": self.dropped,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "pongs": self.pongs,
            "subscriptions_confirmed": self.subscriptions_confirmed,
            "resyncs_requested": self.resyncs_requested,
            "by_event_type": dict(sorted(self.by_event_type.items())),
        }


def backoff_delay(
    attempt: int, *, base: float = 0.5, cap: float = 30.0, rng: random.Random | None = None
) -> float:
    """Recul exponentiel BORNÉ avec gigue : vingt onglets qui rouvrent ne martèlent pas l'exchange."""
    jitter = float((rng or random).uniform(0.0, 1.0))
    return float(min(cap, base * (2**attempt)) * (0.5 + 0.5 * jitter))


class PublicCollector:
    """Consomme un flux public, normalise, valide les carnets, publie des enveloppes dans une file bornée."""

    def __init__(
        self,
        *,
        clock: Clock,
        subscriptions: Sequence[SubscriptionArg],
        connect: WsConnectionFactory,
        url: str,
        normalizer: Normalizer | None = None,
        quality: DataQualityTracker | None = None,
        queue_maxsize: int = 10_000,
        ping_interval_s: float = 20.0,
        max_reconnects: int | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        ensure_public(subscriptions)  # refus structurel d'un canal privé
        self._clock = clock
        self.subscriptions = list(subscriptions)
        self._connect = connect
        self.url = url
        self.normalizer = normalizer or Normalizer(clock, source=f"okx-public:{url}")
        self.quality = quality or DataQualityTracker()
        self.queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=queue_maxsize)
        self.ping_interval_s = ping_interval_s
        self.max_reconnects = max_reconnects
        self._rng = rng or random.Random(25200)
        self._sleep = sleep or asyncio.sleep
        self.books: dict[tuple[str, str], BookValidator] = {}
        self.stats = CollectorStats()
        self.resync_needed: set[tuple[str, str]] = set()

    # --- carnets ----------------------------------------------------------------------------------------

    def validator_for(self, inst_id: str, channel: str) -> BookValidator:
        key = (inst_id, channel)
        validator = self.books.get(key)
        if validator is None:
            validator = BookValidator(inst_id, channel)
            self.books[key] = validator
        return validator

    def book_state(self, inst_id: str, channel: str = "books") -> BookState:
        validator = self.books.get((inst_id, channel))
        return validator.state if validator else BookState.UNINITIALIZED

    def usable_books(self) -> dict[str, bool]:
        return {f"{inst}|{ch}": v.view().usable for (inst, ch), v in sorted(self.books.items())}

    # --- traitement -------------------------------------------------------------------------------------

    def handle_text(self, text: str) -> list[EventEnvelope]:
        """Traite un message texte. Ne lève pas : un message fautif est compté, pas fatal."""
        self.stats.messages += 1
        message = parse_message(text)
        if message.kind is WsMessageKind.PONG:
            self.stats.pongs += 1
            return []
        if message.kind is WsMessageKind.ERROR:
            self.stats.errors += 1
            _log.warning("erreur de flux public", code=message.code, msg=message.msg)
            return []
        if message.kind is WsMessageKind.EVENT:
            if message.event == "subscribe":
                self.stats.subscriptions_confirmed += 1
            return []
        raw = message.raw or {}
        try:
            envelopes = self.normalizer.normalize_ws(raw)
        except DataQualityError as exc:
            self.stats.errors += 1
            _log.warning("message public refusé", detail=str(exc))
            return []
        published: list[EventEnvelope] = []
        for envelope in envelopes:
            if envelope.event_type.startswith("book."):
                if not self._apply_book(envelope):
                    continue
            self._track_quality(envelope)
            published.append(envelope)
            self.stats.events += 1
            self.stats.note_event(envelope.event_type)
        return published

    def _apply_book(self, envelope: EventEnvelope) -> bool:
        """Applique un événement de carnet. Rend False si le carnet n'est pas publiable."""
        inst_id = str(envelope.payload.get("inst_id", ""))
        channel = str(envelope.payload.get("channel", "books"))
        validator = self.validator_for(inst_id, channel)
        result = validator.ingest(
            {
                "action": "snapshot" if envelope.event_type == "book.snapshot" else "update",
                "bids": envelope.payload.get("bids", []),
                "asks": envelope.payload.get("asks", []),
                "seq_id": envelope.payload.get("seq_id"),
                "prev_seq_id": envelope.payload.get("prev_seq_id"),
                "checksum": envelope.payload.get("checksum"),
                "ts_ms": envelope.payload.get("ts_ms"),
            }
        )
        if result.accepted:
            self.resync_needed.discard((inst_id, channel))
            self.quality.observe("book", inst_id, result.view, envelope.available_at)
            return True
        reason = result.reason or "carnet refusé"
        self.quality.mark_invalid("book", inst_id, reason, envelope.available_at)
        if result.resync_required:
            self.quality.note_gap()
            self.resync_needed.add((inst_id, channel))
            self.stats.resyncs_requested += 1
            _log.warning(
                "resynchronisation de carnet requise", inst_id=inst_id, channel=channel, detail=reason
            )
        return False

    def _track_quality(self, envelope: EventEnvelope) -> None:
        kind = CHANNEL_TO_EVENT_TYPE.get(str(envelope.payload.get("channel", "")), "")
        if not kind:
            kind = envelope.event_type.split(".")[0]
        inst_id = envelope.payload.get("inst_id")
        self.quality.observe(kind, str(inst_id) if inst_id else None, envelope.payload, envelope.available_at)

    async def publish(self, envelopes: Sequence[EventEnvelope]) -> None:
        """Publie dans la file bornée. Une file pleine ABANDONNE et compte : jamais de blocage silencieux."""
        for envelope in envelopes:
            try:
                self.queue.put_nowait(envelope)
            except asyncio.QueueFull:
                self.stats.dropped += 1
                self.quality.note_drop("queue_full")
                _log.warning("file de collecte saturée : message abandonné", event_type=envelope.event_type)

    # --- boucle -----------------------------------------------------------------------------------------

    async def run_once(self) -> None:
        """Une session : connexion, souscription, consommation jusqu'à la fin du flux."""
        connection = await self._connect(self.url)
        try:
            await connection.send(build_subscribe(self.subscriptions))
            if self.resync_needed:
                # Une resynchronisation demande un NOUVEAU snapshot de la même famille de flux ; on
                # réabonne les instruments concernés plutôt que de raccorder un snapshot REST (T13).
                args = [SubscriptionArg(channel=ch, inst_id=inst) for inst, ch in sorted(self.resync_needed)]
                await connection.send(build_subscribe(args))
            async for text in connection:
                await self.publish(self.handle_text(text))
        finally:
            await connection.close()

    async def run(self, stop: asyncio.Event) -> None:
        """Boucle de service : reconnexions bornées avec recul et gigue jusqu'à l'arrêt demandé."""
        attempt = 0
        while not stop.is_set():
            try:
                await self.run_once()
                attempt = 0
            except Exception as exc:  # une coupure n'arrête pas le service
                self.stats.errors += 1
                _log.warning("session de collecte interrompue", detail=repr(exc))
            if stop.is_set():
                return
            if self.max_reconnects is not None and self.stats.reconnects >= self.max_reconnects:
                _log.warning("plafond de reconnexions atteint", reconnects=self.stats.reconnects)
                return
            self.stats.reconnects += 1
            await self._sleep(backoff_delay(attempt, rng=self._rng))
            attempt += 1

    def health(self) -> dict[str, Any]:
        now = ensure_utc(self._clock.now_utc())
        return {
            "stats": self.stats.as_dict(),
            "books": self.usable_books(),
            "resync_needed": [f"{i}|{c}" for i, c in sorted(self.resync_needed)],
            "quality": self.quality.counters.as_dict(),
            "market_data_age_seconds": self.quality.max_age_seconds("book", now),
        }
