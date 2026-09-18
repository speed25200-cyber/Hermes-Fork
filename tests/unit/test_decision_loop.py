from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from okxq.domain.errors import CausalityError, OkxqError
from okxq.domain.events import (
    CostBasis,
    EdgeEstimate,
    FeatureVector,
    Forecast,
    MarketSnapshot,
    OrderIntent,
    PortfolioInputs,
    PortfolioTarget,
    PositionSide,
    QualityFlag,
    RiskAction,
    RiskDecision,
    SubmissionOutcome,
    SubmissionResult,
)
from okxq.domain.money import Side
from okxq.domain.orders import OrderKind
from okxq.domain.reasons import ReasonCode
from okxq.runtime.decision_loop import DecisionDeps, DecisionLoop, DecisionRecord

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INSTS = ["A-USDT-SWAP", "B-USDT-SWAP"]


def snapshot(cutoff: datetime, *, eligible=INSTS) -> MarketSnapshot:
    feats = {
        i: FeatureVector(
            instrument=i,
            cutoff_at=cutoff,
            available_at=cutoff,
            names=["x"],
            values=[1.0],
            masks=[QualityFlag.OK],
            schema_hash="h",
        )
        for i in eligible
    }
    return MarketSnapshot(
        snapshot_id="snap_1",
        cutoff_at=cutoff,
        universe_version="u1",
        metadata_version="m1",
        equity_version="e1",
        eligible_instruments=list(eligible),
        features=feats,
        reference_prices={i: Decimal("100") for i in eligible},
    )


class Gate:
    def __init__(self, reasons=None):
        self.reasons = reasons or []

    def blocking_reasons(self, cutoff_at):
        return list(self.reasons)


class Features:
    def __init__(self, snap):
        self.snap = snap

    async def snapshot(self, cutoff_at):
        return self.snap


class Predictor:
    def __init__(self, mu=0.001, fail=False, snapshot_id="snap_1"):
        self.mu, self.fail, self.sid = mu, fail, snapshot_id

    async def predict(self, snapshot):
        if self.fail:
            raise OkxqError("modèle absent", code="MODEL_UNAVAILABLE")
        return [
            Forecast(
                forecast_id=f"f_{i}",
                model_id="m_test",
                snapshot_id=self.sid,
                instrument=i,
                horizon_s=300,
                gross_mu=self.mu,
                uncertainty=0.0005,
                execution_policy_id="p1",
                cost_basis=CostBasis.MID,
                available_at=snapshot.cutoff_at,
            )
            for i in snapshot.eligible_instruments
        ]


class Edges:
    def __init__(self, net="0.0004"):
        self.net = Decimal(net)

    def build(self, snapshot, forecasts):
        return [
            EdgeEstimate(
                edge_id=f"e_{f.instrument}",
                forecast_id=f.forecast_id,
                instrument=f.instrument,
                side=PositionSide.LONG,
                horizon_s=300,
                cost_basis=CostBasis.MID,
                expected_gross_return=Decimal("0.001"),
                components={"fees": Decimal("-0.0005")},
                net_edge=self.net,
                uncertainty=Decimal("0.0005"),
                uncertainty_penalty=Decimal("0.0001"),
                edge_score=self.net - Decimal("0.0001"),
                expires_at=snapshot.cutoff_at + timedelta(seconds=60),
            )
            for f in forecasts
        ]


class Inputs:
    def build(self, snapshot, edges):
        if not edges:
            return None
        n = len(snapshot.eligible_instruments)
        return PortfolioInputs(
            instruments=list(snapshot.eligible_instruments),
            mu=[0.001] * n,
            sigma=[[0.0001 if i == j else 0.0 for j in range(n)] for i in range(n)],
            w0=[0.0] * n,
            cost_buy=[0.0005] * n,
            cost_sell=[0.0005] * n,
            uncertainty_penalty=[0.0001] * n,
            expected_funding_cost=[0.0] * n,
            future_exit_cost=[0.0005] * n,
            asset_limit=[0.1] * n,
            liquidity_capacity=[0.05] * n,
            beta_btc=[1.0] * n,
            beta_eth=[0.0] * n,
            clusters={},
            horizon_s=300,
            risk_aversion=1.0,
            gross_limit=1.0,
            net_limit=0.1,
            btc_beta_limit=0.1,
            eth_beta_limit=0.1,
            cluster_limit=0.3,
            turnover_limit=0.3,
            margin_capacity=0.5,
            margin_requirement_per_unit=[0.1] * n,
            equity_version="e1",
            snapshot_id="snap_1",
            constraints_version="c1",
        )


