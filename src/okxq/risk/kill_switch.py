"""Kill switches persistants (§53, §54, T44).

Niveaux :
- ``SOFT_HALT`` : aucune augmentation d'exposition ; protections et réductions encadrées autorisées ;
- ``HARD_HALT`` : annulation des ordres d'entrée concernés ; la supervision des positions et les
  réductions restent possibles ;
- ``EMERGENCY_FLATTEN`` : demande de sortie contrôlée ; les résidus restent exposés et sont rapportés.

Déclencheurs configurables (``TriggerPolicy``) : données périmées, carnet invalide, état exchange
inconnu, positions divergentes, seuils de perte (journalière / drawdown), levier inattendu, erreurs
répétées, NaN, horloge non fiable. Une panne JEV n'est JAMAIS un déclencheur de protection (§50) : le
signal ``jev_unavailable`` est accepté et ignoré ici (il relève de la politique d'entrées, pas du halt).

Persistance : l'état vit dans ``risk_state`` (halt_level, raison, depuis, perte journalière avec
frontière UTC déclarée, high-water mark). Un redémarrage recharge l'état et ne remet RIEN à zéro (T44).
La perte du jour est un PLAFOND monotone à l'intérieur du jour UTC : ni une remontée d'equity ni un
apport externe ne l'effacent (T45) ; seul le passage au jour UTC suivant la remet à zéro.
Escalade seulement : le niveau ne descend jamais automatiquement au-dessus de SOFT_HALT ; SOFT_HALT se
lève après ``resume_stability_seconds`` de signaux sains (hystérésis : le compteur repart à zéro au
moindre signal). ``auto_resume_after_critical_halt=false`` → HARD/EMERGENCY ne se lèvent que par une
action opérateur autorisée, enregistrée dans ``operator_actions``, et sous préconditions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from okxq.config.schema import RiskCfg
from okxq.domain.clocks import Clock, ensure_utc, utc_day
from okxq.domain.errors import HaltError
from okxq.domain.events import RiskEvent, Severity
from okxq.domain.ids import new_id
from okxq.domain.money import ZERO, dec, dec_from_float
from okxq.domain.reasons import ReasonCode
from okxq.persistence.models import OperatorAction, RiskState


class HaltLevel(StrEnum):
    NONE = "NONE"
    SOFT_HALT = "SOFT_HALT"
    HARD_HALT = "HARD_HALT"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"

    @property
    def rank(self) -> int:
        return _RANK[self]

    @property
    def is_critical(self) -> bool:
        return self.rank >= HaltLevel.HARD_HALT.rank

    @property
    def blocks_increases(self) -> bool:
        return self is not HaltLevel.NONE

    @property
    def reason_code(self) -> ReasonCode:
        return {
            HaltLevel.NONE: ReasonCode.OK,
            HaltLevel.SOFT_HALT: ReasonCode.SOFT_HALT,
            HaltLevel.HARD_HALT: ReasonCode.HARD_HALT,
            HaltLevel.EMERGENCY_FLATTEN: ReasonCode.EMERGENCY_FLATTEN,
        }[self]


_RANK = {HaltLevel.NONE: 0, HaltLevel.SOFT_HALT: 1, HaltLevel.HARD_HALT: 2, HaltLevel.EMERGENCY_FLATTEN: 3}


def max_level(a: HaltLevel, b: HaltLevel) -> HaltLevel:
    return a if a.rank >= b.rank else b


# --- état persistant ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskStateRecord:
    """Image immuable d'une ligne ``risk_state``."""

    account_scope: str
    halt_level: HaltLevel
    halt_reason: str | None
    halt_since: datetime | None
    utc_day: datetime | None
    day_start_equity: Decimal | None
    day_realized_loss: Decimal
    high_water_mark_unit: Decimal | None
    high_water_mark_equity: Decimal | None
    limits_version: str
    updated_at: datetime
    version: int = 0

    @classmethod
    def fresh(cls, account_scope: str, limits_version: str, now: datetime) -> RiskStateRecord:
        return cls(
            account_scope=account_scope,
            halt_level=HaltLevel.NONE,
            halt_reason=None,
            halt_since=None,
            utc_day=None,
            day_start_equity=None,
            day_realized_loss=ZERO,
            high_water_mark_unit=None,
            high_water_mark_equity=None,
            limits_version=limits_version,
            updated_at=ensure_utc(now),
            version=0,
        )


@runtime_checkable
class RiskStateStore(Protocol):
    def load(self, account_scope: str) -> RiskStateRecord | None: ...

    def save(self, record: RiskStateRecord) -> RiskStateRecord: ...


