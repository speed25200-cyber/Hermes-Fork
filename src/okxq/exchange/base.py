"""Interface commune des adaptateurs d'exchange (§32) : OKX réel/DEMO et exchange virtuel de backtest.

Le gateway d'exécution (``okxq.execution.gateway``) ne parle qu'à cette interface. Le backtester remplace
l'adaptateur et l'horloge ; la logique de stratégie, de risque et de comptabilité est la même partout.

Sémantique importante :
- ``place_order`` retourne une réponse de RÉCEPTION (ACK/REJECT/UNKNOWN). Un ACK ne prouve pas une
  exécution ; ``UNKNOWN`` (timeout, coupure) impose une réconciliation avant tout renvoi (§48, §52).
- Les événements d'ordres et fills arrivent de façon asynchrone via ``events()`` ; ils peuvent être
  dupliqués, tardifs, ou précéder l'ACK local (§52.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from okxq.domain.events import Fill, OrderEvent
from okxq.domain.instruments import InstrumentSpec


class PlaceOutcome(StrEnum):
    ACK = "ACK"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Payload normalisé d'ordre — identique à ``OrderIntent.normalized_payload()``."""

    account_scope: str
    client_order_id: str
    inst_id: str
    side: str  # buy / sell
    contracts: Decimal
    price_limit: Decimal | None
    order_type: str  # post_only / limit / ioc / market
    reduce_only: bool
    fencing_token: int = 0


@dataclass(frozen=True, slots=True)
class PlaceResponse:
    outcome: PlaceOutcome
    client_order_id: str
    exchange_order_id: str | None = None
    code: str | None = None
    message: str | None = None
    sent_at: datetime | None = None
    ack_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CancelResponse:
    outcome: PlaceOutcome  # ACK = demande reçue ; l'annulation effective se lit dans les événements
    client_order_id: str
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class OrderStatus:
    client_order_id: str
    exchange_order_id: str | None
    state: str  # live / partially_filled / filled / canceled / rejected / expired / unknown
    filled_contracts: Decimal
    average_price: Decimal | None
    updated_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PositionStatus:
    inst_id: str
    signed_contracts: Decimal
    average_price: Decimal
    mark_price: Decimal | None
    liquidation_price: Decimal | None
    margin: Decimal | None
    leverage: Decimal | None
    as_of: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BalanceStatus:
    settle_ccy: str
    total_equity: Decimal
    cash_balance: Decimal
    available: Decimal
    used_margin: Decimal | None
    unrealized_pnl: Decimal | None
    as_of: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProtectionRequest:
    """Ordre conditionnel de protection (stop) reduce-only, confirmé côté exchange ou pas du tout (§54)."""

    account_scope: str
    client_algo_id: str
    inst_id: str
    side: str
    contracts: Decimal
    trigger_price: Decimal
    trigger_reference: str  # mark / last
    reduce_only: bool = True


@dataclass(frozen=True, slots=True)
class ProtectionStatus:
    client_algo_id: str
    exchange_algo_id: str | None
    state: str  # live / triggered / canceled / rejected / unknown
    trigger_price: Decimal | None
    contracts: Decimal | None
    as_of: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExchangeEvent:
    """Événement asynchrone du flux privé : ordre, fill, position, compte, protection."""

    kind: str  # order / fill / position / balance / protection / heartbeat / disconnect
    receive_ts: datetime
    order_event: OrderEvent | None = None
    fill: Fill | None = None
    position: PositionStatus | None = None
    balance: BalanceStatus | None = None
    protection: ProtectionStatus | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExchangeAdapter(Protocol):
    """Contrat minimal ; chaque implémentation documente ses limites (capability manifest)."""

    @property
    def name(self) -> str: ...

    async def instruments(self) -> list[InstrumentSpec]: ...

    async def place_order(self, request: OrderRequest) -> PlaceResponse: ...

    async def cancel_order(
        self, account_scope: str, client_order_id: str, inst_id: str
    ) -> CancelResponse: ...

    async def cancel_all_after(self, timeout_seconds: int) -> bool: ...

    async def get_order(
        self, account_scope: str, client_order_id: str, inst_id: str
    ) -> OrderStatus | None: ...

    async def open_orders(self, account_scope: str) -> list[OrderStatus]: ...

    async def fills_since(self, account_scope: str, since: datetime) -> list[Fill]: ...

    async def positions(self, account_scope: str) -> list[PositionStatus]: ...

    async def balance(self, account_scope: str) -> BalanceStatus: ...

    async def place_protection(self, request: ProtectionRequest) -> ProtectionStatus: ...

    async def cancel_protection(
        self, account_scope: str, client_algo_id: str, inst_id: str
    ) -> ProtectionStatus: ...

    async def protections(self, account_scope: str) -> list[ProtectionStatus]: ...

    def events(self) -> AsyncIterator[ExchangeEvent]: ...
