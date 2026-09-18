"""Métadonnées d'instruments point-in-time et conversions d'unités (§27, §45, T01-T04).

Seuls les perpetual swaps LINÉAIRES réglés en USDT sont acceptés dans cette version. Tout autre
contrat est rejeté AVANT modèle et AVANT ordre.

Dérivation de ``v = base_units_per_contract`` depuis le contrat OKX vérifié le 18 septembre 2026 [S1] :
``ctVal`` est la valeur d'UN contrat exprimée dans ``ctValCcy`` ; pour un swap linéaire USDT, ``ctValCcy``
est la devise de base (ex. « BTC » pour BTC-USDT-SWAP) et ``ctMult`` vaut « 1 ». Nous exigeons ces trois
conditions ; un ``ctMult`` différent de 1 ou un ``ctValCcy`` différent de la base ne sont pas multipliés
mécaniquement : ils rendent l'instrument non supporté jusqu'à validation par un fixture indépendant.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import RoundingError, UnitError, UnsupportedInstrumentError
from okxq.domain.money import (
    BaseQty,
    Contracts,
    Money,
    dec,
    is_multiple_of,
    round_down_to_step,
)

SUPPORTED_INST_TYPE = "SWAP"
SUPPORTED_CONTRACT_TYPE = "linear"
SUPPORTED_SETTLE_CCY = "USDT"


class InstrumentState(StrEnum):
    LIVE = "live"
    SUSPEND = "suspend"
    PREOPEN = "preopen"
    TEST = "test"
    EXPIRED = "expired"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: str) -> InstrumentState:
        try:
            return cls(raw.lower())
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Version datée des métadonnées d'un instrument (§43)."""

    inst_id: str
    valid_from: datetime
    observed_at: datetime
    settle_ccy: str
    base_ccy: str
    quote_ccy: str
    contract_type: str
    base_units_per_contract: Decimal
    tick_size: Decimal
    lot_size: Decimal
    min_size: Decimal
    state: InstrumentState
    provenance: str
    max_leverage: Decimal | None = None
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "valid_from", ensure_utc(self.valid_from, field="valid_from"))
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, field="observed_at"))
        for name in ("base_units_per_contract", "tick_size", "lot_size", "min_size"):
            value = dec(getattr(self, name), field=name)
            if value <= 0:
                raise UnitError(
                    f"{name} doit être strictement positif", inst_id=self.inst_id, value=str(value)
                )
            object.__setattr__(self, name, value)
        if not is_multiple_of(self.min_size, self.lot_size):
            raise UnitError("min_size n'est pas un multiple de lot_size", inst_id=self.inst_id)

    @property
    def is_tradable(self) -> bool:
        return self.state is InstrumentState.LIVE

    def with_version(self, version: int, observed_at: datetime) -> InstrumentSpec:
        return replace(self, version=version, observed_at=observed_at)


def derive_base_units_per_contract(
    *, ct_val: str, ct_val_ccy: str, ct_type: str, ct_mult: str, base_ccy: str, settle_ccy: str
) -> Decimal:
    """Voir l'en-tête du module : v = ctVal, sous conditions vérifiées explicitement."""
    if ct_type != SUPPORTED_CONTRACT_TYPE:
        raise UnsupportedInstrumentError(
            f"ctType {ct_type!r} non supporté (linéaire uniquement)", ct_type=ct_type
        )
    if settle_ccy != SUPPORTED_SETTLE_CCY:
        raise UnsupportedInstrumentError(
            f"devise de règlement {settle_ccy!r} non supportée", settle_ccy=settle_ccy
        )
    if ct_val_ccy != base_ccy:
        raise UnsupportedInstrumentError(
            "ctValCcy différent de la devise de base : sémantique non validée",
            ct_val_ccy=ct_val_ccy,
            base=base_ccy,
        )
    mult = dec(ct_mult or "1", field="ctMult")
    if mult != 1:
        raise UnsupportedInstrumentError(
            "ctMult différent de 1 : non validé par fixture indépendant", ct_mult=ct_mult
        )
    v = dec(ct_val, field="ctVal")
    if v <= 0:
        raise UnsupportedInstrumentError("ctVal non positif", ct_val=ct_val)
    return v


