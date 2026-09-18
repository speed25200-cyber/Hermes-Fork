from datetime import UTC, datetime
from decimal import Decimal

import pytest

from okxq.domain.errors import RoundingError, UnsupportedInstrumentError
from okxq.domain.instruments import (
    contracts_to_base,
    contracts_to_notional,
    raw_target_contracts,
    round_contracts_risk_reducing,
    spec_from_okx_instrument,
    validate_order_size,
)
from okxq.domain.money import Contracts, Money

OBS = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def okx_payload(**over):
    base = {
        "instType": "SWAP",
        "instId": "BTC-USDT-SWAP",
        "uly": "BTC-USDT",
        "instFamily": "BTC-USDT",
        "baseCcy": "",
        "quoteCcy": "",
        "settleCcy": "USDT",
        "ctVal": "0.01",
        "ctMult": "1",
        "ctValCcy": "BTC",
        "optType": "",
        "stk": "",
        "listTime": "1611916800000",
        "expTime": "",
        "lever": "100",
        "tickSz": "0.1",
        "lotSz": "0.1",
        "minSz": "0.1",
        "ctType": "linear",
        "alias": "",
        "state": "live",
        "maxLmtSz": "100000000",
    }
    base.update(over)
    return base


def test_T01_contract_base_notional_conversions(test_spec):
    c = Contracts(Decimal("10"))
    assert contracts_to_base(c, test_spec).value == Decimal("0.010")
    n = contracts_to_notional(c, test_spec, Decimal("10000"))
    assert n == Money.of("100", "USDT")
    s = Contracts(Decimal("-10"))
    assert contracts_to_notional(s, test_spec, Decimal("10000")).amount == Decimal("-100")
    assert raw_target_contracts(
        Decimal("0.05"), Money.of("100000", "USDT"), test_spec, Decimal("10000")
    ) == Decimal("500")


def test_T02_inverse_options_and_foreign_settlement_are_rejected():
    with pytest.raises(UnsupportedInstrumentError):
        spec_from_okx_instrument(
            okx_payload(ctType="inverse", instId="BTC-USD-SWAP", settleCcy="BTC", ctValCcy="USD"),
            observed_at=OBS,
            provenance="t",
        )
    with pytest.raises(UnsupportedInstrumentError):
        spec_from_okx_instrument(okx_payload(instType="OPTION"), observed_at=OBS, provenance="t")
    with pytest.raises(UnsupportedInstrumentError):
        spec_from_okx_instrument(
            okx_payload(settleCcy="USDC", instId="BTC-USDC-SWAP"), observed_at=OBS, provenance="t"
        )
    with pytest.raises(UnsupportedInstrumentError):
        spec_from_okx_instrument(okx_payload(ctMult="2"), observed_at=OBS, provenance="t")
    with pytest.raises(UnsupportedInstrumentError):
        spec_from_okx_instrument(okx_payload(ctValCcy="USDT"), observed_at=OBS, provenance="t")


def test_okx_linear_swap_is_accepted_with_v_equal_ctval():
    spec = spec_from_okx_instrument(okx_payload(), observed_at=OBS, provenance="fixture")
    assert spec.base_units_per_contract == Decimal("0.01")
    assert spec.base_ccy == "BTC"
    assert spec.is_tradable
    assert spec.valid_from < OBS


def test_T03_lot_and_min_size_rounding_and_validation(btc_spec):
    assert round_contracts_risk_reducing(Decimal("1.27"), btc_spec).value == Decimal("1.2")
    assert round_contracts_risk_reducing(Decimal("-1.27"), btc_spec).value == Decimal("-1.2")
    assert (
        round_contracts_risk_reducing(Decimal("0.05"), btc_spec).value == 0
    )  # sous le minimum : zéro, pas min_size
    with pytest.raises(RoundingError):
        validate_order_size(Contracts(Decimal("0.15")), btc_spec)
    with pytest.raises(RoundingError):
        validate_order_size(Contracts(Decimal("0")), btc_spec)
    validate_order_size(Contracts(Decimal("0.3")), btc_spec)


def test_T04_metadata_version_change_does_not_rewrite_past(btc_spec):
    later = btc_spec.with_version(2, datetime(2026, 9, 19, tzinfo=UTC))
    assert later.version == 2 and btc_spec.version == 1
    assert later.observed_at > btc_spec.observed_at
    assert later.base_units_per_contract == btc_spec.base_units_per_contract
