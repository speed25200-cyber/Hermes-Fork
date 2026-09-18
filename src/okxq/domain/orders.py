"""Ordres : types, états observés, opérations réseau (§28, §52)."""

from __future__ import annotations

from enum import StrEnum

from okxq.domain.money import Side

__all__ = ["TERMINAL_STATES", "OrderKind", "OrderState", "PendingOperation", "Side", "is_terminal"]


class OrderKind(StrEnum):
    POST_ONLY = "post_only"
    LIMIT = "limit"
    IOC = "ioc"
    MARKET = "market"


class OrderState(StrEnum):
    """État OBSERVÉ de l'ordre (distinct de l'opération réseau en cours, §52.2)."""

    INTENT_CREATED = "INTENT_CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class PendingOperation(StrEnum):
    NONE = "none"
    SUBMIT = "submit"
    CANCEL = "cancel"
    AMEND = "amend"


TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}
)


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_STATES
