"""Qualité des données : MISSING, STALE, INVALID et zéro observé sont QUATRE choses différentes (§35).

Un zéro observé est une information ; une absence n'en est pas une. Confondre les deux, c'est apprendre
sur une donnée inventée. Ce module porte donc, pour chaque donnée suivie : son drapeau, son âge, son
motif d'invalidité éventuel, et une politique de forward-fill **bornée par type** — jamais de backward
fill, qui ferait entrer une observation future dans le passé.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from okxq.domain.clocks import ensure_utc
from okxq.domain.events import QualityFlag

__all__ = ["DataQualityTracker", "QualityCounters", "QualityReading"]

#: Durées maximales de forward-fill par type de donnée, en millisecondes (défaut de `market_data`).
DEFAULT_FORWARD_FILL_MS: dict[str, int] = {
    "book": 2_000,
    "trade": 60_000,
    "candle": 120_000,
    "mark_price": 10_000,
    "index_price": 30_000,
    "funding": 3_600_000,
    "open_interest": 300_000,
    "ticker": 60_000,
    "instrument": 86_400_000,
}


@dataclass(frozen=True, slots=True)
class QualityReading:
    """Ce qu'on sait d'une donnée à un instant : son drapeau, son âge, et pourquoi."""

    key: str
    flag: QualityFlag
    age_seconds: float | None
    observed_at: datetime | None
    reason: str | None = None
    value: Any = None

    @property
    def usable(self) -> bool:
        return self.flag is QualityFlag.OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "flag": self.flag.value,
            "age_seconds": self.age_seconds,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "reason": self.reason,
        }


@dataclass(slots=True)
class QualityCounters:
    """Compteurs exposés en métriques. Une perte non comptée est une perte cachée (§65)."""

    gaps: int = 0
    invalid: int = 0
    drops: int = 0
    stale_reads: int = 0
    missing_reads: int = 0
    forward_filled: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "gaps": self.gaps,
            "invalid": self.invalid,
            "drops": self.drops,
            "stale_reads": self.stale_reads,
            "missing_reads": self.missing_reads,
            "forward_filled": self.forward_filled,
            "by_reason": dict(sorted(self.by_reason.items())),
        }


@dataclass(slots=True)
class _Entry:
    value: Any
    observed_at: datetime
    kind: str
    invalid_reason: str | None = None


class DataQualityTracker:
    """Suit la fraîcheur et la validité des données par clé ``(kind, inst_id)``.

    ``max_age_ms`` est la tolérance de DÉCISION (au-delà : STALE) ; ``forward_fill_ms`` est la durée
    maximale pendant laquelle une valeur reste réutilisable telle quelle (au-delà : MISSING).
    """

    def __init__(
        self,
        *,
        max_age_ms: int = 2_000,
        forward_fill_ms: Mapping[str, int] | None = None,
    ) -> None:
        self.max_age = timedelta(milliseconds=max_age_ms)
        self.forward_fill = dict(DEFAULT_FORWARD_FILL_MS)
        if forward_fill_ms:
            self.forward_fill.update(forward_fill_ms)
        self.counters = QualityCounters()
        self._entries: dict[str, _Entry] = {}

    @staticmethod
    def key_for(kind: str, inst_id: str | None = None) -> str:
        return f"{kind}:{inst_id}" if inst_id else kind

    # --- écriture ---------------------------------------------------------------------------------------

    def observe(self, kind: str, inst_id: str | None, value: Any, observed_at: datetime) -> None:
        """Enregistre une observation VALIDE (y compris un zéro réellement observé)."""
        self._entries[self.key_for(kind, inst_id)] = _Entry(
            value=value, observed_at=ensure_utc(observed_at), kind=kind
        )

    def mark_invalid(self, kind: str, inst_id: str | None, reason: str, at: datetime) -> None:
        """Marque une donnée INVALIDE. Elle n'est plus utilisable, même si une ancienne valeur existe."""
        key = self.key_for(kind, inst_id)
        previous = self._entries.get(key)
        self._entries[key] = _Entry(
            value=None if previous is None else previous.value,
            observed_at=ensure_utc(at),
            kind=kind,
            invalid_reason=reason,
        )
        self.counters.invalid += 1
        self.counters.note(reason)

    def note_gap(self, reason: str = "sequence_gap") -> None:
        self.counters.gaps += 1
        self.counters.note(reason)

    def note_drop(self, reason: str = "queue_full") -> None:
        self.counters.drops += 1
        self.counters.note(reason)

    # --- lecture ----------------------------------------------------------------------------------------

    def read(self, kind: str, inst_id: str | None, now: datetime) -> QualityReading:
        """Lit une donnée à ``now`` avec son drapeau. Ne rend JAMAIS une valeur imputée silencieusement."""
        now = ensure_utc(now)
        key = self.key_for(kind, inst_id)
        entry = self._entries.get(key)
        if entry is None:
            self.counters.missing_reads += 1
            return QualityReading(key, QualityFlag.MISSING, None, None, "jamais observé")
        age = (now - entry.observed_at).total_seconds()
        if entry.invalid_reason is not None:
            return QualityReading(key, QualityFlag.INVALID, age, entry.observed_at, entry.invalid_reason)
        limit_ms = self.forward_fill.get(kind, self.forward_fill.get("book", 2_000))
        if age * 1000 > limit_ms:
            self.counters.missing_reads += 1
            return QualityReading(
                key,
                QualityFlag.MISSING,
                age,
                entry.observed_at,
                f"forward-fill épuisé ({age * 1000:.0f} ms > {limit_ms} ms)",
            )
        if age > self.max_age.total_seconds():
            self.counters.stale_reads += 1
            return QualityReading(
                key,
                QualityFlag.STALE,
                age,
                entry.observed_at,
                f"plus ancien que la tolérance de décision ({age * 1000:.0f} ms)",
                value=entry.value,
            )
        if age > 0:
            self.counters.forward_filled += 1
        return QualityReading(key, QualityFlag.OK, age, entry.observed_at, None, value=entry.value)

    def flags_for(self, inst_id: str, kinds: tuple[str, ...], now: datetime) -> list[QualityFlag]:
        return [self.read(kind, inst_id, now).flag for kind in kinds]

    def report(self, now: datetime) -> dict[str, Any]:
        now = ensure_utc(now)
        readings = []
        for key in sorted(self._entries):
            kind, _, inst = key.partition(":")
            readings.append(self.read(kind, inst or None, now).as_dict())
        return {"counters": self.counters.as_dict(), "readings": readings}

    def max_age_seconds(self, kind: str, now: datetime) -> float | None:
        """Âge de la donnée la plus ANCIENNE d'un type (métrique ``market_data_age_seconds``)."""
        now = ensure_utc(now)
        ages = [
            (now - entry.observed_at).total_seconds()
            for key, entry in self._entries.items()
            if key.split(":", 1)[0] == kind
        ]
        return max(ages) if ages else None
