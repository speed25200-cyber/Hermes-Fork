"""Politique d'exécution maker/taker versionnée (§29, §57).

- Entrée passive post-only bornée par ``passive_entry_max_wait_ms`` ; ensuite IOC si autorisé, sinon
  abandon ; au plus ``max_price_chase_attempts`` re-cotations ; jamais de market entry sans
  ``allow_market_entries`` ;
- mesures par instrument / régime / classe de taille : ratio de fills, fraction exécutée, temps au fill,
  sélection adverse après fill, rejets, spread payé, slippage, coût total ;
- les intentions non exécutées sont conservées (elles ne disparaissent pas des statistiques) ;
- chaque exécution enregistre decision_price, arrival_price, average_fill_price, délais, frais et
  implementation shortfall, tous estampillés ``execution_policy_version``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from okxq.config.schema import ExecutionCfg
from okxq.domain.errors import RiskRejectedError
from okxq.domain.money import Side, round_price_aggressive_within_limit, round_price_passive
from okxq.domain.orders import OrderKind

__all__ = [
    "EntryPlan",
    "EntryStep",
    "ExecutionMetrics",
    "ExecutionPolicy",
    "ExecutionRecord",
    "MetricsBucket",
    "PolicyStepOutcome",
    "TopOfBook",
    "UnexecutedIntent",
    "size_bucket",
]


class PolicyStepOutcome(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"
    REJECTED = "REJECTED"
    ABANDONED = "ABANDONED"


@dataclass(frozen=True, slots=True)
class TopOfBook:
    bid: Decimal
    ask: Decimal
    as_of: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class EntryStep:
    order_type: OrderKind
    price: Decimal
    max_wait: timedelta
    attempt: int


@dataclass(frozen=True, slots=True)
class EntryPlan:
    policy_version: str
    side: Side
    contracts: Decimal
    price_limit: Decimal
    decision_price: Decimal
    steps: tuple[EntryStep, ...]


class ExecutionPolicy:
    """Planification d'une entrée à partir de la configuration (pure ; l'exécution vit dans le gateway)."""

    def __init__(self, cfg: ExecutionCfg) -> None:
        self.cfg = cfg
        self.version = cfg.execution_policy_version

    def plan_entry(
        self,
        *,
        side: Side,
        contracts: Decimal,
        price_limit: Decimal,
        decision_price: Decimal,
        book: TopOfBook,
        tick_size: Decimal,
        reduce_only: bool = False,
    ) -> EntryPlan:
        """Post-only au meilleur prix passif (borné), re-cotations bornées, puis IOC ou abandon."""
        if contracts <= 0:
            raise RiskRejectedError("quantité non positive")
        allowed = set(self.cfg.allowed_entry_order_types)
        steps: list[EntryStep] = []
        passive_wait = timedelta(milliseconds=self.cfg.passive_entry_max_wait_ms)
        attempts = 1 + self.cfg.max_price_chase_attempts
        if "post_only" in allowed:
            for attempt in range(attempts):
                raw = book.bid if side is Side.BUY else book.ask
                price = round_price_passive(raw, tick_size, side)
                price = self._bound(price, price_limit, side)
                steps.append(
                    EntryStep(
                        order_type=OrderKind.POST_ONLY,
                        price=price,
                        max_wait=passive_wait / attempts,
                        attempt=attempt,
                    )
                )
        if "ioc" in allowed:
            raw = book.ask if side is Side.BUY else book.bid
            price = round_price_aggressive_within_limit(raw, tick_size, side, price_limit)
            steps.append(
                EntryStep(order_type=OrderKind.IOC, price=price, max_wait=timedelta(0), attempt=len(steps))
            )
        elif "limit" in allowed and not steps:
            price = self._bound(round_price_passive(book.mid, tick_size, side), price_limit, side)
            steps.append(EntryStep(order_type=OrderKind.LIMIT, price=price, max_wait=passive_wait, attempt=0))
        if not steps:
            raise RiskRejectedError("aucun type d'ordre d'entrée autorisé par la politique")
        return EntryPlan(
            policy_version=self.version,
            side=side,
            contracts=contracts,
            price_limit=price_limit,
            decision_price=decision_price,
            steps=tuple(steps),
        )

    def market_entry_allowed(self) -> bool:
        return self.cfg.allow_market_entries

    def order_kind_allowed_for_entry(self, kind: OrderKind) -> bool:
        if kind is OrderKind.MARKET:
            return self.cfg.allow_market_entries
        return kind.value in set(self.cfg.allowed_entry_order_types)

    @staticmethod
    def _bound(price: Decimal, limit: Decimal, side: Side) -> Decimal:
        if side is Side.BUY:
            return min(price, limit)
        return max(price, limit)

    def next_step(self, plan: EntryPlan, current_index: int, outcome: PolicyStepOutcome) -> EntryStep | None:
        """Après une étape : FILLED/REJECTED arrêtent ; UNFILLED/PARTIAL passent à l'étape suivante."""
        if outcome in (PolicyStepOutcome.FILLED, PolicyStepOutcome.REJECTED, PolicyStepOutcome.ABANDONED):
            return None
        nxt = current_index + 1
        return plan.steps[nxt] if nxt < len(plan.steps) else None


