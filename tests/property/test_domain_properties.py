"""Propriétés du domaine : conservation comptable des positions, arrondis, monnaie."""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from okxq.domain.money import Money, round_down_to_step, round_up_to_step
from okxq.domain.positions import Position, apply_fill

pytestmark = pytest.mark.property

qty = st.decimals(
    min_value=Decimal("0.001"), max_value=Decimal("100"), places=3, allow_nan=False, allow_infinity=False
)
price = st.decimals(
    min_value=Decimal("1"), max_value=Decimal("100000"), places=2, allow_nan=False, allow_infinity=False
)
signs = st.sampled_from([Decimal(1), Decimal(-1)])


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(signs, qty, price), min_size=1, max_size=30))
def test_position_conservation(fills):
    """PnL réalisé cumulé + PnL latent = somme des flux mark-to-market ; quantité = somme des fills."""
    pos = Position(inst_id="X", base_ccy="X", settle_ccy="USDT")
    total_realized = Decimal(0)
    net_qty = Decimal(0)
    cash_flow = Decimal(0)  # -signed_qty * price (achat = sortie de cash)
    for sign, q, p in fills:
        signed = sign * q
        pos, split = apply_fill(pos, signed, p)
        total_realized += split.realized_pnl
        net_qty += signed
        cash_flow -= signed * p
        assert split.reduce_qty >= 0 and split.open_qty >= 0
        assert split.reduce_qty + split.open_qty == q
    assert pos.signed_base_qty == net_qty
    assert pos.realized_pnl == total_realized
    last_price = fills[-1][2]
    # valeur de liquidation au dernier prix : cash accumulé + valeur de la position restante
    mark_value = cash_flow + pos.signed_base_qty * last_price
    assert mark_value == total_realized + pos.unrealized_pnl(last_price)


@settings(max_examples=200, deadline=None)
@given(
    st.decimals(
        min_value=Decimal("-1000"), max_value=Decimal("1000"), places=6, allow_nan=False, allow_infinity=False
    ),
    st.sampled_from([Decimal("1"), Decimal("0.1"), Decimal("0.01"), Decimal("0.5")]),
)
def test_round_down_is_toward_zero_and_on_grid(value, step):
    r = round_down_to_step(value, step)
    assert abs(r) <= abs(value)
    assert (r / step) % 1 == 0
    assert abs(value) - abs(r) < step
    u = round_up_to_step(value, step)
    assert abs(u) >= abs(value) and (u / step) % 1 == 0


@settings(max_examples=100, deadline=None)
@given(
    st.decimals(
        min_value=Decimal("-1e6"), max_value=Decimal("1e6"), places=8, allow_nan=False, allow_infinity=False
    ),
    st.decimals(
        min_value=Decimal("-1e6"), max_value=Decimal("1e6"), places=8, allow_nan=False, allow_infinity=False
    ),
)
def test_money_arithmetic_is_associative_and_reversible(a, b):
    x, y = Money.of(a, "USDT"), Money.of(b, "USDT")
    assert (x + y) - y == x
    assert (x + y).amount == a + b
    assert (-x).amount == -a
