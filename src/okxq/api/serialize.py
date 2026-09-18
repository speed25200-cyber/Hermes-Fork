"""Sérialisation JSON des lectures.

- API v1 : les décimaux sont des chaînes (sans perte), les dates en ISO 8601 UTC ;
- couche de compatibilité : l'interface conservée attend des nombres JavaScript et des millisecondes
  epoch ; ``as_float``/``as_ms`` font cette conversion explicitement, ``None`` restant ``None``
  (« Non disponible »), jamais 0.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json"))
    return str(value)


def as_float(value: Decimal | int | float | str | None) -> float | None:
    """Conversion explicite pour l'interface (affichage seulement, jamais pour un calcul monétaire)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def as_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.astimezone(UTC).timestamp() * 1000)


def as_iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()