def spec_from_okx_instrument(
    payload: dict[str, Any], *, observed_at: datetime, provenance: str
) -> InstrumentSpec:
    """Construit une version de métadonnées depuis un enregistrement ``GET /api/v5/public/instruments``.

    Rejette explicitement tout ce qui n'est pas un swap linéaire USDT (T02).
    """
    inst_type = str(payload.get("instType", ""))
    inst_id = str(payload.get("instId", ""))
    if inst_type != SUPPORTED_INST_TYPE:
        raise UnsupportedInstrumentError(f"instType {inst_type!r} non supporté", inst_id=inst_id)
    if not inst_id.endswith(f"-{SUPPORTED_SETTLE_CCY}-SWAP"):
        raise UnsupportedInstrumentError("identifiant hors convention *-USDT-SWAP", inst_id=inst_id)
    settle = str(payload.get("settleCcy", ""))
    base = str(payload.get("ctValCcy", "")) if not payload.get("baseCcy") else str(payload["baseCcy"])
    # Pour un SWAP OKX, baseCcy/quoteCcy sont vides : la base se lit dans l'identifiant et dans ctValCcy.
    ident_base = inst_id.split("-")[0]
    if base and base != ident_base:
        raise UnsupportedInstrumentError("devise de base incohérente avec l'identifiant", inst_id=inst_id)
    base = ident_base
    v = derive_base_units_per_contract(
        ct_val=str(payload.get("ctVal", "")),
        ct_val_ccy=str(payload.get("ctValCcy", "")),
        ct_type=str(payload.get("ctType", "")),
        ct_mult=str(payload.get("ctMult", "1") or "1"),
        base_ccy=base,
        settle_ccy=settle,
    )
    list_time = payload.get("listTime")
    valid_from = observed_at
    if list_time:
        try:
            valid_from = datetime.fromtimestamp(int(str(list_time)) / 1000, tz=observed_at.tzinfo)
        except (TypeError, ValueError):
            valid_from = observed_at
    lever = payload.get("lever")
    return InstrumentSpec(
        inst_id=inst_id,
        valid_from=min(valid_from, observed_at),
        observed_at=observed_at,
        settle_ccy=settle,
        base_ccy=base,
        quote_ccy=SUPPORTED_SETTLE_CCY,
        contract_type=SUPPORTED_CONTRACT_TYPE,
        base_units_per_contract=v,
        tick_size=dec(str(payload.get("tickSz", "")), field="tickSz"),
        lot_size=dec(str(payload.get("lotSz", "")), field="lotSz"),
        min_size=dec(str(payload.get("minSz", "")), field="minSz"),
        state=InstrumentState.parse(str(payload.get("state", "unknown"))),
        provenance=provenance,
        max_leverage=dec(str(lever), field="lever") if lever not in (None, "") else None,
    )


# --- conversions (§45) -------------------------------------------------------------------------------


def contracts_to_base(contracts: Contracts, spec: InstrumentSpec) -> BaseQty:
    return BaseQty(contracts.value * spec.base_units_per_contract, spec.base_ccy)


def base_to_contracts_exact(qty: BaseQty, spec: InstrumentSpec) -> Contracts:
    if qty.ccy != spec.base_ccy:
        raise UnitError("devise de base incompatible", expected=spec.base_ccy, got=qty.ccy)
    return Contracts(qty.value / spec.base_units_per_contract)


def contracts_to_notional(contracts: Contracts, spec: InstrumentSpec, reference_price: Decimal) -> Money:
    price = dec(reference_price, field="reference_price")
    if price <= 0:
        raise UnitError("prix de référence non positif", price=str(price))
    return Money(contracts.value * spec.base_units_per_contract * price, spec.settle_ccy)


def raw_target_contracts(
    target_weight: Decimal, equity: Money, spec: InstrumentSpec, reference_price: Decimal
) -> Decimal:
    """``target_weight * equity / (v * price)`` — brut, non arrondi, signé."""
    if equity.ccy != spec.settle_ccy:
        raise UnitError(
            "equity dans une autre devise que le règlement", equity=equity.ccy, settle=spec.settle_ccy
        )
    price = dec(reference_price, field="reference_price")
    if price <= 0:
        raise UnitError("prix de référence non positif")
    return dec(target_weight, field="target_weight") * equity.amount / (spec.base_units_per_contract * price)


def round_contracts_risk_reducing(raw: Decimal, spec: InstrumentSpec) -> Contracts:
    """Arrondi d'une AUGMENTATION de risque : vers zéro sur la grille lot_size, puis contrôle min_size.

    Une quantité sous min_size devient zéro (pas d'ordre), jamais min_size (ce serait augmenter le risque).
    """
    rounded = round_down_to_step(dec(raw, field="contracts"), spec.lot_size)
    if rounded != 0 and abs(rounded) < spec.min_size:
        return Contracts(Decimal(0))
    return Contracts(rounded)


def validate_order_size(contracts: Contracts, spec: InstrumentSpec) -> None:
    """Vérifie lot, minimum et signe (T03). Lève RoundingError sinon."""
    if contracts.value == 0:
        raise RoundingError("taille nulle", inst_id=spec.inst_id)
    if not is_multiple_of(contracts.abs, spec.lot_size):
        raise RoundingError(
            "taille non multiple du lot", inst_id=spec.inst_id, contracts=str(contracts.value)
        )
    if contracts.abs < spec.min_size:
        raise RoundingError("taille sous le minimum", inst_id=spec.inst_id, contracts=str(contracts.value))


def validate_price_tick(price: Decimal, spec: InstrumentSpec) -> None:
    if price <= 0 or not is_multiple_of(dec(price, field="price"), spec.tick_size):
        raise RoundingError("prix hors grille de tick", inst_id=spec.inst_id, price=str(price))
