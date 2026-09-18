"""Machine d'état des ordres (§28, §52.2).

Deux axes distincts :
- ``OrderState`` : ce que l'on a OBSERVÉ (localement ou depuis l'exchange) ;
- ``PendingOperation`` : l'opération réseau en cours (submit/cancel/amend), qui peut se résoudre avant,
  après, ou jamais (UNKNOWN) par rapport aux événements du flux privé.

Règles de transition (fonctions pures, projection immuable) :
- un événement déjà appliqué (même ``raw_hash``/``event_id``) est ignoré : idempotence (T34) ;
- un fill peut précéder l'ACK local (T35) : il est accepté et résout l'opération SUBMIT ;
- ``cumulative_filled`` n'est JAMAIS décrémenté par un événement ancien ; un fill signalé après une
  annulation mais exécuté avant elle augmente la quantité sans « dé-annuler » l'ordre (T31) ;
- un état terminal est absorbant, sauf ``FILLED`` qui peut succéder à ``CANCELED``/``EXPIRED`` lorsque
  l'exchange prouve l'exécution complète (la vérité de l'exchange prime) ;
- une correction fournisseur (quantité revue à la baisse) est une procédure DISTINCTE et traçable
  (:func:`apply_provider_correction`), jamais un effet de bord d'un événement ;
- la projection est reconstruisible depuis les événements bruts (:func:`rebuild_projection`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from okxq.domain.errors import OrderStateError
from okxq.domain.events import OrderEvent, OrderEventKind
from okxq.domain.ids import payload_hash
from okxq.domain.orders import OrderState, PendingOperation, is_terminal

__all__ = [
    "OrderProjection",
    "ProviderCorrection",
    "TransitionResult",
    "apply_event",
    "apply_provider_correction",
    "event_dedup_key",
    "mark_acknowledged",
    "mark_cancel_requested",
    "mark_rejected_locally",
    "mark_submitted",
    "mark_unknown",
    "rebuild_projection",
]

_RANK: dict[OrderState, int] = {
    OrderState.INTENT_CREATED: 0,
    OrderState.RISK_APPROVED: 1,
    OrderState.SUBMITTED: 2,
    OrderState.UNKNOWN: 2,
    OrderState.ACKNOWLEDGED: 3,
    OrderState.CANCEL_REQUESTED: 3,
    OrderState.PARTIALLY_FILLED: 4,
    OrderState.FILLED: 5,
    OrderState.CANCELED: 5,
    OrderState.REJECTED: 5,
    OrderState.EXPIRED: 5,
}


def event_dedup_key(event: OrderEvent) -> str:
    """Clé d'idempotence d'un événement : son ``raw_hash`` s'il existe, sinon le hachage de son contenu."""
    if event.raw_hash:
        return event.raw_hash
    return payload_hash(event.model_dump(mode="json", exclude={"event_id"}))


@dataclass(frozen=True, slots=True)
class ProviderCorrection:
    """Trace d'une correction de quantité imposée par le fournisseur (procédure distincte, §52.2)."""

    corrected_at: datetime
    previous_cumulative: Decimal
    corrected_cumulative: Decimal
    reason: str
    reference: str


@dataclass(frozen=True, slots=True)
class OrderProjection:
    """Projection immuable d'un ordre : état observé + opération réseau + quantités."""

    client_order_id: str
    contracts: Decimal
    observed_state: OrderState = OrderState.RISK_APPROVED
    pending_operation: PendingOperation = PendingOperation.NONE
    cumulative_filled: Decimal = Decimal(0)
    average_fill_price: Decimal | None = None
    exchange_order_id: str | None = None
    last_event_ts: datetime | None = None
    last_receive_ts: datetime | None = None
    terminal_at: datetime | None = None
    local_ack_at: datetime | None = None
    needs_reconciliation: bool = False
    applied_keys: frozenset[str] = field(default_factory=frozenset)
    corrections: tuple[ProviderCorrection, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.observed_state)

    @property
    def remaining(self) -> Decimal:
        return max(Decimal(0), self.contracts - self.cumulative_filled)


@dataclass(frozen=True, slots=True)
class TransitionResult:
    projection: OrderProjection
    changed: bool
    duplicate: bool = False
    ignored_reason: str | None = None
    became_terminal: bool = False
    anomalies: tuple[str, ...] = ()


# --- transitions locales (opérations réseau) ----------------------------------------------------------


def mark_submitted(p: OrderProjection, *, sent_at: datetime) -> OrderProjection:
    """Tentative marquée AVANT l'envoi réseau : état SUBMITTED, opération SUBMIT en cours."""
    if p.observed_state is not OrderState.RISK_APPROVED or p.pending_operation is not PendingOperation.NONE:
        raise OrderStateError(
            "envoi impossible : l'ordre n'est pas en RISK_APPROVED sans opération en cours",
            client_order_id=p.client_order_id,
            observed_state=p.observed_state.value,
            pending_operation=p.pending_operation.value,
        )
    return replace(
        p,
        observed_state=OrderState.SUBMITTED,
        pending_operation=PendingOperation.SUBMIT,
        last_receive_ts=sent_at,
    )


