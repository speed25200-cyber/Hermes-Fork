"""Outbox transactionnelle (§41, D7).

- L'événement est écrit dans la MÊME transaction que l'écriture métier (via ``UnitOfWork.outbox``) ;
- un worker le réclame avec un bail durable (``claimed_by``, ``claimed_until``, ``fencing_token``) ;
- un bail expiré est relivré à un autre worker ; la clôture est conditionnelle au bail (fencing) ;
- l'idempotence repose sur ``idempotency_key`` (unique) et sur les handlers, qui doivent tolérer une
  relivraison : la livraison est « au moins une fois », jamais « exactement une fois » à travers le réseau.
- ``consume`` avance ``consumer_offsets`` vers l'avant uniquement : une relivraison est ignorée.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from okxq.domain.clocks import Clock
from okxq.persistence.models import OutboxEvent
from okxq.persistence.repositories import UnitOfWork

__all__ = ["ClaimedEvent", "OutboxWorker", "consume_with_offset"]


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    """Vue immuable d'un événement réclamé ; sert de jeton pour ``complete``/``fail``."""

    event_id: int
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str
    attempts: int
    fencing_token: int
    claimed_until: datetime

    @classmethod
    def from_row(cls, row: OutboxEvent) -> ClaimedEvent:
        assert row.claimed_until is not None and row.fencing_token is not None
        return cls(
            event_id=row.id,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            event_type=row.event_type,
            payload=dict(row.payload),
            idempotency_key=row.idempotency_key,
            attempts=row.attempts,
            fencing_token=row.fencing_token,
            claimed_until=row.claimed_until,
        )


class OutboxWorker:
    """Réclamation et clôture fencées d'événements d'outbox pour un worker identifié."""

    def __init__(self, *, worker_id: str, clock: Clock, lease_seconds: float, max_attempts: int = 5) -> None:
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("lease_seconds et max_attempts doivent être positifs")
        self.worker_id = worker_id
        self._clock = clock
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def claim(
        self, uow: UnitOfWork, *, fencing_token: int, limit: int = 10, event_types: set[str] | None = None
    ) -> list[ClaimedEvent]:
        """Réclame jusqu'à ``limit`` événements PENDING ou au bail expiré, un par un, de façon atomique."""
        now = self._clock.now_utc()
        claimed: list[ClaimedEvent] = []
        for row in uow.outbox.claimable(now=now, limit=limit, event_types=event_types):
            if row.attempts >= self.max_attempts:
                if uow.outbox.try_claim(
                    row.id,
                    worker_id=self.worker_id,
                    fencing_token=fencing_token,
                    now=now,
                    lease_seconds=self.lease_seconds,
                ):
                    uow.outbox.fail(
                        row.id,
                        worker_id=self.worker_id,
                        fencing_token=fencing_token,
                        error="tentatives épuisées",
                        dead=True,
                    )
                continue
            if uow.outbox.try_claim(
                row.id,
                worker_id=self.worker_id,
                fencing_token=fencing_token,
                now=now,
                lease_seconds=self.lease_seconds,
            ):
                uow.session.refresh(row)
                claimed.append(ClaimedEvent.from_row(row))
        return claimed

    def complete(self, uow: UnitOfWork, event: ClaimedEvent) -> bool:
        return uow.outbox.complete(
            event.event_id,
            worker_id=self.worker_id,
            fencing_token=event.fencing_token,
            now=self._clock.now_utc(),
        )

    def fail(self, uow: UnitOfWork, event: ClaimedEvent, *, error: str, dead: bool = False) -> bool:
        return uow.outbox.fail(
            event.event_id,
            worker_id=self.worker_id,
            fencing_token=event.fencing_token,
            error=error,
            dead=dead,
        )


def consume_with_offset(
    uow: UnitOfWork,
    *,
    consumer_name: str,
    handler: Callable[[OutboxEvent], None],
    now: datetime,
    limit: int = 100,
    statuses: set[str] | None = None,
) -> int:
    """Consommation idempotente d'un flux d'événements (lecture) ordonné par ``id``.

    Le handler est appelé une fois par événement d'identifiant strictement supérieur à l'offset ; l'offset
    n'avance que vers l'avant. Retourne le nombre d'événements traités.
    """
    last = uow.offsets.get(consumer_name)
    count = 0
    for row in uow.outbox.after(last, limit=limit, statuses=statuses):
        handler(row)
        if uow.offsets.advance(consumer_name, row.id, now=now):
            count += 1
    return count