class InMemoryRiskStateStore:
    """Adaptateur de test : même sémantique de version optimiste que la base."""

    def __init__(self) -> None:
        self._rows: dict[str, RiskStateRecord] = {}

    def load(self, account_scope: str) -> RiskStateRecord | None:
        return self._rows.get(account_scope)

    def save(self, record: RiskStateRecord) -> RiskStateRecord:
        current = self._rows.get(record.account_scope)
        if current is not None and current.version != record.version:
            raise HaltError("version d'état de risque périmée", expected=current.version, got=record.version)
        saved = replace(record, version=record.version + 1)
        self._rows[record.account_scope] = saved
        return saved


class SqlRiskStateStore:
    """Persistance dans ``risk_state`` avec version optimiste (aucune mise à jour aveugle, §5)."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def load(self, account_scope: str) -> RiskStateRecord | None:
        with self._factory() as session:
            row = session.get(RiskState, account_scope)
            return None if row is None else _record_from_row(row)

    def save(self, record: RiskStateRecord) -> RiskStateRecord:
        with self._factory() as session:
            row = session.get(RiskState, record.account_scope)
            if row is None:
                if record.version != 0:
                    raise HaltError("état de risque introuvable pour une version non nulle")
                row = RiskState(account_scope=record.account_scope)
                session.add(row)
            elif row.version != record.version:
                raise HaltError("version d'état de risque périmée", expected=row.version, got=record.version)
            row.halt_level = record.halt_level.value
            row.halt_reason = record.halt_reason
            row.halt_since = record.halt_since
            row.utc_day = record.utc_day
            row.day_start_equity = record.day_start_equity
            row.day_realized_loss = record.day_realized_loss
            row.high_water_mark_unit = record.high_water_mark_unit
            row.high_water_mark_equity = record.high_water_mark_equity
            row.limits_version = record.limits_version
            row.updated_at = record.updated_at
            row.version = record.version + 1
            session.commit()
            return _record_from_row(row)


def _record_from_row(row: RiskState) -> RiskStateRecord:
    return RiskStateRecord(
        account_scope=row.account_scope,
        halt_level=HaltLevel(row.halt_level),
        halt_reason=row.halt_reason,
        halt_since=row.halt_since,
        utc_day=row.utc_day,
        day_start_equity=row.day_start_equity,
        day_realized_loss=row.day_realized_loss,
        high_water_mark_unit=row.high_water_mark_unit,
        high_water_mark_equity=row.high_water_mark_equity,
        limits_version=row.limits_version,
        updated_at=row.updated_at,
        version=row.version,
    )


# --- signaux et politique ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthSignals:
    """État observé à un instant. ``equity`` est l'equity RÉCONCILIÉE (None si inconnue)."""

    now: datetime
    data_stale: bool = False
    book_invalid: bool = False
    exchange_state_unknown: bool = False
    positions_divergent: bool = False
    unexpected_leverage: bool = False
    repeated_errors: int = 0
    nan_detected: bool = False
    clock_unreliable: bool = False
    reconciliation_ok: bool = True
    jev_unavailable: bool = False  # ignoré : jamais un déclencheur de protection
    equity: Decimal | None = None
    unit_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TriggerPolicy:
    data_stale: HaltLevel = HaltLevel.SOFT_HALT
    book_invalid: HaltLevel = HaltLevel.SOFT_HALT
    exchange_state_unknown: HaltLevel = HaltLevel.HARD_HALT
    positions_divergent: HaltLevel = HaltLevel.HARD_HALT
    daily_loss: HaltLevel = HaltLevel.HARD_HALT
    drawdown: HaltLevel = HaltLevel.SOFT_HALT
    unexpected_leverage: HaltLevel = HaltLevel.HARD_HALT
    repeated_errors: HaltLevel = HaltLevel.SOFT_HALT
    repeated_errors_threshold: int = 5
    nan_detected: HaltLevel = HaltLevel.HARD_HALT
    clock_unreliable: HaltLevel = HaltLevel.HARD_HALT


@dataclass(frozen=True, slots=True)
class Trigger:
    name: str
    level: HaltLevel
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KillSwitchVerdict:
    level: HaltLevel
    previous_level: HaltLevel
    triggers: list[Trigger]
    daily_loss_fraction: Decimal | None
    drawdown_fraction: Decimal | None
    stable_for_s: float
    events: list[RiskEvent] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.level is not self.previous_level


@dataclass(frozen=True, slots=True)
class OperatorRequest:
    request_id: str
    actor: str
    role: str
    reason: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if not self.actor.strip() or not self.reason.strip():
            raise HaltError("action opérateur sans acteur ou sans raison")


