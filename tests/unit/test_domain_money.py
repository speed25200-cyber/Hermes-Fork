from decimal import Decimal

import pytest

from okxq.domain.errors import RoundingError, UnitError
from okxq.domain.money import (
    Contracts,
    Money,
    Side,
    dec,
    dec_from_float,
    is_multiple_of,
    round_down_to_step,
    round_price_aggressive_within_limit,
    round_price_passive,
    round_up_to_step,
)


def test_dec_refuses_floats_and_non_finite():
    with pytest.raises(UnitError):
        dec(0.1)  # type: ignore[arg-type]
    with pytest.raises(UnitError):
        dec("nan")
    with pytest.raises(UnitError):
        dec(True)  # type: ignore[arg-type]
    assert dec("1.50") == Decimal("1.50")


def test_dec_from_float_quantizes_explicitly():
    assert dec_from_float(0.1 + 0.2, 8) == Decimal("0.30000000")
    with pytest.raises(UnitError):
        dec_from_float(float("inf"), 4)


def test_money_currency_safety():
    a = Money.of("1.5", "USDT")
    b = Money.of("2", "USDT")
    assert (a + b).amount == Decimal("3.5")
    with pytest.raises(UnitError):
        _ = a + Money.of("1", "BTC")
    with pytest.raises(UnitError):
        Money.of("1", "usdt")


def test_round_down_to_step_moves_toward_zero_for_both_signs():
    assert round_down_to_step(Decimal("10.7"), Decimal("1")) == Decimal("10")
    assert round_down_to_step(Decimal("-10.7"), Decimal("1")) == Decimal("-10")
    assert round_up_to_step(Decimal("10.1"), Decimal("0.5")) == Decimal("10.5")
    assert is_multiple_of(Decimal("0.3"), Decimal("0.1"))
    assert not is_multiple_of(Decimal("0.35"), Decimal("0.1"))


def test_passive_price_rounding_never_crosses_book():
    tick = Decimal("0.1")
    assert round_price_passive(Decimal("100.06"), tick, Side.BUY) == Decimal("100.0")
    assert round_price_passive(Decimal("100.01"), tick, Side.SELL) == Decimal("100.1")


def test_aggressive_price_respects_preapproved_limit():
    tick = Decimal("0.1")
    assert round_price_aggressive_within_limit(
        Decimal("100.04"), tick, Side.BUY, Decimal("100.1")
    ) == Decimal("100.1")
    assert round_price_aggressive_within_limit(
        Decimal("100.16"), tick, Side.BUY, Decimal("100.1")
    ) == Decimal("100.1")
    # limite hors grille : le prix retenu reste sous la limite, sur la grille
    assert round_price_aggressive_within_limit(
        Decimal("100.16"), tick, Side.BUY, Decimal("100.05")
    ) == Decimal("100.0")
    assert round_price_aggressive_within_limit(
        Decimal("99.91"), tick, Side.SELL, Decimal("99.95")
    ) == Decimal("100.0")
    with pytest.raises(RoundingError):  # aucun prix positif admissible
        round_price_aggressive_within_limit(Decimal("0.16"), tick, Side.BUY, Decimal("0.05"))


def test_contracts_sign():
    assert Contracts(Decimal("-3")).sign == -1
    assert Contracts(Decimal("0")).is_zero()
