"""Séquence de démarrage opérationnel (§52.4) et procédure d'arrêt (§62).

``configs → environnement → leadership → flux privés → bootstrap/réconciliation → protections →
validation des données → autorisation éventuelle de nouvelles intentions``. Aucun ordre d'entrée ne part
avant la fin ; les halts et drawdowns persistés ne sont jamais remis à zéro.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from okxq.domain.clocks import Clock

StepFn = Callable[[], Awaitable["StepResult"]]

STEP_ORDER: tuple[str, ...] = (
    "load_config",
    "verify_environment",
    "acquire_leadership",
    "connect_private_streams",
    "bootstrap_reconcile",
    "restore_protections",
    "validate_data",
    "authorize_entries",
)


@dataclass(frozen=True, slots=True)
class StepResult:
    ok: bool
    detail: str = ""
    blocking: bool = True


@dataclass(slots=True)
class StartupReport:
    started_at: datetime
    finished_at: datetime | None = None
    steps: list[dict[str, object]] = field(default_factory=list)
    entries_authorized: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "steps": list(self.steps),
            "entries_authorized": self.entries_authorized,
        }


class StartupSequence:
    def __init__(self, clock: Clock, steps: dict[str, StepFn]) -> None:
        missing = [s for s in STEP_ORDER if s not in steps]
        if missing:
            raise ValueError(f"étapes de démarrage manquantes : {missing}")
        unknown = [s for s in steps if s not in STEP_ORDER]
        if unknown:
            raise ValueError(f"étapes inconnues : {unknown}")
        self._clock = clock
        self._steps = steps

    async def run(self) -> StartupReport:
        report = StartupReport(started_at=self._clock.now_utc())
        for name in STEP_ORDER:
            t0 = self._clock.now_utc()
            try:
                result = await self._steps[name]()
            except Exception as exc:  # une exception est un échec bloquant, jamais un succès implicite
                result = StepResult(ok=False, detail=f"exception : {exc!r}", blocking=True)
            report.steps.append(
                {
                    "step": name,
                    "ok": result.ok,
                    "detail": result.detail,
                    "duration_ms": int((self._clock.now_utc() - t0).total_seconds() * 1000),
                }
            )
            if not result.ok and result.blocking:
                report.finished_at = self._clock.now_utc()
                report.entries_authorized = False
                return report
        report.finished_at = self._clock.now_utc()
        report.entries_authorized = all(bool(s["ok"]) for s in report.steps)
        return report


SHUTDOWN_ORDER: tuple[str, ...] = (
    "suspend_entries",
    "drain_pending_intents",
    "confirm_cancellations_and_protections",
    "write_checkpoint",
    "stop_non_critical",
)


class ShutdownSequence:
    """Arrêt contrôlé : conserver ou fermer les positions est une POLITIQUE explicite, jamais un effet de bord."""

    def __init__(self, clock: Clock, steps: dict[str, StepFn], *, position_policy: str) -> None:
        if position_policy not in ("keep_positions_supervised", "flatten_before_stop"):
            raise ValueError("politique de positions à l'arrêt inconnue")
        missing = [s for s in SHUTDOWN_ORDER if s not in steps]
        if missing:
            raise ValueError(f"étapes d'arrêt manquantes : {missing}")
        self._clock = clock
        self._steps = steps
        self.position_policy = position_policy

    async def run(self) -> StartupReport:
        report = StartupReport(started_at=self._clock.now_utc())
        for name in SHUTDOWN_ORDER:
            try:
                result = await self._steps[name]()
            except Exception as exc:
                result = StepResult(ok=False, detail=f"exception : {exc!r}", blocking=False)
            report.steps.append(
                {"step": name, "ok": result.ok, "detail": result.detail, "policy": self.position_policy}
            )
        report.finished_at = self._clock.now_utc()
        return report