def mark_acknowledged(
    p: OrderProjection, *, exchange_order_id: str | None, ack_at: datetime
) -> TransitionResult:
    """ACK local de ``place_order``. Ne régresse jamais un état déjà avancé par un fill (T35)."""
    if p.is_terminal or _RANK[p.observed_state] >= _RANK[OrderState.ACKNOWLEDGED]:
        # Le flux privé a déjà parlé : on ne fait que compléter l'identifiant exchange et l'heure d'ACK.
        new = replace(
            p,
            exchange_order_id=p.exchange_order_id or exchange_order_id,
            local_ack_at=p.local_ack_at or ack_at,
            pending_operation=PendingOperation.NONE
            if p.pending_operation is PendingOperation.SUBMIT
            else p.pending_operation,
        )
        return TransitionResult(new, changed=new != p, ignored_reason="ack_after_progress")
    new = replace(
        p,
        observed_state=OrderState.ACKNOWLEDGED,
        pending_operation=PendingOperation.NONE,
        exchange_order_id=p.exchange_order_id or exchange_order_id,
        local_ack_at=ack_at,
        last_receive_ts=ack_at,
        needs_reconciliation=False,
    )
    return TransitionResult(new, changed=True)


def mark_rejected_locally(p: OrderProjection, *, at: datetime) -> TransitionResult:
    """Rejet synchrone de ``place_order`` : l'exchange n'a pas créé l'ordre (état terminal)."""
    if p.is_terminal:
        return TransitionResult(p, changed=False, ignored_reason="already_terminal")
    if p.cumulative_filled > 0:
        # Un rejet après un fill observé est contradictoire : l'exchange prime, on réconcilie.
        return TransitionResult(
            replace(p, needs_reconciliation=True, pending_operation=PendingOperation.NONE),
            changed=True,
            anomalies=("reject_after_fill",),
        )
    new = replace(
        p,
        observed_state=OrderState.REJECTED,
        pending_operation=PendingOperation.NONE,
        terminal_at=at,
        last_receive_ts=at,
    )
    return TransitionResult(new, changed=True, became_terminal=True)


def mark_unknown(p: OrderProjection, *, at: datetime) -> TransitionResult:
    """ACK perdu (timeout, coupure) : l'ordre existe peut-être. Aucun retry aveugle ; réconcilier (T32)."""
    if p.is_terminal or _RANK[p.observed_state] > _RANK[OrderState.UNKNOWN]:
        return TransitionResult(
            replace(p, pending_operation=PendingOperation.NONE),
            changed=p.pending_operation is not PendingOperation.NONE,
            ignored_reason="exchange_already_observed",
        )
    new = replace(
        p,
        observed_state=OrderState.UNKNOWN,
        pending_operation=PendingOperation.NONE,
        needs_reconciliation=True,
        last_receive_ts=at,
    )
    return TransitionResult(new, changed=True)


def mark_cancel_requested(p: OrderProjection) -> OrderProjection:
    """Annulation = opération réseau distincte ; l'état observé ne change pas avant la réponse."""
    if p.is_terminal:
        raise OrderStateError("annulation d'un ordre terminal", client_order_id=p.client_order_id)
    if p.pending_operation is not PendingOperation.NONE:
        raise OrderStateError(
            "une opération réseau est déjà en cours",
            client_order_id=p.client_order_id,
            pending_operation=p.pending_operation.value,
        )
    return replace(p, pending_operation=PendingOperation.CANCEL)


# --- événements du flux privé --------------------------------------------------------------------------


def _is_newer(event: OrderEvent, p: OrderProjection) -> bool:
    if event.event_ts is not None and p.last_event_ts is not None:
        return event.event_ts >= p.last_event_ts
    if p.last_receive_ts is None:
        return True
    return event.receive_ts >= p.last_receive_ts