class Portfolio:
    def __init__(self, clock, weight="0.05", expired=False):
        self.clock, self.weight, self.expired = clock, Decimal(weight), expired

    def optimize(self, inputs):
        now = self.clock.now_utc()
        return PortfolioTarget(
            target_id="t1",
            snapshot_id=inputs.snapshot_id,
            equity_version="e1",
            signed_weights={i: self.weight for i in inputs.instruments},
            constraints_version="c1",
            solver_status="optimal",
            created_at=now,
            expires_at=now - timedelta(seconds=1) if self.expired else now + timedelta(seconds=30),
        )


class Intents:
    def __init__(self, clock, n=1):
        self.clock, self.n = clock, n

    def build(self, snapshot, target, decision_id):
        now = self.clock.now_utc()
        return [
            OrderIntent(
                intent_id=f"int_{k}",
                decision_id=decision_id,
                target_id=target.target_id,
                account_scope="paper",
                inst_id=snapshot.eligible_instruments[k],
                side=Side.BUY,
                contracts=Decimal("5"),
                price_limit=Decimal("100"),
                order_type=OrderKind.POST_ONLY,
                reduce_only=False,
                ttl_ms=2000,
                reason="test",
                client_order_id=f"cl_{k}",
                created_at=now,
                expires_at=now + timedelta(seconds=2),
            )
            for k in range(self.n)
        ]


class Risk:
    def __init__(self, clock, action=RiskAction.ALLOW, reduce_to=None):
        self.clock, self.action, self.reduce_to = clock, action, reduce_to
        self.seen = []

    async def evaluate(self, intent):
        self.seen.append(intent)
        now = self.clock.now_utc()
        if self.action is RiskAction.REJECT:
            return RiskDecision(
                decision_id="rd",
                intent_id=intent.intent_id,
                intent_hash=intent.payload_hash(),
                action=RiskAction.REJECT,
                allowed_payload_hash=None,
                limits_version="l",
                position_version="p",
                created_at=now,
                expires_at=now + timedelta(seconds=1),
                reason_codes=["RISK_LIMIT"],
            )
        if self.action is RiskAction.REDUCE:
            reduced = intent.model_copy(update={"contracts": self.reduce_to})
            return RiskDecision(
                decision_id="rd",
                intent_id=intent.intent_id,
                intent_hash=intent.payload_hash(),
                action=RiskAction.REDUCE,
                allowed_payload_hash=reduced.payload_hash(),
                allowed_contracts=self.reduce_to,
                limits_version="l",
                position_version="p",
                created_at=now,
                expires_at=now + timedelta(seconds=1),
            )
        return RiskDecision(
            decision_id="rd",
            intent_id=intent.intent_id,
            intent_hash=intent.payload_hash(),
            action=RiskAction.ALLOW,
            allowed_payload_hash=intent.payload_hash(),
            allowed_contracts=intent.contracts,
            limits_version="l",
            position_version="p",
            created_at=now,
            expires_at=now + timedelta(seconds=1),
        )


class Gateway:
    def __init__(self):
        self.submitted = []

    async def submit(self, approved):
        self.submitted.append(approved)
        return SubmissionResult(
            intent_id=approved.intent.intent_id,
            client_order_id=approved.intent.client_order_id,
            outcome=SubmissionOutcome.ACK,
            exchange_order_id="x1",
        )

    async def reconcile(self):
        raise NotImplementedError


class Sink:
    def __init__(self):
        self.records: list[DecisionRecord] = []

    def persist(self, record):
        self.records.append(record)


def make(clock, **over):
    gw = Gateway()
    sink = Sink()
    base = dict(
        clock=clock,
        mode="PAPER",
        minimum_eligible=2,
        gate=Gate(),
        feature_provider=Features(snapshot(T0)),
        predictor=Predictor(),
        edge_builder=Edges(),
        inputs_builder=Inputs(),
        portfolio_builder=Portfolio(clock),
        intent_builder=Intents(clock),
        risk_service=Risk(clock),
        gateway=gw,
        sink=sink,
    )
    base.update(over)
    return DecisionLoop(DecisionDeps(**base)), gw, sink