AUTHORIZED_RESUME_ROLES: frozenset[str] = frozenset({"operator", "owner"})


class KillSwitch:
    """Machine d'état des halts, persistée ; un redémarrage recharge et n'efface rien (T44)."""

    def __init__(
        self,
        *,
        account_scope: str,
        store: RiskStateStore,
        cfg: RiskCfg,
        clock: Clock,
        limits_version: str,
        policy: TriggerPolicy | None = None,
    ) -> None:
        self._scope = account_scope
        self._store = store
        self._cfg = cfg
        self._clock = clock
        self._policy = policy or TriggerPolicy()
        loaded = store.load(account_scope)
        if loaded is None:
            loaded = store.save(RiskStateRecord.fresh(account_scope, limits_version, clock.now_utc()))
        self._state = loaded
        # Stabilité en mémoire seulement : après un redémarrage, le délai de reprise repart de zéro (prudent).
        self._stable_since: datetime | None = None

    # --- lecture ---------------------------------------------------------------------------------------

    @property
    def state(self) -> RiskStateRecord:
        return self._state

    def refresh(self) -> RiskStateRecord:
        """Relit l'état persisté sans rien écrire. Pour les rôles qui NE conduisent PAS la protection.

        Un seul processus doit faire avancer la machine d'état (``observe``) : deux écrivains sur la
        même ligne ``risk_state`` se disputeraient la version optimiste, et chacun recalculerait la
        frontière de jour de son côté. Mais tous les autres rôles doivent VOIR le halt que celui-là a
        décidé — sinon la stratégie continuerait de décider sur le niveau chargé à sa construction,
        c'est-à-dire sur l'état du monde au démarrage du processus. Un halt qu'on ne relit pas est un
        halt qui ne protège que le processus qui l'a levé.
        """
        loaded = self._store.load(self._scope)
        if loaded is not None:
            self._state = loaded
        return self._state

    @property
    def level(self) -> HaltLevel:
        return self._state.halt_level

    def allows(self, *, increases_exposure: bool) -> tuple[bool, ReasonCode]:
        """Une augmentation est refusée dès SOFT_HALT ; une réduction/protection reste possible."""
        if not increases_exposure:
            if self.level is HaltLevel.NONE:
                return True, ReasonCode.OK
            return True, ReasonCode.REDUCTION_ALLOWED_UNDER_HALT
        if self.level.blocks_increases:
            return False, self.level.reason_code
        return True, ReasonCode.OK

    def daily_loss_fraction(self, equity: Decimal | None = None) -> Decimal | None:
        """Fraction de perte du jour : la PIRE perte observée depuis la frontière UTC, pas l'écart
        instantané. Rendre l'écart instantané sous-déclarerait une limite déjà atteinte dès que
        l'equity remonte — notamment après un apport externe (T45)."""
        start = self._state.day_start_equity
        if start is None or start <= 0:
            return None
        worst = self._state.day_realized_loss
        if equity is not None:
            worst = max(worst, max(start - dec(equity), ZERO))
        return worst / start

    def drawdown_fraction(self, equity: Decimal | None, unit_value: Decimal | None) -> Decimal | None:
        if unit_value is not None and self._state.high_water_mark_unit:
            hwm = self._state.high_water_mark_unit
            return max(hwm - dec(unit_value), ZERO) / hwm
        if equity is not None and self._state.high_water_mark_equity:
            hwm = self._state.high_water_mark_equity
            return max(hwm - dec(equity), ZERO) / hwm
        return None

    # --- observation -----------------------------------------------------------------------------------

    def observe(self, signals: HealthSignals) -> KillSwitchVerdict:
        now = ensure_utc(signals.now)
        previous = self._state.halt_level
        state = self._roll_day_and_marks(self._state, signals, now)
        triggers = self._triggers(signals, state)
        target = previous
        reason = state.halt_reason
        for t in triggers:
            if t.level.rank > target.rank:
                target = t.level
                reason = t.name
        events: list[RiskEvent] = []
        if triggers:
            self._stable_since = None
        elif self._stable_since is None:
            self._stable_since = now
        stable_for = 0.0 if self._stable_since is None else (now - self._stable_since).total_seconds()

        if target.rank > previous.rank:
            state = replace(state, halt_level=target, halt_reason=reason, halt_since=now)
            events.append(
                self._event(
                    Severity.CRITICAL if target.is_critical else Severity.WARN, target, reason or "", now
                )
            )
        elif (
            previous is HaltLevel.SOFT_HALT
            and not triggers
            and stable_for >= self._cfg.resume_stability_seconds
        ):
            state = replace(state, halt_level=HaltLevel.NONE, halt_reason=None, halt_since=None)
            events.append(self._event(Severity.INFO, HaltLevel.NONE, "auto_resume_after_soft_halt", now))
        elif previous.is_critical and not triggers and stable_for >= self._cfg.resume_stability_seconds:
            # auto_resume_after_critical_halt est False par construction (Literal[False]) : reprise opérateur.
            events.append(
                self._event(Severity.WARN, previous, ReasonCode.OPERATOR_ACTION_REQUIRED.value, now)
            )

        state = replace(state, updated_at=now)
        self._state = self._store.save(state)
        return KillSwitchVerdict(
            level=self._state.halt_level,
            previous_level=previous,
            triggers=triggers,
            daily_loss_fraction=self.daily_loss_fraction(signals.equity),
            drawdown_fraction=self.drawdown_fraction(signals.equity, signals.unit_value),
            stable_for_s=stable_for,
            events=events,
        )

    def _roll_day_and_marks(
        self, state: RiskStateRecord, signals: HealthSignals, now: datetime
    ) -> RiskStateRecord:
        if signals.equity is None:
            return state
        equity = dec(signals.equity)
        day = utc_day(now)
        if state.utc_day is None or state.utc_day != day:
            state = replace(state, utc_day=day, day_start_equity=equity, day_realized_loss=ZERO)
        else:
            dip = max((state.day_start_equity or equity) - equity, ZERO)
            # POURQUOI un maximum et non la valeur courante : la perte du jour est la PIRE perte
            # observée depuis la frontière UTC déclarée. La recalculer à chaque observation
            # l'effaçait dès que l'equity remontait — et un simple DÉPÔT externe suffisait à faire
            # remonter l'equity au-dessus de son point de départ, ramenant la perte du jour à zéro
            # (T45). Il suffisait alors d'un virement pour faire disparaître une limite journalière
            # atteinte, donc pour rendre une reprise possible le jour même. Seule la frontière de
            # jour UTC (branche au-dessus) remet ce plafond à zéro.
            state = replace(state, day_realized_loss=max(state.day_realized_loss, dip))
        hwm_eq = state.high_water_mark_equity
        if hwm_eq is None or equity > hwm_eq:
            state = replace(state, high_water_mark_equity=equity)
        if signals.unit_value is not None:
            unit = dec(signals.unit_value)
            hwm_u = state.high_water_mark_unit
            if hwm_u is None or unit > hwm_u:
                state = replace(state, high_water_mark_unit=unit)
        return state

    def _triggers(self, s: HealthSignals, state: RiskStateRecord) -> list[Trigger]:
        p = self._policy
        out: list[Trigger] = []
        if s.data_stale:
            out.append(Trigger("data_stale", p.data_stale))
        if s.book_invalid:
            out.append(Trigger("book_invalid", p.book_invalid))
        if s.exchange_state_unknown:
            out.append(Trigger("exchange_state_unknown", p.exchange_state_unknown))
        if s.positions_divergent:
            out.append(Trigger("positions_divergent", p.positions_divergent))
        if s.unexpected_leverage:
            out.append(Trigger("unexpected_leverage", p.unexpected_leverage))
        if s.repeated_errors >= p.repeated_errors_threshold:
            out.append(Trigger("repeated_errors", p.repeated_errors, {"count": str(s.repeated_errors)}))
        if s.nan_detected:
            out.append(Trigger("nan_detected", p.nan_detected))
        if s.clock_unreliable:
            out.append(Trigger("clock_unreliable", p.clock_unreliable))
        start = state.day_start_equity
        if start is not None and start > 0:
            # On part de la perte du jour PERSISTÉE (déjà un plafond, voir ``_roll_day_and_marks``)
            # et non du seul écart instantané : sinon une remontée d'equity ou un apport externe
            # ferait disparaître le déclencheur d'une limite déjà atteinte, et l'opérateur pourrait
            # lever le halt le jour même (T45). Le déclencheur survit aussi à un redémarrage sans
            # equity observée, puisqu'il est lu dans l'état rechargé (T44).
            loss = state.day_realized_loss
            if s.equity is not None:
                loss = max(loss, max(start - dec(s.equity), ZERO))
            fraction = loss / start
            if fraction >= dec_frac(self._cfg.daily_loss_halt_fraction):
                out.append(Trigger("daily_loss", p.daily_loss, {"fraction": format(fraction, "f")}))
        dd = self.drawdown_from(state, s.equity, s.unit_value)
        if dd is not None and dd >= dec_frac(self._cfg.drawdown_review_fraction):
            out.append(Trigger("drawdown", p.drawdown, {"fraction": format(dd, "f")}))
        # s.jev_unavailable : volontairement ignoré (§50).
        return out

    @staticmethod
    def drawdown_from(
        state: RiskStateRecord, equity: Decimal | None, unit_value: Decimal | None
    ) -> Decimal | None:
        if unit_value is not None and state.high_water_mark_unit:
            return max(state.high_water_mark_unit - dec(unit_value), ZERO) / state.high_water_mark_unit
        if equity is not None and state.high_water_mark_equity:
            return max(state.high_water_mark_equity - dec(equity), ZERO) / state.high_water_mark_equity
        return None

    # --- actions ---------------------------------------------------------------------------------------

    def request(
        self, level: HaltLevel, reason: str, *, operator: OperatorRequest | None = None
    ) -> RiskStateRecord:
        """Escalade manuelle (pause, demande de flatten). Ne descend jamais le niveau."""
        now = self._clock.now_utc()
        if level.rank <= self._state.halt_level.rank:
            return self._state
        who = f"{operator.actor}:{operator.request_id}" if operator else "system"
        self._state = self._store.save(
            replace(
                self._state,
                halt_level=level,
                halt_reason=f"{reason} [{who}]"[:256],
                halt_since=now,
                updated_at=now,
            )
        )
        self._stable_since = None
        return self._state

    def resume_preconditions(self, signals: HealthSignals) -> list[str]:
        """Préconditions d'une reprise : aucun déclencheur actif, réconciliation OK, délai de stabilité écoulé."""
        problems: list[str] = []
        now = ensure_utc(signals.now)
        triggers = self._triggers(signals, self._roll_day_and_marks(self._state, signals, now))
        if triggers:
            problems.append("declencheurs_actifs:" + ",".join(t.name for t in triggers))
        if not signals.reconciliation_ok:
            problems.append("reconciliation_non_ok")
        since = self._state.halt_since
        if since is not None and (now - since) < timedelta(seconds=self._cfg.resume_stability_seconds):
            problems.append("delai_de_stabilite_non_ecoule")
        return problems

    def operator_resume(self, request: OperatorRequest, signals: HealthSignals) -> RiskStateRecord:
        """Reprise après halt critique : uniquement par action opérateur autorisée et sous préconditions."""
        if self._state.halt_level is HaltLevel.NONE:
            return self._state
        if request.role not in AUTHORIZED_RESUME_ROLES:
            raise HaltError(
                "rôle non autorisé à lever un halt",
                code=ReasonCode.OPERATOR_ACTION_REQUIRED.value,
                role=request.role,
            )
        problems = self.resume_preconditions(signals)
        if problems:
            raise HaltError(
                "préconditions de reprise non satisfaites",
                code=ReasonCode.RESUME_PRECONDITIONS_UNMET.value,
                problems=problems,
            )
        now = ensure_utc(signals.now)
        self._state = self._store.save(
            replace(self._state, halt_level=HaltLevel.NONE, halt_reason=None, halt_since=None, updated_at=now)
        )
        self._stable_since = now
        return self._state

    # --- utilitaires -----------------------------------------------------------------------------------

    def _event(self, severity: Severity, level: HaltLevel, reason: str, now: datetime) -> RiskEvent:
        return RiskEvent(
            event_id=new_id("rsk"),
            severity=severity,
            reason_code=level.reason_code.value if level is not HaltLevel.NONE else ReasonCode.OK.value,
            affected_scope=self._scope,
            evidence={"trigger": reason, "level": level.value},
            requested_action=level.value,
            created_at=now,
        )


def dec_frac(value: float) -> Decimal:
    return dec_from_float(float(value), 12)


def record_operator_action(
    session: Session,
    *,
    action: str,
    scope: str,
    request: OperatorRequest,
    status: str,
    observed_result: dict[str, object] | None = None,
    completed_at: datetime | None = None,
) -> OperatorAction:
    """Journalise une action opérateur (audit, §53) ; la ligne est créée AVANT l'application."""
    row = OperatorAction(
        request_id=request.request_id,
        action=action,
        scope=scope,
        reason=request.reason[:512],
        actor=request.actor[:128],
        role=request.role[:32],
        requested_at=ensure_utc(request.requested_at),
        status=status,
        observed_result=dict(observed_result or {}),
        completed_at=completed_at,
    )
    session.add(row)
    session.flush()
    return row


def latest_operator_actions(session: Session, *, limit: int = 20) -> list[OperatorAction]:
    stmt = select(OperatorAction).order_by(OperatorAction.requested_at.desc()).limit(limit)
    return list(session.scalars(stmt))
