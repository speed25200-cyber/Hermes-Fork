"""Alertes : seuils avec hystérésis, déduplication, destinataires, puits enfichables (§61).

Trois refus explicites :

- **Rien n'est présumé envoyé.** Un webhook sans URL configurée produit une livraison marquée non
  délivrée avec son motif ; une réponse non 2xx n'est pas un envoi confirmé. Prétendre qu'une alerte
  est partie sans canal configuré est la façon la plus sûre de ne pas être prévenu.
- **Pas de battement d'alerte.** L'hystérésis (deux seuils) et la déduplication par fenêtre évitent
  qu'une valeur qui oscille autour d'un seuil réveille l'opérateur vingt fois ; une AGGRAVATION de
  priorité passe malgré la fenêtre, parce que c'est l'information nouvelle.
- **Pas de secret dans une alerte.** Les messages passent par le masqueur des journaux avant livraison.

Priorités §61 : P1 = exposition sans protection confirmée, état de compte inconnu, sortie urgente non
exécutée, mismatch financier, perte de leadership, rupture prolongée du flux privé.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from okxq.domain.clocks import Clock, SystemClock, ensure_utc
from okxq.domain.ids import new_id
from okxq.runtime.logging import get_logger, get_masker

_log = get_logger("okxq.alerts")


class Priority(StrEnum):
    P1 = "P1"  # action immédiate : risque de perte non contrôlée
    P2 = "P2"  # dégradation sérieuse : le système protège mais n'opère plus normalement
    P3 = "P3"  # anomalie à examiner
    P4 = "P4"  # information

    @property
    def rank(self) -> int:
        return {"P1": 4, "P2": 3, "P3": 2, "P4": 1}[self.value]


@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: str
    key: str
    priority: Priority
    title: str
    message: str
    state: str  # FIRING / RESOLVED
    created_at: datetime
    recipients: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "alert_id": self.alert_id,
            "key": self.key,
            "priority": self.priority.value,
            "title": self.title,
            "message": self.message,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "recipients": list(self.recipients),
            "evidence": dict(self.evidence),
        }
        masked = get_masker().mask(payload)
        return masked if isinstance(masked, dict) else payload


@dataclass(frozen=True, slots=True)
class Delivery:
    sink: str
    alert_id: str
    delivered: bool
    detail: str
    at: datetime


@runtime_checkable
class AlertSink(Protocol):
    name: str

    def send(self, alert: Alert, now: datetime) -> Delivery: ...


@dataclass(frozen=True, slots=True)
class AlertRule:
    """Règle de seuil avec hystérésis : ``threshold`` déclenche, ``clear_threshold`` résout."""

    name: str
    metric: str
    threshold: float
    clear_threshold: float
    direction: str  # "above" | "below"
    priority: Priority
    title: str
    message: str = ""

    def __post_init__(self) -> None:
        if self.direction not in ("above", "below"):
            raise ValueError("direction doit valoir 'above' ou 'below'")
        if self.direction == "above" and self.clear_threshold > self.threshold:
            raise ValueError("hystérésis incohérente : clear_threshold doit être ≤ threshold (above)")
        if self.direction == "below" and self.clear_threshold < self.threshold:
            raise ValueError("hystérésis incohérente : clear_threshold doit être ≥ threshold (below)")

    def fires(self, value: float) -> bool:
        return value > self.threshold if self.direction == "above" else value < self.threshold

    def clears(self, value: float) -> bool:
        return value <= self.clear_threshold if self.direction == "above" else value >= self.clear_threshold


#: Règles par défaut. Les priorités P1 reprennent la liste §61 « alerter en priorité ».
DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        "position_non_protegee",
        "unprotected_position_notional",
        0.0,
        0.0,
        "above",
        Priority.P1,
        "Exposition sans protection confirmée",
        "Une position est ouverte sans stop CONFIRMÉ côté exchange.",
    ),
    AlertRule(
        "etat_compte_inconnu",
        "unknown_orders_total",
        0.0,
        0.0,
        "above",
        Priority.P1,
        "État de compte inconnu",
        "Au moins un ordre est en état UNKNOWN : réconcilier avant toute nouvelle entrée.",
    ),
    AlertRule(
        "mismatch_financier",
        "reconciliation_gap_usdt",
        1.0,
        0.25,
        "above",
        Priority.P1,
        "Écart de réconciliation financière",
        "Le ledger et l'exchange divergent au-delà du seuil.",
    ),
    AlertRule(
        "flux_prive_rompu",
        "private_stream_health",
        0.5,
        0.9,
        "below",
        Priority.P1,
        "Flux privé dégradé ou rompu",
        "Le flux privé n'est ni connecté ni réconcilié.",
    ),
    AlertRule(
        "halt_critique",
        "halt_state",
        1.0,
        0.0,
        "above",
        Priority.P1,
        "Halt critique actif",
        "Le système est en HARD_HALT, sortie d'urgence ou état inconnu.",
    ),
    AlertRule(
        "donnees_perimees",
        "market_data_age_seconds",
        5.0,
        2.0,
        "above",
        Priority.P2,
        "Données de marché périmées",
        "L'âge de la donnée dépasse la tolérance de décision.",
    ),
    AlertRule(
        "perte_journaliere",
        "daily_loss",
        0.008,
        0.004,
        "above",
        Priority.P2,
        "Perte journalière proche du seuil d'arrêt",
        "La perte du jour approche le seuil d'arrêt configuré.",
    ),
    AlertRule(
        "drawdown_revue",
        "drawdown",
        0.05,
        0.03,
        "above",
        Priority.P2,
        "Drawdown au seuil de revue",
        "Le drawdown atteint le seuil de revue.",
    ),
    AlertRule(
        "marge_haute",
        "margin_utilization",
        0.45,
        0.35,
        "above",
        Priority.P2,
        "Utilisation de marge élevée",
        "La marge utilisée approche le plafond.",
    ),
    AlertRule(
        "deadline_manquee",
        "decision_deadline_miss_total",
        3.0,
        0.0,
        "above",
        Priority.P3,
        "Décisions hors budget",
        "Des décisions terminent après leur deadline.",
    ),
    AlertRule(
        "outbox_engorgee",
        "outbox_depth",
        50.0,
        10.0,
        "above",
        Priority.P3,
        "Outbox engorgée",
        "Les intentions s'accumulent sans être envoyées.",
    ),
    AlertRule(
        "disque_faible",
        "disk_free_bytes",
        2e9,
        5e9,
        "below",
        Priority.P3,
        "Disque bientôt plein",
        "L'espace libre passe sous le seuil : collecte menacée.",
    ),
    AlertRule(
        "jev_indisponible",
        "jev_error_total",
        5.0,
        1.0,
        "above",
        Priority.P4,
        "JEV en erreur répétée",
        "Le sous-système sémantique échoue ; le risque n'est pas affecté.",
    ),
)


class LocalJsonlSink:
    """Puits local par défaut : une ligne JSON par alerte, dans un fichier append-only."""

    name = "local_jsonl"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, alert: Alert, now: datetime) -> Delivery:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(alert.to_dict(), ensure_ascii=False) + "\n")
            return Delivery(self.name, alert.alert_id, True, f"écrit dans {self.path}", now)
        except OSError as exc:
            return Delivery(self.name, alert.alert_id, False, f"écriture impossible : {exc}", now)


WebhookTransport = Callable[[str, dict[str, Any]], int]


class WebhookSink:
    """Adaptateur générique. Sans URL configurée, RIEN n'est envoyé et la livraison le dit."""

    name = "webhook"

    def __init__(self, url: str | None = None, *, transport: WebhookTransport | None = None) -> None:
        self.url = url
        self._transport = transport

    def send(self, alert: Alert, now: datetime) -> Delivery:
        if not self.url:
            return Delivery(
                self.name, alert.alert_id, False, "ALERT_WEBHOOK_URL absent : aucun envoi tenté", now
            )
        if self._transport is None:
            return Delivery(
                self.name, alert.alert_id, False, "aucun transport HTTP fourni : aucun envoi tenté", now
            )
        try:
            status = self._transport(self.url, alert.to_dict())
        except Exception as exc:  # une panne de canal n'est jamais un envoi réussi
            return Delivery(self.name, alert.alert_id, False, f"échec du transport : {exc!r}", now)
        if 200 <= status < 300:
            return Delivery(self.name, alert.alert_id, True, f"HTTP {status}", now)
        return Delivery(self.name, alert.alert_id, False, f"envoi non confirmé : HTTP {status}", now)


