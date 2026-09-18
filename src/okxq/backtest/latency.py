"""Modèle de latence déterministe (§33) : base + jitter seedé, par nature d'opération.

Le jitter est dérivé de ``(seed, kind, key)`` par un générateur seedé sur une chaîne (SHA-512 en
interne) : il ne dépend ni de l'ordre des appels ni du processus, ce qui rend le replay reproductible (T70).
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from okxq.domain.errors import ConfigError

__all__ = ["LatencyKind", "LatencyModel"]


class LatencyKind(StrEnum):
    ORDER = "order"  # trajet ordre → exchange
    CANCEL = "cancel"  # trajet annulation → exchange
    ACK = "ack"  # exchange → nous (accusé de réception)
    EVENT = "event"  # exchange → nous (fill, position, solde)


_DEFAULT_BASE_MS: dict[LatencyKind, int] = {
    LatencyKind.ORDER: 60,
    LatencyKind.CANCEL: 60,
    LatencyKind.ACK: 25,
    LatencyKind.EVENT: 25,
}


@dataclass(frozen=True, slots=True)
class LatencyModel:
    base_ms: Mapping[LatencyKind, int] = field(default_factory=lambda: dict(_DEFAULT_BASE_MS))
    jitter_ms: int = 15
    seed: int = 25200

    def __post_init__(self) -> None:
        if self.jitter_ms < 0:
            raise ConfigError("jitter_ms négatif")
        for kind in LatencyKind:
            if kind not in self.base_ms:
                raise ConfigError(f"latence de base manquante pour {kind.value}")
            if self.base_ms[kind] < 0:
                raise ConfigError(f"latence de base négative pour {kind.value}")

    def delay(self, kind: LatencyKind, key: str) -> timedelta:
        """Délai déterministe pour une opération identifiée par ``key`` (ex. client_order_id)."""
        base = self.base_ms[kind]
        if self.jitter_ms == 0:
            return timedelta(milliseconds=base)
        rng = random.Random(f"{self.seed}:{kind.value}:{key}")
        return timedelta(milliseconds=base + rng.randint(0, self.jitter_ms))

    @classmethod
    def zero(cls) -> LatencyModel:
        """Latence nulle (tests unitaires de matching pur)."""
        return cls(base_ms=dict.fromkeys(LatencyKind, 0), jitter_ms=0)
