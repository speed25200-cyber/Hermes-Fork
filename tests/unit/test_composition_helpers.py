"""Fabriques partagées par les tests de câblage : un ``ApprovedOrder`` réel, pas une imitation.

Construire un ordre approuvé en passant par les vrais contrats garantit que le test échouerait si
l'approbation cessait d'être liée au hash du payload (T49).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from okxq.domain.events import ApprovedOrder, OrderIntent, RiskAction, RiskDecision
from okxq.domain.money import Side
from okxq.domain.orders import OrderKind

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def intent(**overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "intent_id": "int_test_1",
        "decision_id": "dec_test_1",
        "target_id": "tgt_test_1",
        "account_scope": "smoke-fixture",
        "inst_id": "BTC-USDT-SWAP",
        "side": Side.BUY,
        "contracts": Decimal("1"),
        "price_limit": Decimal("65000"),
        "order_type": OrderKind.IOC,
        "reduce_only": False,
        "ttl_ms": 2000,
        "reason": "TEST",
        "client_order_id": "cl_test_1",
        "created_at": T0,
        "expires_at": T0 + timedelta(milliseconds=2000),
    }
    base.update(overrides)
    return OrderIntent(**base)  # type: ignore[arg-type]


def approved_order() -> ApprovedOrder:
    """Approbation valide : le hash approuvé est celui du payload normalisé exact."""
    order_intent = intent()
    decision = RiskDecision(
        decision_id="rd_test_1",
        intent_id=order_intent.intent_id,
        intent_hash=order_intent.payload_hash(),
        action=RiskAction.ALLOW,
        allowed_payload_hash=order_intent.payload_hash(),
        allowed_contracts=order_intent.contracts,
        limits_version="limits-test",
        position_version="pos-test",
        created_at=T0,
        expires_at=T0 + timedelta(seconds=1),
        reason_codes=["OK"],
    )
    return ApprovedOrder(intent=order_intent, decision=decision, payload_hash=order_intent.payload_hash())
