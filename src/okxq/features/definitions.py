"""Définitions de features et résultats élémentaires (§7, §35).

Une ``FeatureDefinition`` décrit une feature de façon stable (nom, version, source, sémantique temporelle,
fenêtre, normalisation, unité, politique d'absence). Un ``FeatureResult`` porte une valeur OU un masque
avec sa raison : la valeur est ``None`` dès que le masque n'est pas OK (jamais de zéro silencieux).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from okxq.domain.events import QualityFlag


class TemporalSemantics(StrEnum):
    """Quand la donnée sous-jacente est réputée connue (§35)."""

    CLOSED_BAR = "closed_bar"  # bougies clôturées (confirm:"1") uniquement
    INTRABAR = "intrabar"  # bougie en formation (confirm:"0") : explicitement intrabougie
    BOOK_STATE = "book_state"  # état du carnet à la coupure
    TRADE_WINDOW = "trade_window"  # fenêtre glissante de trades terminée à la coupure
    DERIVED = "derived"  # calculée depuis d'autres features/records (mark, index, funding, OI)
    EVENT = "event"  # événements textuels / évaluations JEV


class MissingPolicy(StrEnum):
    """Que faire quand la donnée manque. Aucune politique n'impute une valeur."""

    MASK = "mask"  # MISSING avec raison
    STALE_BOUNDED = "stale_bounded"  # utilisable jusqu'à ``max_age_s`` puis STALE ; MISSING si absent


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    version: str
    source: str
    temporal_semantics: TemporalSemantics
    lookback_s: int
    normalization: str
    unit: str
    missing_policy: MissingPolicy = MissingPolicy.MASK
    max_age_s: int | None = None
    group: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum() or self.name != self.name.lower():
            raise ValueError(f"nom de feature invalide : {self.name!r} (snake_case ascii attendu)")
        if not self.version:
            raise ValueError(f"version vide pour {self.name}")
        if self.lookback_s < 0:
            raise ValueError(f"lookback_s négatif pour {self.name}")
        if self.missing_policy is MissingPolicy.STALE_BOUNDED and self.max_age_s is None:
            raise ValueError(f"{self.name} : stale_bounded exige max_age_s")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "temporal_semantics": self.temporal_semantics.value,
            "lookback_s": self.lookback_s,
            "normalization": self.normalization,
            "unit": self.unit,
            "missing_policy": self.missing_policy.value,
            "max_age_s": self.max_age_s,
            "group": self.group,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class FeatureResult:
    """Valeur calculée ou masque motivé. ``value`` est ``None`` si et seulement si ``flag`` n'est pas OK."""

    value: float | None
    flag: QualityFlag = QualityFlag.OK
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.flag is QualityFlag.OK:
            if self.value is None or not math.isfinite(self.value):
                raise ValueError("FeatureResult OK exige une valeur finie")
        elif self.value is not None:
            raise ValueError(f"FeatureResult {self.flag} ne porte pas de valeur (pas d'imputation)")
        elif not self.reason:
            raise ValueError(f"FeatureResult {self.flag} exige une raison")


def ok(value: float) -> FeatureResult:
    """Valeur finie → OK ; valeur non finie → INVALID motivé (jamais de NaN silencieux)."""
    if value is None or not math.isfinite(value):
        return FeatureResult(None, QualityFlag.INVALID, "non_finite")
    return FeatureResult(float(value))


def missing(reason: str) -> FeatureResult:
    return FeatureResult(None, QualityFlag.MISSING, reason)


def stale(reason: str) -> FeatureResult:
    return FeatureResult(None, QualityFlag.STALE, reason)


def invalid(reason: str) -> FeatureResult:
    return FeatureResult(None, QualityFlag.INVALID, reason)


@dataclass(frozen=True, slots=True)
class FeatureGroupOutput:
    """Sortie d'un module de features : un résultat par nom défini, dans l'ordre des définitions."""

    results: dict[str, FeatureResult] = field(default_factory=dict)

    def fill_missing(self, names: list[str], reason: str) -> FeatureGroupOutput:
        merged = dict(self.results)
        for name in names:
            merged.setdefault(name, missing(reason))
        return FeatureGroupOutput(merged)


def all_missing(names: list[str], reason: str) -> dict[str, FeatureResult]:
    return {name: missing(reason) for name in names}


def all_invalid(names: list[str], reason: str) -> dict[str, FeatureResult]:
    return {name: invalid(reason) for name in names}