async def test_full_trade_path_persists_everything(clock):
    loop, gw, _sink = make(clock)
    rec = await loop.run_once(T0, T0 + timedelta(seconds=3))
    assert rec.outcome == "TRADE" and len(gw.submitted) == 1
    assert _sink.records[-1] is rec and rec.reason_codes == ["OK"]
    assert set(rec.timings_ms) >= {
        "gate",
        "snapshot",
        "inference",
        "edges",
        "optimizer",
        "intents",
        "execution",
    }
    assert gw.submitted[0].payload_hash == gw.submitted[0].intent.payload_hash()


async def test_gate_blocks_before_any_computation(clock):
    loop, gw, _sink = make(clock, gate=Gate([ReasonCode.HALTED]))
    rec = await loop.run_once(T0, T0 + timedelta(seconds=3))
    assert rec.outcome == "NO_TRADE" and rec.reason_codes == ["HALTED"] and not gw.submitted


async def test_universe_insufficient_is_a_valid_no_trade(clock):
    loop, _gw, _sink = make(clock, feature_provider=Features(snapshot(T0, eligible=INSTS[:1])))
    rec = await loop.run_once(T0, T0 + timedelta(seconds=3))
    assert rec.outcome == "NO_TRADE" and "UNIVERSE_INSUFFICIENT" in rec.reason_codes


async def test_model_unavailable_never_sends(clock):
    loop, gw, _sink = make(clock, predictor=Predictor(fail=True))
    rec = await loop.run_once(T0, T0 + timedelta(seconds=3))
    assert rec.outcome == "NO_TRADE" and "MODEL_UNAVAILABLE" in rec.reason_codes and not gw.submitted


async def test_edge_below_costs_recorded_as_rejected_alternative(clock):
    loop, _gw, _sink = make(clock, edge_builder=Edges(net="-0.0002"))
    rec = await loop.run_once(T0, T0 + timedelta(seconds=3))
    assert rec.outcome == "NO_TRADE"
    assert {a["reason"] for a in rec.rejected_alternatives} == {"EDGE_BELOW_COSTS"}


async def test_risk_reject_and_reduce(clock):
    loop, gw, _sink = make(clock, risk_service=Risk(clock, RiskAction.REJECT))
    rec = await loop.run_once(T0, T0 + timedelta(seconds=3))
    assert (
        rec.outcome == "NO_TRADE"
        and not gw.submitted
        and rec.rejected_alternatives[0]["reason"] == "RISK_LIMIT"
    )
    loop, gw, _sink = make(clock, risk_service=Risk(clock, RiskAction.REDUCE, reduce_to=Decimal("2")))
    rec = await loop.run_once(T0, T0 + timedelta(seconds=3))
    assert rec.outcome == "TRADE" and gw.submitted[0].intent.contracts == Decimal("2")


async def test_expired_target_or_deadline_skips_without_orders(clock):
    loop, gw, _sink = make(clock, portfolio_builder=Portfolio(clock, expired=True))
    rec = await loop.run_once(T0, T0 + timedelta(seconds=3))
    assert rec.outcome == "SKIPPED" and not gw.submitted

    class SlowPredictor(Predictor):
        async def predict(self, snapshot):
            clock.advance(timedelta(seconds=10))
            return await super().predict(snapshot)

    loop, gw, _sink = make(clock, predictor=SlowPredictor())
    rec = await loop.run_once(clock.now_utc(), clock.now_utc() + timedelta(seconds=3))
    assert rec.outcome == "SKIPPED" and "SNAPSHOT_EXPIRED" in rec.reason_codes and not gw.submitted


async def test_causality_violations_fail_the_decision(clock):
    loop, gw, _sink = make(clock, predictor=Predictor(snapshot_id="other"))
    rec = await loop.run_once(T0, T0 + timedelta(seconds=3))
    assert rec.outcome == "FAILED" and "CAUSALITY_VIOLATION" in rec.reason_codes and not gw.submitted
    loop, gw, _sink = make(clock)
    with pytest.raises(CausalityError):
        await loop.run_once(T0 + timedelta(minutes=5), T0 + timedelta(minutes=5, seconds=3))