def apply_event(p: OrderProjection, event: OrderEvent) -> TransitionResult:
    """Applique un événement du flux privé (ou de la réconciliation) à la projection."""
    if event.client_order_id != p.client_order_id:
        raise OrderStateError("événement d'un autre ordre", client_order_id=event.client_order_id)
    key = event_dedup_key(event)
    if key in p.applied_keys:
        return TransitionResult(p, changed=False, duplicate=True, ignored_reason="duplicate")

    anomalies: list[str] = []
    applied = p.applied_keys | {key}
    newer = _is_newer(event, p)
    increases_qty = event.cumulative_filled > p.cumulative_filled

    if event.cumulative_filled < p.cumulative_filled:
        anomalies.append("cumulative_below_projection")
    if not newer and not increases_qty:
        # Événement ancien sans information nouvelle : on note qu'il est vu, rien d'autre ne change.
        return TransitionResult(
            replace(p, applied_keys=applied),
            changed=False,
            ignored_reason="stale_event",
            anomalies=tuple(anomalies),
        )

    cumulative = max(p.cumulative_filled, event.cumulative_filled)
    avg_price = (
        event.average_fill_price if increases_qty and event.average_fill_price else p.average_fill_price
    )
    exchange_order_id = p.exchange_order_id or event.exchange_order_id
    if p.exchange_order_id and event.exchange_order_id and event.exchange_order_id != p.exchange_order_id:
        anomalies.append("exchange_order_id_mismatch")

    state = p.observed_state
    became_terminal = False
    event_state = event.observed_state
    if p.is_terminal:
        if (
            event_state is OrderState.FILLED
            and p.observed_state in (OrderState.CANCELED, OrderState.EXPIRED)
            and increases_qty
        ):
            state = OrderState.FILLED
            anomalies.append("filled_after_terminal")
        elif event_state is not p.observed_state and is_terminal(event_state):
            anomalies.append("terminal_state_conflict")
    elif is_terminal(event_state):
        state = event_state
        became_terminal = True
    elif event.event_kind is OrderEventKind.UNKNOWN:
        anomalies.append("unknown_event_kind")
    elif _RANK[event_state] >= _RANK[state] or state is OrderState.UNKNOWN:
        state = event_state
    if increases_qty and state in (OrderState.ACKNOWLEDGED, OrderState.SUBMITTED, OrderState.UNKNOWN):
        state = OrderState.PARTIALLY_FILLED if cumulative < p.contracts else OrderState.FILLED
        became_terminal = state is OrderState.FILLED
    if cumulative > p.contracts:
        anomalies.append("overfill")

    pending = p.pending_operation
    if pending is PendingOperation.SUBMIT:
        pending = PendingOperation.NONE  # l'exchange connaît l'ordre : l'opération SUBMIT est résolue
    if pending is PendingOperation.CANCEL and (
        is_terminal(state) or event.event_kind in (OrderEventKind.CANCEL, OrderEventKind.REJECT)
    ):
        pending = PendingOperation.NONE
    if pending is PendingOperation.AMEND and (is_terminal(state) or event.event_kind is OrderEventKind.AMEND):
        pending = PendingOperation.NONE

    terminal_at = p.terminal_at
    if is_terminal(state) and terminal_at is None:
        terminal_at = event.event_ts or event.receive_ts

    new = replace(
        p,
        observed_state=state,
        pending_operation=pending,
        cumulative_filled=cumulative,
        average_fill_price=avg_price,
        exchange_order_id=exchange_order_id,
        last_event_ts=event.event_ts if newer else p.last_event_ts,
        last_receive_ts=event.receive_ts if newer else p.last_receive_ts,
        terminal_at=terminal_at,
        needs_reconciliation=False if state is not OrderState.UNKNOWN else p.needs_reconciliation,
        applied_keys=applied,
    )
    return TransitionResult(
        new,
        changed=new != p,
        became_terminal=became_terminal and not p.is_terminal,
        anomalies=tuple(anomalies),
    )


def apply_provider_correction(
    p: OrderProjection,
    *,
    corrected_cumulative: Decimal,
    reason: str,
    reference: str,
    at: datetime,
) -> OrderProjection:
    """Correction fournisseur explicite (procédure opérateur tracée). Seule voie pour BAISSER la quantité."""
    if corrected_cumulative < 0 or corrected_cumulative > p.contracts:
        raise OrderStateError("correction hors bornes", client_order_id=p.client_order_id)
    if not reason or not reference:
        raise OrderStateError("une correction exige une raison et une référence fournisseur")
    trace = ProviderCorrection(
        corrected_at=at,
        previous_cumulative=p.cumulative_filled,
        corrected_cumulative=corrected_cumulative,
        reason=reason,
        reference=reference,
    )
    state = p.observed_state
    if corrected_cumulative < p.contracts and state is OrderState.FILLED:
        state = OrderState.PARTIALLY_FILLED if corrected_cumulative > 0 else OrderState.ACKNOWLEDGED
    return replace(
        p,
        cumulative_filled=corrected_cumulative,
        observed_state=state,
        corrections=(*p.corrections, trace),
        needs_reconciliation=True,
    )


def rebuild_projection(
    client_order_id: str, contracts: Decimal, events: Iterable[OrderEvent]
) -> OrderProjection:
    """Reconstruit la projection depuis le journal brut, ordonné par ``(receive_ts, event_id)``."""
    ordered = sorted(events, key=lambda e: (e.receive_ts, e.event_id))
    p = OrderProjection(client_order_id=client_order_id, contracts=contracts)
    if ordered:
        p = replace(p, observed_state=OrderState.SUBMITTED)
    for event in ordered:
        p = apply_event(p, event).projection
    return p
