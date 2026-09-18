"""Santé des composants : vivacité ≠ aptitude (§61).

``liveness()`` signifie « le processus répond ». ``readiness()`` signifie « ce processus est réellement
prêt pour sa fonction » : un gateway connecté mais **non réconcilié** n'est pas prêt, et un flux muet
depuis plus longtemps que sa tolérance devient FAULT même si la connexion tient. Les deux réponses ne
sont jamais confondues : c'est ce qui empêche un orchestrateur de router du trafic vers un processus qui
ne sait pas encore ce qu'il possède.

``health_tick()`` rend exactement la forme attendue par l'interface conservée (canal ``health-tick`` :
``{"modules": {nom: {"status", "info"}}}``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from okxq.domain.clocks import Clock, ensure_utc

# Noms de composants stables (utilisés par l'interface, les métriques et les runbooks).
MARKET_DATA = "market_data"
PRIVATE_STREAM = "private_stream"
DATABASE = "database"
STRATEGY = "strategy"
RISK = "risk"
GATEWAY = "gateway"
RECONCILIATION = "reconciliation"
JEV = "jev"
CLOCK = "clock"
OUTBOX = "outbox"

#: Composants dont l'état conditionne l'aptitude par défaut lorsqu'ils sont déclarés.
DEFAULT_REQUIRED: frozenset[str] = frozenset(
    {MARKET_DATA, PRIVATE_STREAM, DATABASE, STRATEGY, RISK, GATEWAY, RECONCILIATION, CLOCK, OUTBOX}
)


class Status(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAULT = "FAULT"

    @classmethod
    def parse(cls, value: Status | str) -> Status:
        if isinstance(value, Status):
            return value
        try:
            return cls(str(value).upper())
        except ValueError as exc:
            raise ValueError(f"statut de santé inconnu : {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Component:
    name: str
    status: Status
    info: str
    updated_at: datetime
    required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "info": self.info,
            "updated_at": self.updated_at.isoformat(),
            "required": self.required,
        }


class HealthRegistry:
    """Registre en mémoire d'un processus. Chaque composant porte son âge : un état sans fraîcheur mentirait."""

    def __init__(
        self,
        *,
        clock: Clock,
        mode: str,
        stale_after_seconds: float | None = None,
        required: frozenset[str] = DEFAULT_REQUIRED,
    ) -> None:
        self._clock = clock
        self.mode = mode
        self._stale_after = stale_after_seconds
        self._required_names = required
        self._components: dict[str, Component] = {}

    # --- écriture ---------------------------------------------------------------------------------------

    def set(self, name: str, status: Status | str, info: str = "", *, required: bool | None = None) -> None:
        parsed = Status.parse(status)
        is_required = self._required_names.__contains__(name) if required is None else required
        self._components[name] = Component(
            name=name,
            status=parsed,
            info=info,
            updated_at=ensure_utc(self._clock.now_utc()),
            required=is_required,
        )

    def touch(self, name: str) -> None:
        """Rafraîchit l'horodatage sans changer le statut (le flux a parlé)."""
        existing = self._components.get(name)
        if existing is None:
            self.set(name, Status.OK)
            return
        self._components[name] = Component(
            name=existing.name,
            status=existing.status,
            info=existing.info,
            updated_at=ensure_utc(self._clock.now_utc()),
            required=existing.required,
        )

    def set_gateway(self, *, connected: bool, reconciled: bool, info: str = "") -> None:
        """Un gateway connecté mais non réconcilié est WARN : il n'est PAS prêt (§61)."""
        if not connected:
            self.set(GATEWAY, Status.FAULT, info or "flux privé non connecté")
        elif not reconciled:
            self.set(GATEWAY, Status.WARN, info or "connecté mais réconciliation non terminée")
        else:
            self.set(GATEWAY, Status.OK, info or "connecté et réconcilié")

    def remove(self, name: str) -> None:
        self._components.pop(name, None)

    # --- lecture ----------------------------------------------------------------------------------------

    def _effective(self, component: Component, now: datetime) -> Component:
        if self._stale_after is None:
            return component
        age = (now - component.updated_at).total_seconds()
        if age > self._stale_after and component.status is not Status.FAULT:
            return Component(
                name=component.name,
                status=Status.FAULT,
                info=f"muet depuis {age:.0f} s (tolérance {self._stale_after:.0f} s)",
                updated_at=component.updated_at,
                required=component.required,
            )
        return component

    def components(self) -> dict[str, Component]:
        now = ensure_utc(self._clock.now_utc())
        return {name: self._effective(c, now) for name, c in self._components.items()}

    def liveness(self) -> dict[str, Any]:
        return {
            "status": "alive",
            "mode": self.mode,
            "ts": ensure_utc(self._clock.now_utc()).isoformat(),
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        components = self.components()
        reasons: list[str] = []
        for name, component in sorted(components.items()):
            if component.required and component.status is not Status.OK:
                reasons.append(f"{name} : {component.status.value} {component.info}".strip())
        detail: dict[str, Any] = {
            "status": "ready" if not reasons else "not_ready",
            "mode": self.mode,
            "ts": ensure_utc(self._clock.now_utc()).isoformat(),
            "components": {name: c.to_dict() for name, c in sorted(components.items())},
            "reasons": reasons,
        }
        return (not reasons), detail

    def health_tick(self) -> dict[str, Any]:
        """Forme attendue par l'interface conservée (canal ``health-tick``)."""
        return {
            "modules": {
                name: {"status": c.status.value, "info": c.info}
                for name, c in sorted(self.components().items())
            },
            "mode": self.mode,
            "ts": ensure_utc(self._clock.now_utc()).isoformat(),
        }
