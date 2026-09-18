"""Types de colonnes : TIMESTAMPTZ garanti timezone-aware en sortie, NUMERIC de précision explicite."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, Numeric
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from okxq.domain.clocks import ensure_utc


class TZDateTime(TypeDecorator[datetime]):
    """Stocke en UTC ; refuse les dates naïves à l'écriture ; rend toujours une date UTC aware."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


# Précisions documentées (§44) : montants et notionnels NUMERIC(28,8) ; prix/quantités NUMERIC(28,12)
# (les prix de certains instruments ont 7 décimales, les quantités de base jusqu'à 8 ; 12 laisse une marge
# testée par les tests de dépassement).
MoneyNumeric = Numeric(28, 8, asdecimal=True)
PriceNumeric = Numeric(28, 12, asdecimal=True)
QtyNumeric = Numeric(28, 12, asdecimal=True)
FractionNumeric = Numeric(18, 12, asdecimal=True)

JsonDoc: Any = JSON().with_variant(JSONB(), "postgresql")

MONEY_MAX_ABS = Decimal("1e20")
PRICE_MAX_ABS = Decimal("1e16")


def check_numeric_bounds(value: Decimal, *, kind: str) -> Decimal:
    """Test de dépassement (§44) : refuse une valeur que la colonne ne peut pas représenter."""
    limit = MONEY_MAX_ABS if kind == "money" else PRICE_MAX_ABS
    if abs(value) >= limit:
        raise OverflowError(f"valeur {kind} hors précision NUMERIC : {value}")
    return value
