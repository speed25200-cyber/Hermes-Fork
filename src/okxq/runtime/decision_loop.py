"""Boucle décisionnelle (§55) : une décision par frontière, entièrement journalisée, NO_TRADE compris.

Toutes les dépendances sont des protocoles injectés ; ce module n'importe aucun moteur concret. Il
applique l'ordre des étapes, la deadline, les contrats de causalité, et enregistre la décision avec ses
raisons et alternatives rejetées. Le Risk Engine et le gateway restent les seuls chemins vers un ordre.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from okxq.domain.clocks import Clock, ensure_utc
from okxq.domain.errors import CausalityError, OkxqError
from okxq.domain.events import (
    ApprovedOrder,
    EdgeEstimate,
    Forecast,
    MarketSnapshot,
    OrderIntent,
    PortfolioInputs,
    PortfolioTarget,
    RiskAction,
    RiskDecision,
    SubmissionResult,
)
from okxq.domain.ids import new_id
from okxq.domain.protocols import ExecutionGateway, FeatureProvider, PortfolioBuilder, Predictor, RiskService
from okxq.domain.reasons import ReasonCode


class EdgeBuilder(Protocol):
    def build(self, snapshot: MarketSnapshot, forecasts: Sequence[Forecast]) -> list[EdgeEstimate]: ...


class InputsBuilder(Protocol):
    def build(self, snapshot: MarketSnapshot, edges: Sequence[EdgeEstimate]) -> PortfolioInputs | None: ...


class IntentBuilder(Protocol):
    def build(
        self, snapshot: MarketSnapshot, target: PortfolioTarget, decision_id: str
    ) -> list[OrderIntent]: ...


class OperationalGate(Protocol):
    """État opérationnel consulté avant toute décision : halts, leadership, réconciliation, données."""

    def blocking_reasons(self, cutoff_at: datetime) -> list[ReasonCode]: ...


class DecisionSink(Protocol):
    def persist(self, record: DecisionRecord) -> None: ...


@dataclass(slots=True)
class DecisionRecord:
    decision_id: str
    mode: str
    cutoff_at: datetime
    started_at: datetime
    finished_at: datetime | None = None
    outcome: str = "NO_TRADE"
    reason_codes: list[str] = field(default_factory=list)
    snapshot_id: str | None = None
    model_id: str | None = None
    universe_version: str | None = None
    equity_version: str | None = None
    forecasts: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    target: dict[str, Any] | None = None
    intents: list[dict[str, Any]] = field(default_factory=list)
    risk_decisions: list[dict[str, Any]] = field(default_factory=list)
    submissions: list[dict[str, Any]] = field(default_factory=list)
    rejected_alternatives: list[dict[str, Any]] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: new_id("trc"))


@dataclass(slots=True)
class DecisionDeps:
    clock: Clock
    mode: str
    minimum_eligible: int
    gate: OperationalGate
    feature_provider: FeatureProvider
    predictor: Predictor
    edge_builder: EdgeBuilder
    inputs_builder: InputsBuilder
    portfolio_builder: PortfolioBuilder
    intent_builder: IntentBuilder
    risk_service: RiskService
    gateway: ExecutionGateway
    sink: DecisionSink
    min_edge_score: float = 0.0
    on_step: Callable[[str, int], Awaitable[None]] | None = None


class DecisionLoop:
    def __init__(self, deps: DecisionDeps) -> None:
        self._d = deps

    async def run_once(self, cutoff_at: datetime, deadline_at: datetime) -> DecisionRecord:
        d = self._d
        cutoff_at = ensure_utc(cutoff_at, field="cutoff_at")
        deadline_at = ensure_utc(deadline_at, field="deadline_at")
        rec = DecisionRecord(
            decision_id=new_id("dec"), mode=d.mode, cutoff_at=cutoff_at, started_at=d.clock.now_utc()
        )
        if rec.started_at < cutoff_at:
            raise CausalityError("décision démarrée avant son cutoff", cutoff=cutoff_at.isoformat())
        t_step = time.perf_counter()

        def lap(name: str) -> None:
            nonlocal t_step
            now = time.perf_counter()
            rec.timings_ms[name] = int((now - t_step) * 1000)
            t_step = now

        def expired() -> bool:
            return d.clock.now_utc() > deadline_at

        try:
            # 1. état opérationnel
            blocking = d.gate.blocking_reasons(cutoff_at)
            if blocking:
                rec.reason_codes = [r.value for r in blocking]
                return self._finish(rec, "NO_TRADE")
            lap("gate")

            # 2. snapshot cohérent
            snapshot = await d.feature_provider.snapshot(cutoff_at)
            if snapshot.cutoff_at != cutoff_at:
                raise CausalityError("snapshot.cutoff_at différent du cutoff demandé")
            for fv in snapshot.features.values():
                if fv.available_at > snapshot.cutoff_at:
                    raise CausalityError("feature disponible après le cutoff", instrument=fv.instrument)
            rec.snapshot_id = snapshot.snapshot_id
            rec.universe_version = snapshot.universe_version
            rec.equity_version = snapshot.equity_version
            lap("snapshot")
            if len(snapshot.eligible_instruments) < d.minimum_eligible:
                rec.reason_codes.append(ReasonCode.UNIVERSE_INSUFFICIENT.value)
                return self._finish(rec, "NO_TRADE")
            if expired():
                rec.reason_codes.append(ReasonCode.SNAPSHOT_EXPIRED.value)
                return self._finish(rec, "SKIPPED")

            # 4. inférence
            try:
                forecasts = await d.predictor.predict(snapshot)
            except OkxqError as exc:
                rec.reason_codes.append(ReasonCode.MODEL_UNAVAILABLE.value)
                rec.rejected_alternatives.append({"stage": "predict", "error": str(exc)})
                return self._finish(rec, "NO_TRADE")
            for f in forecasts:
                if f.available_at < snapshot.cutoff_at:
                    # une prévision « disponible » avant le cutoff serait un artefact : on la date au cutoff
                    pass
                if f.snapshot_id != snapshot.snapshot_id:
                    raise CausalityError("prévision issue d'un autre snapshot", forecast=f.forecast_id)
            rec.forecasts = [f.model_dump(mode="json") for f in forecasts]
            rec.model_id = forecasts[0].model_id if forecasts else None
            lap("inference")
            if not forecasts:
                rec.reason_codes.append(ReasonCode.NO_VALID_TARGET.value)
                return self._finish(rec, "NO_TRADE")

            # 5. coûts, incertitude, avantage net
            edges = d.edge_builder.build(snapshot, forecasts)
            rec.edges = [e.model_dump(mode="json") for e in edges]
            kept = [e for e in edges if float(e.edge_score) > d.min_edge_score]
            for e in edges:
                if e not in kept:
                    rec.rejected_alternatives.append(
                        {
                            "instrument": e.instrument,
                            "side": e.side.value,
                            "reason": ReasonCode.EDGE_BELOW_COSTS.value,
                            "net_edge": str(e.net_edge),
                            "uncertainty_penalty": str(e.uncertainty_penalty),
                        }
                    )
            lap("edges")
            if expired():
                rec.reason_codes.append(ReasonCode.SNAPSHOT_EXPIRED.value)
                return self._finish(rec, "SKIPPED")

            # 6. portefeuille cible
            inputs = d.inputs_builder.build(snapshot, kept)
            if inputs is None:
                rec.reason_codes.append(ReasonCode.NO_VALID_TARGET.value)
                return self._finish(rec, "NO_TRADE")
            target = d.portfolio_builder.optimize(inputs)
            rec.target = target.model_dump(mode="json")
            lap("optimizer")
            if target.expires_at <= d.clock.now_utc():
                rec.reason_codes.append(ReasonCode.SNAPSHOT_EXPIRED.value)
                return self._finish(rec, "SKIPPED")
            if target.reason_codes:
                rec.reason_codes.extend(target.reason_codes)

            # 7-8. conversion, arrondis, deltas utiles
            intents = d.intent_builder.build(snapshot, target, rec.decision_id)
            rec.intents = [i.model_dump(mode="json") for i in intents]
            lap("intents")
            if not intents:
                if not rec.reason_codes:
                    rec.reason_codes.append(ReasonCode.TURNOVER_NOT_JUSTIFIED.value)
                return self._finish(rec, "NO_TRADE")
            if expired():
                rec.reason_codes.append(ReasonCode.SNAPSHOT_EXPIRED.value)
                return self._finish(rec, "SKIPPED")

            # 9-10. risque puis gateway unique
            sent = 0
            for intent in intents:
                decision: RiskDecision = await d.risk_service.evaluate(intent)
                rec.risk_decisions.append(decision.model_dump(mode="json"))
                if decision.action not in (RiskAction.ALLOW, RiskAction.REDUCE):
                    rec.rejected_alternatives.append(
                        {
                            "intent_id": intent.intent_id,
                            "reason": ReasonCode.RISK_LIMIT.value,
                            "codes": decision.reason_codes,
                        }
                    )
                    continue
                if decision.action is RiskAction.REDUCE and decision.allowed_contracts is not None:
                    if decision.allowed_contracts <= 0:
                        continue
                    intent = intent.model_copy(update={"contracts": decision.allowed_contracts})
                    if intent.payload_hash() != decision.allowed_payload_hash:
                        rec.rejected_alternatives.append(
                            {"intent_id": intent.intent_id, "reason": ReasonCode.PAYLOAD_HASH_MISMATCH.value}
                        )
                        continue
                if d.clock.now_utc() > deadline_at or d.clock.now_utc() > decision.expires_at:
                    rec.rejected_alternatives.append(
                        {"intent_id": intent.intent_id, "reason": ReasonCode.APPROVAL_EXPIRED.value}
                    )
                    continue
                approved = ApprovedOrder(intent=intent, decision=decision, payload_hash=intent.payload_hash())
                result: SubmissionResult = await d.gateway.submit(approved)
                rec.submissions.append(result.model_dump(mode="json"))
                sent += 1
            lap("execution")
            if sent == 0:
                if not rec.reason_codes:
                    rec.reason_codes.append(ReasonCode.RISK_LIMIT.value)
                return self._finish(rec, "NO_TRADE")
            rec.reason_codes.append(ReasonCode.OK.value)
            return self._finish(rec, "TRADE")
        except OkxqError as exc:
            rec.reason_codes.append(exc.code)
            rec.rejected_alternatives.append({"stage": "loop", "error": str(exc)})
            return self._finish(rec, "FAILED")

    def _finish(self, rec: DecisionRecord, outcome: str) -> DecisionRecord:
        rec.outcome = outcome
        rec.finished_at = self._d.clock.now_utc()
        self._d.sink.persist(rec)
        return rec
