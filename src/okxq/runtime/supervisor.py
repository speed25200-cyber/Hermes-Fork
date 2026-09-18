"""Superviseur : actions opérateur auditées, watchdog, santé agrégée (§58, §61, §65).

Les actions opérateur (pause, annulation des entrées, demande de flatten, reprise) arrivent de l'API
comme des enregistrements ``REQUESTED`` ; le superviseur les exécute via des gestionnaires injectés et
consigne le résultat observé. Une requête acceptée ne signifie pas une reprise effectuée.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from okxq.domain.clocks import Clock

ActionHandler = Callable[["OperatorRequest"], Awaitable[dict[str, Any]]]

SUPPORTED_ACTIONS: frozenset[str] = frozenset(
    {"pause", "cancel_entry_orders", "request_flatten", "request_resume"}
)


@dataclass(frozen=True, slots=True)
class OperatorRequest:
    request_id: str
    action: str
    scope: str
    reason: str
    actor: str
    role: str
    requested_at: datetime


@dataclass(slots=True)
class ActionOutcome:
    request_id: str
    status: str  # APPLIED / REFUSED / FAILED / PENDING
    observed_result: dict[str, Any] = field(default_factory=dict)
    completed_at: datetime | None = None


class Supervisor:
    def __init__(
        self,
        *,
        clock: Clock,
        handlers: dict[str, ActionHandler],
        allowed_roles: dict[str, frozenset[str]] | None = None,
    ) -> None:
        unknown = set(handlers) - SUPPORTED_ACTIONS
        if unknown:
            raise ValueError(f"actions non supportées : {sorted(unknown)}")
        self._clock = clock
        self._handlers = handlers
        self._roles = allowed_roles or {
            "pause": frozenset({"operator", "admin"}),
            "cancel_entry_orders": frozenset({"operator", "admin"}),
            "request_flatten": frozenset({"operator", "admin"}),
            "request_resume": frozenset({"operator", "admin"}),
        }
        self.history: list[ActionOutcome] = []

    async def process(self, request: OperatorRequest) -> ActionOutcome:
        if request.action not in SUPPORTED_ACTIONS or request.action not in self._handlers:
            outcome = ActionOutcome(
                request.request_id, "REFUSED", {"error": "action inconnue"}, self._clock.now_utc()
            )
        elif request.role not in self._roles.get(request.action, frozenset()):
            outcome = ActionOutcome(
                request.request_id, "REFUSED", {"error": "rôle insuffisant"}, self._clock.now_utc()
            )
        elif not request.reason.strip():
            outcome = ActionOutcome(
                request.request_id, "REFUSED", {"error": "raison obligatoire"}, self._clock.now_utc()
            )
        else:
            try:
                observed = await self._handlers[request.action](request)
                status = str(observed.get("status", "APPLIED"))
                outcome = ActionOutcome(request.request_id, status, observed, self._clock.now_utc())
            except Exception as exc:  # l'échec est consigné, jamais masqué
                outcome = ActionOutcome(
                    request.request_id, "FAILED", {"error": repr(exc)}, self._clock.now_utc()
                )
        self.history.append(outcome)
        return outcome


@dataclass(slots=True)
class ComponentHealth:
    status: str  # OK / WARN / FAULT
    info: str = ""
    updated_at: datetime | None = None


class HealthAggregator:
    """Agrège l'état des composants ; readiness = tous les composants critiques OK."""

    def __init__(self, *, critical: frozenset[str]) -> None:
        self._critical = critical
        self._components: dict[str, ComponentHealth] = {}

    def set(self, name: str, status: str, info: str = "", *, at: datetime | None = None) -> None:
        if status not in ("OK", "WARN", "FAULT"):
            raise ValueError("statut de santé inconnu")
        self._components[name] = ComponentHealth(status, info, at)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            k: {
                "status": v.status,
                "info": v.info,
                "updated_at": v.updated_at.isoformat() if v.updated_at else None,
            }
            for k, v in self._components.items()
        }

    def ready(self) -> bool:
        return all(self._components.get(c, ComponentHealth("FAULT")).status == "OK" for c in self._critical)

    def live(self) -> bool:
        return True
