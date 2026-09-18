"""Limiteur de débit centralisé (§46) par portée effective : compte, IP, instrument, canal, famille.

Chaque règle définit une capacité par fenêtre glissante et des budgets RÉSERVÉS par classe (annulations,
protections, heartbeat, réconciliation). Les téléchargements (``DOWNLOAD``) et le trafic général ne
peuvent consommer que le reliquat non réservé : ils ne peuvent jamais épuiser les budgets vitaux. Une
classe réservée puise dans son budget propre puis, s'il est vide, dans le reliquat général.

Les valeurs par défaut reproduisent les limites documentées (docs-v5, 2026-09-18) ; elles sont
indicatives et bornées vers le bas — l'exchange reste l'arbitre (HTTP 429 / code 50011).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from okxq.domain.clocks import Clock
from okxq.domain.errors import ExchangeError

__all__ = ["DEFAULT_RULES", "BudgetClass", "LimitDecision", "LimitRule", "RateLimitedError", "RateLimiter"]


class RateLimitedError(ExchangeError):
    code = "RATE_LIMITED"


class BudgetClass(StrEnum):
    TRADE = "trade"
    CANCEL = "cancel"
    PROTECTION = "protection"
    HEARTBEAT = "heartbeat"
    RECONCILIATION = "reconciliation"
    DOWNLOAD = "download"
    GENERAL = "general"


RESERVED_CLASSES: frozenset[BudgetClass] = frozenset(
    {BudgetClass.CANCEL, BudgetClass.PROTECTION, BudgetClass.HEARTBEAT, BudgetClass.RECONCILIATION}
)


@dataclass(frozen=True, slots=True)
class LimitRule:
    family: str  # ex. "trade.order", "account.read", "ws.private"
    scope_kind: str  # account / ip / instrument / channel
    capacity: int
    window: timedelta
    reserved: Mapping[BudgetClass, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.window <= timedelta(0):
            raise ValueError("capacité et fenêtre doivent être positives")
        if any(cls not in RESERVED_CLASSES for cls in self.reserved):
            raise ValueError("seules les classes vitales peuvent avoir un budget réservé")
        if sum(self.reserved.values()) >= self.capacity:
            raise ValueError("les budgets réservés doivent laisser un reliquat général")

    @property
    def general_capacity(self) -> int:
        return self.capacity - sum(self.reserved.values())


@dataclass(frozen=True, slots=True)
class LimitDecision:
    allowed: bool
    retry_after: timedelta
    pool: str


class _Window:
    def __init__(self, capacity: int, window: timedelta) -> None:
        self.capacity = capacity
        self.window = window
        self.hits: deque[datetime] = deque()

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.window
        while self.hits and self.hits[0] <= cutoff:
            self.hits.popleft()

    def try_hit(self, now: datetime) -> timedelta | None:
        self._prune(now)
        if len(self.hits) < self.capacity:
            self.hits.append(now)
            return None
        return self.hits[0] + self.window - now


class RateLimiter:
    def __init__(self, rules: Iterable[LimitRule], *, clock: Clock) -> None:
        self._rules: dict[str, LimitRule] = {}
        for rule in rules:
            if rule.family in self._rules:
                raise ValueError(f"règle dupliquée : {rule.family}")
            self._rules[rule.family] = rule
        self._clock = clock
        self._windows: dict[tuple[str, str, str], _Window] = {}

    def rule(self, family: str) -> LimitRule:
        try:
            return self._rules[family]
        except KeyError as exc:
            raise ExchangeError("famille de limite inconnue", family=family) from exc

    def _window(self, rule: LimitRule, scope_id: str, pool: str) -> _Window:
        key = (rule.family, scope_id, pool)
        w = self._windows.get(key)
        if w is None:
            cap = rule.reserved.get(BudgetClass(pool), 0) if pool != "general" else rule.general_capacity
            w = _Window(max(cap, 0), rule.window)
            self._windows[key] = w
        return w

    def try_acquire(self, family: str, scope_id: str, budget: BudgetClass) -> LimitDecision:
        rule = self.rule(family)
        now = self._clock.now_utc()
        if budget in RESERVED_CLASSES and rule.reserved.get(budget, 0) > 0:
            wait = self._window(rule, scope_id, budget.value).try_hit(now)
            if wait is None:
                return LimitDecision(True, timedelta(0), budget.value)
        general_wait = self._window(rule, scope_id, "general").try_hit(now)
        if general_wait is None:
            return LimitDecision(True, timedelta(0), "general")
        return LimitDecision(False, general_wait, "general")

    async def acquire(
        self, family: str, scope_id: str, budget: BudgetClass, *, max_wait: timedelta = timedelta(seconds=2)
    ) -> None:
        """Attend (bornée) puis lève ``RateLimitedError`` si le budget reste épuisé."""
        waited = timedelta(0)
        while True:
            decision = self.try_acquire(family, scope_id, budget)
            if decision.allowed:
                return
            if waited + decision.retry_after > max_wait:
                raise RateLimitedError(
                    "budget de débit épuisé", family=family, scope=scope_id, budget=budget.value
                )
            await asyncio.sleep(decision.retry_after.total_seconds())
            waited += decision.retry_after


DEFAULT_RULES: tuple[LimitRule, ...] = (
    # Ordres : 60 / 2 s par instrument (place, cancel, amend partagent la portée instrument), dont réserves.
    LimitRule(
        "trade.order",
        "instrument",
        60,
        timedelta(seconds=2),
        {BudgetClass.CANCEL: 20, BudgetClass.PROTECTION: 10},
    ),
    LimitRule("trade.batch", "instrument", 300, timedelta(seconds=2), {BudgetClass.CANCEL: 100}),
    LimitRule("trade.algo", "instrument", 20, timedelta(seconds=2), {BudgetClass.PROTECTION: 10}),
    LimitRule("trade.cancel_all_after", "account", 1, timedelta(seconds=1)),
    LimitRule("trade.read", "account", 60, timedelta(seconds=2), {BudgetClass.RECONCILIATION: 20}),
    LimitRule("trade.fills_history", "account", 10, timedelta(seconds=2), {BudgetClass.RECONCILIATION: 5}),
    LimitRule("account.read", "account", 10, timedelta(seconds=2), {BudgetClass.RECONCILIATION: 4}),
    LimitRule("account.bills", "account", 5, timedelta(seconds=1), {BudgetClass.RECONCILIATION: 2}),
    LimitRule("account.config", "account", 5, timedelta(seconds=2)),
    LimitRule("ws.private.messages", "channel", 480, timedelta(hours=1), {BudgetClass.HEARTBEAT: 240}),
    LimitRule("public.download", "ip", 20, timedelta(seconds=2)),
)
