"""Fixtures partagées. Le réseau est interdit par construction (§67) : toute socket sortante échoue."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from okxq.config import AppConfig, load_config
from okxq.domain.clocks import SimulatedClock
from okxq.domain.instruments import InstrumentSpec, InstrumentState

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)

_real_connect = socket.socket.connect


def _blocked_connect(self: socket.socket, address: object) -> None:
    host = address[0] if isinstance(address, tuple) else str(address)
    if host in ("127.0.0.1", "::1", "localhost") or (isinstance(address, str) and address.startswith("/")):
        _real_connect(self, address)  # type: ignore[arg-type]
        return
    raise RuntimeError(f"réseau interdit dans les tests hermétiques : connexion vers {address!r}")


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if "connected" in request.keywords:
        yield
        return
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    yield


@pytest.fixture
def clock() -> SimulatedClock:
    return SimulatedClock(T0)


@pytest.fixture
def test_spec() -> InstrumentSpec:
    """Instrument synthétique TEST-USDT-SWAP (§68.1) : 0,001 unité de base par contrat."""
    return InstrumentSpec(
        inst_id="TEST-USDT-SWAP",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        observed_at=T0,
        settle_ccy="USDT",
        base_ccy="TEST",
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        lot_size=Decimal("1"),
        min_size=Decimal("1"),
        state=InstrumentState.LIVE,
        provenance="fixture:§68.1",
        max_leverage=Decimal("50"),
    )


@pytest.fixture
def btc_spec() -> InstrumentSpec:
    return InstrumentSpec(
        inst_id="BTC-USDT-SWAP",
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        observed_at=T0,
        settle_ccy="USDT",
        base_ccy="BTC",
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=Decimal("0.01"),
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.1"),
        min_size=Decimal("0.1"),
        state=InstrumentState.LIVE,
        provenance="fixture:okx-instruments-2026-09-18",
        max_leverage=Decimal("100"),
    )


@pytest.fixture
def paper_config() -> AppConfig:
    return load_config(ROOT / "configs" / "paper.yaml")


@pytest.fixture
def smoke_config() -> AppConfig:
    return load_config(FIXTURES / "configs" / "smoke.fixture.yaml")


@pytest.fixture
def pg_url() -> str:
    url = os.environ.get("OKXQ_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OKXQ_TEST_DATABASE_URL absent : test PostgreSQL NOT_RUN")
    return url
