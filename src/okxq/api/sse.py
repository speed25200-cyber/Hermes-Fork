"""Bus d'événements en mémoire borné et flux SSE ``/api/flux`` de l'interface conservée.

Le format est celui que ``pont.js`` attend : une ligne ``data: {"canal": ..., "charge": ...}`` par
événement. Deux canaux sont produits ici :

- ``health-tick`` toutes les 4 s, depuis ``okxq.runtime.health`` ;
- ``ai-log`` : le journal des décisions, du risque, de la qualité des données et des actions opérateur,
  alimenté par le runtime dans le même processus (``EventBus.publish``) ou, pour le service API qui
  vit dans son propre conteneur, par ``JournalPoller`` qui relit la base à chaque battement.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from starlette.requests import Request

from okxq.api.readmodel import ReadModel
from okxq.api.serialize import as_iso, to_jsonable
from okxq.runtime.health import HealthRegistry

HEALTH_TICK_SECONDS = 4.0
HEARTBEAT_SECONDS = 20.0


class EventBus:
    """File bornée (les plus anciens sortent) + diffusion vers les flux SSE ouverts."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._events: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=maxlen)
        self._seq = 0
        self._subscribers: dict[int, tuple[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop]] = {}
        self._next_sub = 0
        self._lock = threading.Lock()

    def publish(self, canal: str, charge: dict[str, Any]) -> int:
        payload = to_jsonable(charge)
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._events.append((seq, canal, payload))
            targets = list(self._subscribers.values())
        message = {"canal": canal, "charge": payload, "seq": seq}
        for queue, loop in targets:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, message)
            except RuntimeError:  # boucle fermée : l'abonné sera retiré à sa prochaine lecture
                continue
        return seq

    def recent(self, canal: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            items = [c for _, k, c in self._events if k == canal]
        return items[-limit:]

    def subscribe(self) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._next_sub += 1
            sid = self._next_sub
            self._subscribers[sid] = (queue, loop)
        return sid, queue

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subscribers.pop(sid, None)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def format_sse(canal: str, charge: Any) -> bytes:
    return f"data: {json.dumps({'canal': canal, 'charge': charge}, ensure_ascii=False)}\n\n".encode()


async def sse_stream(
    request: Request,
    bus: EventBus,
    health: HealthRegistry,
    *,
    tick_seconds: float = HEALTH_TICK_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    max_events: int | None = None,
) -> AsyncIterator[bytes]:
    """Générateur SSE : commentaire d'ouverture, un ``health-tick`` immédiat, puis le bus."""
    sid, queue = bus.subscribe()
    sent = 0
    try:
        yield b": ouvert\n\n"
        yield format_sse("health-tick", health.health_tick())
        sent += 1
        loop = asyncio.get_running_loop()
        last_tick = loop.time()
        last_beat = loop.time()
        while max_events is None or sent < max_events:
            if await request.is_disconnected():
                break
            now = loop.time()
            wait = max(0.05, min(tick_seconds - (now - last_tick), heartbeat_seconds - (now - last_beat)))
            try:
                message = await asyncio.wait_for(queue.get(), timeout=wait)
            except TimeoutError:
                message = None
            now = loop.time()
            if message is not None:
                yield format_sse(str(message["canal"]), message["charge"])
                sent += 1
                if message["canal"] == "health-tick":
                    last_tick = now
                continue
            if now - last_tick >= tick_seconds:
                yield format_sse("health-tick", health.health_tick())
                sent += 1
                last_tick = now
            if now - last_beat >= heartbeat_seconds:
                yield b": .\n\n"
                last_beat = now
    finally:
        bus.unsubscribe(sid)


class JournalPoller:
    """Relit la base et publie sur ``ai-log`` ce qui est nouveau depuis la dernière lecture."""

    def __init__(self, readmodel: ReadModel, bus: EventBus, *, prime_limit: int = 60) -> None:
        self._rm = readmodel
        self._bus = bus
        self._prime_limit = prime_limit
        self._seen: set[str] = set()
        self._since: datetime | None = None

    def _emit(self, entry: dict[str, Any]) -> None:
        key = f"{entry.get('event')}|{entry.get('id')}"
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[-2500:])
        self._bus.publish("ai-log", entry)

    def poll(self) -> int:
        """Retourne le nombre d'entrées publiées ; jamais d'exception vers le battement."""
        try:
            entries = self._rm.journal_entries(since=self._since, limit=self._prime_limit)
        except Exception as exc:
            self._bus.publish(
                "ai-log",
                {"ts": as_iso(self._rm.now()), "event": "JOURNAL_DB_ERROR", "error": type(exc).__name__},
            )
            return 0
        count = 0
        for entry in entries:
            self._emit(entry)
            count += 1
        if entries:
            latest = max(str(e.get("ts") or "") for e in entries)
            try:
                self._since = datetime.fromisoformat(latest)
            except ValueError:
                pass
        return count