# --- mesures ----------------------------------------------------------------------------------------


def size_bucket(notional_usdt: Decimal) -> str:
    if notional_usdt < 1000:
        return "S"
    if notional_usdt < 10000:
        return "M"
    return "L"


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """Enregistrement d'une exécution (ou tentative) pour l'analyse des coûts (§57)."""

    intent_id: str
    inst_id: str
    regime: str
    side: Side
    contracts: Decimal
    filled_contracts: Decimal
    decision_price: Decimal
    arrival_price: Decimal
    average_fill_price: Decimal | None
    decision_at: datetime
    sent_at: datetime | None
    first_fill_at: datetime | None
    completed_at: datetime | None
    fees: Decimal
    spread_at_arrival: Decimal
    liquidity: str
    rejected: bool
    reject_reason: str | None
    policy_version: str
    notional_usdt: Decimal
    mid_after_horizon: Decimal | None = None

    @property
    def filled_fraction(self) -> Decimal:
        return Decimal(0) if self.contracts == 0 else self.filled_contracts / self.contracts

    @property
    def time_to_fill(self) -> timedelta | None:
        if self.sent_at is None or self.first_fill_at is None:
            return None
        return self.first_fill_at - self.sent_at

    @property
    def slippage_vs_decision(self) -> Decimal | None:
        """Signé : positif = exécution défavorable par rapport au prix de décision (par contrat)."""
        if self.average_fill_price is None:
            return None
        return self.side.sign * (self.average_fill_price - self.decision_price)

    @property
    def implementation_shortfall(self) -> Decimal | None:
        """Coût signé de la part exécutée (prix) + frais, par rapport au prix de décision (unité prix×contrat)."""
        if self.average_fill_price is None:
            return None
        price_cost = self.side.sign * (self.average_fill_price - self.decision_price) * self.filled_contracts
        return price_cost + self.fees

    @property
    def adverse_selection(self) -> Decimal | None:
        """Mouvement du mid après le fill dans le sens défavorable au maker (positif = adverse)."""
        if self.average_fill_price is None or self.mid_after_horizon is None:
            return None
        return -self.side.sign * (self.mid_after_horizon - self.average_fill_price)

    @property
    def spread_paid(self) -> Decimal | None:
        if self.average_fill_price is None:
            return None
        return self.side.sign * (self.average_fill_price - self.arrival_price)


@dataclass(frozen=True, slots=True)
class UnexecutedIntent:
    intent_id: str
    inst_id: str
    regime: str
    side: Side
    contracts: Decimal
    decision_price: Decimal
    reason: str
    decided_at: datetime
    policy_version: str


