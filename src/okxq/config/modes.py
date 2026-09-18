"""Modes d'exécution strictement séparés (§1)."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    DEMO = "DEMO"
    LIVE = "LIVE"

    @property
    def sends_orders_to_exchange(self) -> bool:
        return self in (Mode.DEMO, Mode.LIVE)

    @property
    def uses_private_streams(self) -> bool:
        return self in (Mode.DEMO, Mode.LIVE)

    @property
    def uses_real_capital(self) -> bool:
        return self is Mode.LIVE

    @property
    def consumes_public_market_data(self) -> bool:
        return self in (Mode.PAPER, Mode.SHADOW, Mode.DEMO, Mode.LIVE)
