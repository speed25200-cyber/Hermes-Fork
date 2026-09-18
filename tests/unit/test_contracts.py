from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from okxq.domain.events import (
    ApprovedOrder,
    FeatureVector,
    OrderIntent,
    QualityFlag,
    RiskAction,
    RiskDecision,
)
from okxq.domain.money import Side
from okxq.domain.orders import OrderKind

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def intent(**over) -> OrderIntent:
    base = dict(
        intent_id="int_1",
        decision_id="dec_1",
        target_id="tgt_1",
        account_scope="paper-local",
        inst_id="TEST-USDT-SWAP",
        side=Side.BUY,
        contracts=Decimal("10"),
        price_limit=Decimal("10000"),
        order_type=OrderKind.POST_ONLY,
        reduce_only=False,
        ttl_ms=2000,
        reason="test",
        client_order_id="cl_1",
        created_at=T0,
        expires_at=T0 + timedelta(seconds=2),
    )
    base.update(over)
    return OrderIntent(**base)


def decision(i: OrderIntent, **over) -> RiskDecision:
    base = dict(
        decision_id="rd_1",
        intent_id=i.intent_id,
        intent_hash=i.payload_hash(),
        action=RiskAction.ALLOW,
        allowed_payload_hash=i.payload_hash(),
        allowed_contracts=i.contracts,
        limits_version="l1",
        position_version="p1",
        created_at=T0,
        expires_at=T0 + timedelta(seconds=1),
    )
    base.update(over)
    return RiskDecision(**base)


def test_feature_vector_masks_are_consistent():
    FeatureVector(
        instrument="X",
        cutoff_at=T0,
        available_at=T0,
        names=["a", "b"],
        values=[1.0, None],
        masks=[QualityFlag.OK, QualityFlag.MISSING],
        schema_hash="h",
    )
    with pytest.raises(ValidationError):
        FeatureVector(
            instrument="X",
            cutoff_at=T0,
            available_at=T0,
            names=["a"],
            values=[0.0],
            masks=[QualityFlag.MISSING],
            schema_hash="h",
        )
    with pytest.raises(ValidationError):
        FeatureVector(
            instrument="X",
            cutoff_at=T0,
            available_at=T0,
            names=["a"],
            values=[float("nan")],
            masks=[QualityFlag.OK],
            schema_hash="h",
        )


def test_intent_rules():
    with pytest.raises(ValidationError):
        intent(order_type=OrderKind.MARKET)
    with pytest.raises(ValidationError):
        intent(price_limit=None)
    with pytest.raises(ValidationError):
        intent(contracts=Decimal("0"))
    with pytest.raises(ValidationError):
        intent(extra_field=1)


def test_T49_modified_payload_invalidates_approval():
    i = intent()
    d = decision(i)
    ApprovedOrder(intent=i, decision=d, payload_hash=i.payload_hash())
    bigger = intent(contracts=Decimal("11"))
    with pytest.raises(ValidationError):
        ApprovedOrder(intent=bigger, decision=d, payload_hash=bigger.payload_hash())
    with pytest.raises(ValidationError):
        ApprovedOrder(intent=bigger, decision=d, payload_hash=i.payload_hash())


def test_reject_decision_cannot_carry_payload_and_cannot_be_approved():
    i = intent()
    with pytest.raises(ValidationError):
        decision(i, action=RiskAction.REJECT)
    d = decision(i, action=RiskAction.REJECT, allowed_payload_hash=None, allowed_contracts=None)
    with pytest.raises(ValidationError):
        ApprovedOrder(intent=i, decision=d, payload_hash=i.payload_hash())


def test_canonical_hash_is_stable_across_equal_objects():
    assert intent().payload_hash() == intent().payload_hash()
    assert intent().canonical_hash() == intent().canonical_hash()