@dataclass(slots=True)
class MetricsBucket:
    orders: int = 0
    filled_orders: int = 0
    rejected: int = 0
    contracts_requested: Decimal = Decimal(0)
    contracts_filled: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    time_to_fill_total: timedelta = timedelta(0)
    time_to_fill_count: int = 0
    slippage_total: Decimal = Decimal(0)
    slippage_count: int = 0
    spread_paid_total: Decimal = Decimal(0)
    adverse_total: Decimal = Decimal(0)
    adverse_count: int = 0
    shortfall_total: Decimal = Decimal(0)
    unexecuted: int = 0

    def as_dict(self) -> dict[str, str | int]:
        def ratio(num: Decimal, den: Decimal) -> str:
            return "0" if den == 0 else format(num / den, "f")

        ttf = (
            "0"
            if self.time_to_fill_count == 0
            else format(Decimal(self.time_to_fill_total.total_seconds()) / self.time_to_fill_count, "f")
        )
        return {
            "orders": self.orders,
            "fill_ratio": ratio(Decimal(self.filled_orders), Decimal(self.orders)),
            "filled_fraction": ratio(self.contracts_filled, self.contracts_requested),
            "rejected": self.rejected,
            "unexecuted": self.unexecuted,
            "mean_time_to_fill_s": ttf,
            "mean_slippage": ratio(self.slippage_total, Decimal(self.slippage_count)),
            "mean_spread_paid": ratio(self.spread_paid_total, Decimal(self.slippage_count)),
            "mean_adverse_selection": ratio(self.adverse_total, Decimal(self.adverse_count)),
            "total_fees": format(self.fees, "f"),
            "total_shortfall": format(self.shortfall_total, "f"),
        }


class ExecutionMetrics:
    """Agrégation par (instrument, régime, classe de taille) ; conserve chaque enregistrement brut."""

    def __init__(self, policy_version: str) -> None:
        self.policy_version = policy_version
        self.records: list[ExecutionRecord] = []
        self.unexecuted: list[UnexecutedIntent] = []
        self._buckets: dict[tuple[str, str, str], MetricsBucket] = defaultdict(MetricsBucket)

    def record(self, rec: ExecutionRecord) -> None:
        if rec.policy_version != self.policy_version:
            raise ValueError("version de politique inattendue sur l'enregistrement")
        self.records.append(rec)
        b = self._buckets[(rec.inst_id, rec.regime, size_bucket(rec.notional_usdt))]
        b.orders += 1
        b.contracts_requested += rec.contracts
        b.contracts_filled += rec.filled_contracts
        b.fees += rec.fees
        if rec.rejected:
            b.rejected += 1
        if rec.filled_contracts > 0:
            if rec.filled_contracts >= rec.contracts:
                b.filled_orders += 1
            ttf = rec.time_to_fill
            if ttf is not None:
                b.time_to_fill_total += ttf
                b.time_to_fill_count += 1
            slip = rec.slippage_vs_decision
            spread = rec.spread_paid
            if slip is not None and spread is not None:
                b.slippage_total += slip
                b.spread_paid_total += spread
                b.slippage_count += 1
            adverse = rec.adverse_selection
            if adverse is not None:
                b.adverse_total += adverse
                b.adverse_count += 1
            shortfall = rec.implementation_shortfall
            if shortfall is not None:
                b.shortfall_total += shortfall

    def record_unexecuted(self, item: UnexecutedIntent) -> None:
        self.unexecuted.append(item)
        self._buckets[(item.inst_id, item.regime, "n/a")].unexecuted += 1

    def bucket(self, inst_id: str, regime: str, size: str) -> MetricsBucket:
        return self._buckets[(inst_id, regime, size)]

    def summary(self) -> dict[str, dict[str, str | int]]:
        return {"|".join(k): v.as_dict() for k, v in sorted(self._buckets.items())}
