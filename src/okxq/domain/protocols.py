"""Interfaces minimales (§43). Chaque interface possède un adaptateur de test et un adaptateur opérationnel."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from okxq.domain.events import (
    ApprovedOrder,
    Forecast,
    JevEvaluation,
    MarketSnapshot,
    OrderIntent,
    PortfolioInputs,
    PortfolioTarget,
    ReconciliationReport,
    RiskDecision,
    SourceDocument,
    SubmissionResult,
)


@runtime_checkable
class Clock(Protocol):
    def now_utc(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


@runtime_checkable
class FeatureProvider(Protocol):
    async def snapshot(self, cutoff_at: datetime) -> MarketSnapshot: ...


@runtime_checkable
class Predictor(Protocol):
    async def predict(self, snapshot: MarketSnapshot) -> list[Forecast]: ...


@runtime_checkable
class PortfolioBuilder(Protocol):
    def optimize(self, inputs: PortfolioInputs) -> PortfolioTarget: ...


@runtime_checkable
class RiskService(Protocol):
    async def evaluate(self, intent: OrderIntent) -> RiskDecision: ...


@runtime_checkable
class ExecutionGateway(Protocol):
    async def submit(self, approved: ApprovedOrder) -> SubmissionResult: ...

    async def reconcile(self) -> ReconciliationReport: ...


@runtime_checkable
class JevEventEvaluator(Protocol):
    async def evaluate(self, document: SourceDocument) -> JevEvaluation: ...
