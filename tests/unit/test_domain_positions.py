from decimal import Decimal

import pytest

from okxq.domain.errors import LedgerError
from okxq.domain.positions import Position, apply_fill


def pos() -> Position:
    return Position(inst_id="TEST-USDT-SWAP", base_ccy="TEST", settle_ccy="USDT")


def test_open_and_average():
    p, s = apply_fill(pos(), Decimal("0.010"), Decimal("10000"))
    assert p.signed_base_qty == Decimal("0.010") and s.open_qty == Decimal("0.010")
    p, _ = apply_fill(p, Decimal("0.010"), Decimal("11000"))
    assert p.average_entry_price == Decimal("10500")


def test_reduce_then_close_realizes_pnl():
    p, _ = apply_fill(pos(), Decimal("0.010"), Decimal("10000"))
    p, s = apply_fill(p, Decimal("-0.004"), Decimal("10100"))
    assert s.reduce_qty == Decimal("0.004") and s.realized_pnl == Decimal("0.4")
    p, s = apply_fill(p, Decimal("-0.006"), Decimal("10100"))
    assert p.is_flat and p.realized_pnl == Decimal("1.0") and s.realized_pnl == Decimal("0.6")
    assert p.average_entry_price == 0


def test_T39_long_to_short_flip_splits_close_and_open():
    p, _ = apply_fill(pos(), Decimal("0.010"), Decimal("10000"))
    p, s = apply_fill(p, Decimal("-0.015"), Decimal("9900"))
    assert s.flipped and s.reduce_qty == Decimal("0.010") and s.open_qty == Decimal("0.005")
    assert s.realized_pnl == Decimal("-1.0")
    assert p.signed_base_qty == Decimal("-0.005") and p.average_entry_price == Decimal("9900")
    assert p.unrealized_pnl(Decimal("9800")) == Decimal("0.5")


def test_invalid_fill():
    with pytest.raises(LedgerError):
        apply_fill(pos(), Decimal("0"), Decimal("1"))
    with pytest.raises(LedgerError):
        apply_fill(pos(), Decimal("1"), Decimal("0"))
