"""Watchdog à heartbeats CONDITIONNÉS (§53, T61).

Un heartbeat n'est pas un « je suis vivant » aveugle : il est accepté seulement s'il est accompagné de
l'état réellement surveillé et si cet état est sain (processus stratégie : décision récente ; âge de la
réconciliation ; âge du flux privé ; leadership détenu). Un heartbeat sans état observé est refusé
(``HEARTBEAT_UNCONDITIONED``).

T61 : le processus de stratégie est mort mais le processus de heartbeat, lui, tourne → le watchdog
applique la POLITIQUE DE SÉCURITÉ, pas un maintien aveugle : aucune nouvelle entrée, les ordres
d'entrée orphelins expirent ou sont annulés, la supervision des positions (réductions, protections)
est conservée, et un SOFT_HALT est demandé.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from okxq.domain.clocks import Clock, ensure_utc
from okxq.domain.reasons import ReasonCode
from okxq.risk.kill_switch import HaltLevel


@dataclass(frozen=True, slots=True)
class ObservedState:
    """État réellement observé, joint à chaque heartbeat."""

    observed_at: datetime
    strategy_alive: bool | None = None
    strategy_last_decision_at: datetime | None = None
    reconciliation_completed_at: datetime | None = None
    private_stream_last_at: datetime | None = None
    leadership_held: bool | None = None


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    accepted: bool
    reason: ReasonCode
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SafetyPolicy:
    allow_new_entries: bool
    cancel_orphan_entries: bool
    keep_position_supervision: bool
    requested_halt: HaltLevel
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WatchdogVerdict:
    now: datetime
    strategy_ok: bool
    reconciliation_ok: bool
    private_stream_ok: bool
    leadership_ok: bool
    policy: SafetyPolicy


NORMAL_POLICY = SafetyPolicy(
    allow_new_entries=True, cancel_orphan_entries=False, keep_position_supervision=True, requested_halt=HaltLevel.NONE
)


class Watchdog:
    def __init__(
        self,
        *,
        clock: Clock,
        max_decision_age_s: float,
        max_reconciliation_age_s: float,
        max_private_stream_age_s: float,
        max_heartbeat_age_s: float,
    ) -> None:
        self._clock = clock
        self._max_decision_age_s = max_decision_age_s
        self._max_reconciliation_age_s = max_reconciliation_age_s
        self._max_private_stream_age_s = max_private_stream_age_s
        self._max_heartbeat_age_s = max_heartbeat_age_s
        self._last_accepted: dict[str, tuple[datetime, ObservedState]] = {}
        self._last_observed: ObservedState | None = None

    # --- heartbeats ------------------------------------------------------------------------------------

    def heartbeat(self, component: str, observed: ObservedState | None) -> HeartbeatResult:
        """Accepte le heartbeat SEULEMENT si l'état observé du composant est présent et sain."""
        now = self._clock.now_utc()
        if observed is None:
            return HeartbeatResult(False, ReasonCode.HEARTBEAT_UNCONDITIONED, "heartbeat sans état observé")
        observed_at = ensure_utc(observed.observed_at)
        if (now - observed_at).total_seconds() > self._max_heartbeat_age_s:
            return HeartbeatResult(False, ReasonCode.DATA_STALE, "état observé trop ancien")
        self._last_observed = observed
        healthy, reason, detail = self._component_health(component, observed, now)
        if not healthy:
            return HeartbeatResult(False, reason, detail)
        self._last_accepted[component] = (now, observed)
        return HeartbeatResult(True, ReasonCode.OK)

    def _component_health(self, component: str, o: ObservedState, now: datetime) -> tuple[bool, ReasonCode, str]:
        if component == "strategy":
            if o.strategy_alive is None or o.strategy_last_decision_at is None:
                return False, ReasonCode.HEARTBEAT_UNCONDITIONED, "état de la stratégie non fourni"
            if not o.strategy_alive:
                return False, ReasonCode.STRATEGY_PROCESS_DEAD, "processus stratégie mort"
            age = (now - ensure_utc(o.strategy_last_decision_at)).total_seconds()
            if age > self._max_decision_age_s:
                return False, ReasonCode.STRATEGY_PROCESS_DEAD, f"dernière décision il y a {age:.0f}s"
            return True, ReasonCode.OK, ""
        if component == "reconciliation":
            if o.reconciliation_completed_at is None:
                return False, ReasonCode.RECONCILIATION_PENDING, "aucune réconciliation observée"
            age = (now - ensure_utc(o.reconciliation_completed_at)).total_seconds()
            if age > self._max_reconciliation_age_s:
                return False, ReasonCode.RECONCILIATION_PENDING, f"réconciliation vieille de {age:.0f}s"
            return True, ReasonCode.OK, ""
        if component == "private_stream":
            if o.private_stream_last_at is None:
                return False, ReasonCode.CONNECTION_LOST, "flux privé jamais observé"
            age = (now - ensure_utc(o.private_stream_last_at)).total_seconds()
            if age > self._max_private_stream_age_s:
                return False, ReasonCode.CONNECTION_LOST, f"flux privé vieux de {age:.0f}s"
            return True, ReasonCode.OK, ""
        if component == "leadership":
            if o.leadership_held is None:
                return False, ReasonCode.HEARTBEAT_UNCONDITIONED, "leadership non observé"
            if not o.leadership_held:
                return False, ReasonCode.NOT_LEADER, "bail non détenu"
            return True, ReasonCode.OK, ""
        return False, ReasonCode.HEARTBEAT_UNCONDITIONED, f"composant inconnu : {component}"

    # --- verdict ---------------------------------------------------------------------------------------

    def _fresh(self, component: str, now: datetime) -> bool:
        entry = self._last_accepted.get(component)
        if entry is None:
            return False
        return (now - entry[0]).total_seconds() <= self._max_heartbeat_age_s

    def evaluate(self) -> WatchdogVerdict:
        now = self._clock.now_utc()
        strategy_ok = self._fresh("strategy", now)
        reconciliation_ok = self._fresh("reconciliation", now)
        private_ok = self._fresh("private_stream", now)
        leadership_ok = self._fresh("leadership", now)
        reasons: list[str] = []
        if not strategy_ok:
            reasons.append(ReasonCode.STRATEGY_PROCESS_DEAD.value)
        if not reconciliation_ok:
            reasons.append(ReasonCode.RECONCILIATION_PENDING.value)
        if not private_ok:
            reasons.append(ReasonCode.CONNECTION_LOST.value)
        if not leadership_ok:
            reasons.append(ReasonCode.NOT_LEADER.value)
        if not reasons:
            policy = NORMAL_POLICY
        else:
            # Politique de sécurité (T61) : jamais de maintien aveugle. La supervision des positions reste.
            halt = HaltLevel.SOFT_HALT
            if not leadership_ok or not private_ok:
                halt = HaltLevel.HARD_HALT
            policy = SafetyPolicy(
                allow_new_entries=False,
                cancel_orphan_entries=True,
                keep_position_supervision=True,
                requested_halt=halt,
                reasons=reasons,
            )
        return WatchdogVerdict(
            now=now,
            strategy_ok=strategy_ok,
            reconciliation_ok=reconciliation_ok,
            private_stream_ok=private_ok,
            leadership_ok=leadership_ok,
            policy=policy,
        )
