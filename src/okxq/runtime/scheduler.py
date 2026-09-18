"""Ordonnanceur de décisions aligné sur les frontières UTC de 60 secondes (§55).

Propriétés :
- une frontière manquée ne déclenche jamais une rafale de rattrapage : la prochaine frontière est
  recalculée depuis « maintenant » ;
- une décision en cours ne se superpose pas à la suivante : politique ``skip`` (la frontière est
  journalisée comme sautée) ou ``coalesce`` (la frontière suivante attend la fin, puis une seule
  décision est prise) ;
- chaque décision reçoit une deadline explicite (``boundary + budget``) ; un dépassement est compté,
  et la boucle de risque, indépendante, n'est jamais bloquée par cette boucle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from okxq.domain.clocks import Clock, next_boundary
from okxq.domain.reasons import ReasonCode

DecisionCallback = Callable[[datetime, datetime], Awaitable[object]]


@dataclass(slots=True)
class SchedulerStats:
    boundaries_seen: int = 0
    decisions_started: int = 0
    decisions_completed: int = 0
    decisions_failed: int = 0
    skipped_overlap: int = 0
    coalesced: int = 0
    deadline_misses: int = 0
    missed_boundaries: int = 0
    last_boundary: datetime | None = None
    last_duration_ms: int | None = None
    events: list[dict[str, object]] = field(default_factory=list)

    def record(self, kind: str, **details: object) -> None:
        self.events.append({"kind": kind, **details})
        if len(self.events) > 1000:
            del self.events[: len(self.events) - 1000]


class DecisionScheduler:
    def __init__(
        self,
        *,
        clock: Clock,
        interval_seconds: int,
        budget_ms: int,
        overlap_policy: Literal["skip", "coalesce"],
        on_boundary: DecisionCallback,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if interval_seconds <= 0 or budget_ms <= 0:
            raise ValueError("intervalle et budget doivent être positifs")
        self._clock = clock
        self._interval = interval_seconds
        self._budget = timedelta(milliseconds=budget_ms)
        self._policy = overlap_policy
        self._callback = on_boundary
        self._sleep = sleep or asyncio.sleep
        self._running: asyncio.Task[object] | None = None
        self._pending_coalesced: datetime | None = None
        self.stats = SchedulerStats()

    # --- pilotage explicite (tests, backtest) -------------------------------------------------------

    async def fire(self, boundary: datetime) -> bool:
        """Déclenche la décision de ``boundary`` selon la politique de superposition. Retourne True si lancée."""
        self.stats.boundaries_seen += 1
        self.stats.last_boundary = boundary
        if self._running is not None and not self._running.done():
            if self._policy == "skip":
                self.stats.skipped_overlap += 1
                self.stats.record(
                    "skipped", boundary=boundary.isoformat(), reason=ReasonCode.DECISION_SKIPPED_OVERLAP
                )
                return False
            self.stats.coalesced += 1
            self._pending_coalesced = boundary
            self.stats.record("coalesced", boundary=boundary.isoformat())
            return False
        self._running = asyncio.ensure_future(self._run_one(boundary))
        return True

    async def _run_one(self, boundary: datetime) -> object:
        deadline = boundary + self._budget
        started = self._clock.now_utc()
        self.stats.decisions_started += 1
        try:
            result = await self._callback(boundary, deadline)
            self.stats.decisions_completed += 1
            return result
        except Exception as exc:  # la décision échoue, la boucle survit et journalise
            self.stats.decisions_failed += 1
            self.stats.record("failed", boundary=boundary.isoformat(), error=repr(exc))
            return None
        finally:
            finished = self._clock.now_utc()
            self.stats.last_duration_ms = int((finished - started).total_seconds() * 1000)
            if finished > deadline:
                self.stats.deadline_misses += 1
                self.stats.record(
                    "deadline_miss",
                    boundary=boundary.isoformat(),
                    late_ms=int((finished - deadline).total_seconds() * 1000),
                )
            if self._pending_coalesced is not None and self._policy == "coalesce":
                pending, self._pending_coalesced = self._pending_coalesced, None
                # Une seule décision de rattrapage, sur la DERNIÈRE frontière demandée, jamais une rafale.
                self._running = asyncio.ensure_future(self._run_one(pending))

    async def wait_idle(self) -> None:
        while self._running is not None and not self._running.done():
            await self._running
            # une tâche coalescée peut avoir été relancée dans le finally

    # --- boucle temps réel ---------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        expected = next_boundary(self._clock.now_utc(), self._interval)
        while not stop.is_set():
            now = self._clock.now_utc()
            if now >= expected + timedelta(seconds=self._interval):
                # Le processus a dormi au-delà d'une frontière entière : on compte les minutes manquées
                # et l'on repart de la prochaine frontière, sans rattrapage.
                missed = int((now - expected).total_seconds() // self._interval)
                self.stats.missed_boundaries += missed
                self.stats.record("missed", count=missed, resumed_from=now.isoformat())
                expected = next_boundary(now, self._interval)
                continue
            wait_s = (expected - now).total_seconds()
            if wait_s > 0:
                await self._sleep(min(wait_s, 1.0))
                continue
            await self.fire(expected)
            expected = expected + timedelta(seconds=self._interval)
        await self.wait_idle()