@dataclass(slots=True)
class _RuleState:
    firing: bool = False
    since: datetime | None = None
    last_value: float | None = None


class AlertManager:
    def __init__(
        self,
        *,
        rules: Sequence[AlertRule] | None = None,
        sinks: Sequence[AlertSink] | None = None,
        clock: Clock | None = None,
        dedup_seconds: int = 300,
        recipients: dict[Priority, list[str]] | None = None,
    ) -> None:
        self.rules = list(rules or ())
        self.sinks = list(sinks or ())
        self._clock = clock or SystemClock()
        self._dedup = timedelta(seconds=dedup_seconds)
        self._recipients = dict(recipients or {})
        self._rule_state: dict[str, _RuleState] = {r.name: _RuleState() for r in self.rules}
        self._last_sent: dict[str, tuple[datetime, Priority]] = {}
        self._active: dict[str, Alert] = {}
        self.deliveries: list[Delivery] = []

    # --- API ---------------------------------------------------------------------------------------------

    def observe(self, metric: str, value: float) -> list[Alert]:
        """Applique les règles portant sur cette métrique. Rend les alertes réellement émises."""
        now = ensure_utc(self._clock.now_utc())
        emitted: list[Alert] = []
        for rule in self.rules:
            if rule.metric != metric:
                continue
            state = self._rule_state.setdefault(rule.name, _RuleState())
            state.last_value = float(value)
            if not state.firing and rule.fires(value):
                state.firing, state.since = True, now
                alert = self._emit(
                    key=rule.name,
                    priority=rule.priority,
                    title=rule.title,
                    message=rule.message or f"{metric} = {value} (seuil {rule.threshold})",
                    state="FIRING",
                    now=now,
                    evidence={"metric": metric, "value": value, "threshold": rule.threshold},
                    bypass_dedup=True,
                )
                if alert is not None:
                    self._active[rule.name] = alert
                    emitted.append(alert)
            elif state.firing and rule.clears(value):
                state.firing, state.since = False, None
                alert = self._emit(
                    key=rule.name,
                    priority=rule.priority,
                    title=rule.title,
                    message=f"{metric} revenu à {value} (seuil de résolution {rule.clear_threshold})",
                    state="RESOLVED",
                    now=now,
                    evidence={"metric": metric, "value": value, "clear_threshold": rule.clear_threshold},
                    bypass_dedup=True,
                )
                self._active.pop(rule.name, None)
                if alert is not None:
                    emitted.append(alert)
        return emitted

    def raise_alert(
        self,
        key: str,
        priority: Priority,
        title: str,
        message: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> Alert | None:
        """Lève une alerte hors règle de seuil. Rend None si la déduplication l'absorbe."""
        now = ensure_utc(self._clock.now_utc())
        return self._emit(
            key=key,
            priority=priority,
            title=title,
            message=message,
            state="FIRING",
            now=now,
            evidence=evidence or {},
            bypass_dedup=False,
        )

    def active(self) -> list[Alert]:
        return list(self._active.values())

    def recipients_for(self, priority: Priority) -> list[str]:
        return list(self._recipients.get(priority, []))

    # --- interne -----------------------------------------------------------------------------------------

    def _deduplicated(self, key: str, priority: Priority, now: datetime) -> bool:
        previous = self._last_sent.get(key)
        if previous is None:
            return False
        sent_at, sent_priority = previous
        if now - sent_at >= self._dedup:
            return False
        # Une AGGRAVATION passe : c'est une information nouvelle, pas une répétition.
        return priority.rank <= sent_priority.rank

    def _emit(
        self,
        *,
        key: str,
        priority: Priority,
        title: str,
        message: str,
        state: str,
        now: datetime,
        evidence: dict[str, Any],
        bypass_dedup: bool,
    ) -> Alert | None:
        if not bypass_dedup and self._deduplicated(key, priority, now):
            return None
        alert = Alert(
            alert_id=new_id("alr"),
            key=key,
            priority=priority,
            title=title,
            message=message,
            state=state,
            created_at=now,
            recipients=self.recipients_for(priority),
            evidence=evidence,
        )
        self._last_sent[key] = (now, priority)
        for sink in self.sinks:
            delivery = sink.send(alert, now)
            self.deliveries.append(delivery)
            if not delivery.delivered:
                _log.warning("alerte non délivrée", sink=delivery.sink, key=key, detail=delivery.detail)
        return alert


def build_default_manager(
    *,
    sink_path: str | Path = "data/alerts/alerts.jsonl",
    alert_sink: str = "local",
    webhook_url: str | None = None,
    webhook_transport: WebhookTransport | None = None,
    dedup_seconds: int = 300,
    recipients: dict[Priority, list[str]] | None = None,
    clock: Clock | None = None,
) -> AlertManager:
    """Gestionnaire par défaut : puits local toujours présent, webhook seulement s'il est configuré."""
    sinks: list[AlertSink] = [LocalJsonlSink(sink_path)]
    if alert_sink == "webhook":
        sinks.append(WebhookSink(webhook_url, transport=webhook_transport))
    return AlertManager(
        rules=DEFAULT_RULES, sinks=sinks, clock=clock, dedup_seconds=dedup_seconds, recipients=recipients
    )
