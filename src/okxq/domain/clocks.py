"""Horloges (§34).

Deux horloges distinctes : ``now_utc()`` pour les instants (timezone-aware, UTC) et ``monotonic_ns()``
pour les durées. Une horloge monotone ne se compare ni entre machines ni entre redémarrages ; les
identifiants ``HOST_ID`` et ``BOOT_ID`` accompagnent donc tout enregistrement qui en dépend.
"""

from __future__ import annotations

import os
import secrets
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from okxq.domain.errors import TimestampError

HOST_ID: str = os.environ.get("OKXQ_HOST_ID") or socket.gethostname()
BOOT_ID: str = secrets.token_hex(8)


def ensure_utc(dt: datetime, *, field: str = "timestamp") -> datetime:
    """Refuse une date naïve ; normalise en UTC une date timezone-aware."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise TimestampError(f"{field} doit être timezone-aware (UTC)", field=field)
    return dt.astimezone(UTC)


def floor_to_interval(dt: datetime, seconds: int) -> datetime:
    """Frontière UTC précédente d'un intervalle en secondes (par défaut 60 s pour la boucle)."""
    dt = ensure_utc(dt)
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)


def next_boundary(dt: datetime, seconds: int) -> datetime:
    return floor_to_interval(dt, seconds) + timedelta(seconds=seconds)


def utc_day(dt: datetime) -> datetime:
    """Frontière journalière UTC déclarée (§38) : 00:00:00 UTC du jour de ``dt``."""
    dt = ensure_utc(dt)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


@runtime_checkable
class Clock(Protocol):
    def now_utc(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


class SystemClock:
    """Horloge opérationnelle."""

    def now_utc(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class SimulatedClock:
    """Horloge pilotée par le replay/backtest : elle n'avance que lorsqu'on le lui demande."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start, field="start")
        self._mono = 0

    def now_utc(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        return self._mono

    def set(self, dt: datetime) -> None:
        dt = ensure_utc(dt)
        if dt < self._now:
            raise TimestampError("une horloge simulée ne recule pas", requested=dt.isoformat())
        delta_ns = int((dt - self._now).total_seconds() * 1_000_000_000)
        self._mono += delta_ns
        self._now = dt

    def advance(self, delta: timedelta) -> None:
        self.set(self._now + delta)

    def advance_seconds(self, seconds: float) -> None:
        self.advance(timedelta(seconds=seconds))
