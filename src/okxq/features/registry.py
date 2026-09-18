"""Registre de features, hash de schéma et moteur de référence (§7, §35).

``FeatureRegistry`` fixe l'ordre et la définition des features ; son ``schema_hash`` (SHA-256 du JSON
canonique des définitions) est persistable dans ``feature_schemas`` et voyage dans chaque
``FeatureVector``. ``FeatureEngine.compute`` est l'UNIQUE implémentation des formules : le batch
(``okxq.features.batch``) et l'incrémental (``okxq.features.incremental``) ne font qu'assembler l'état
point-in-time puis appellent cette méthode ; leur parité est testée (T16).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from okxq.domain.clocks import ensure_utc
from okxq.domain.events import FeatureVector, QualityFlag
from okxq.domain.ids import payload_hash
from okxq.features import cross_asset, derivatives, events, microstructure, timeseries
from okxq.features.definitions import (
    FeatureDefinition,
    FeatureResult,
    MissingPolicy,
    TemporalSemantics,
)
from okxq.features.state import PointInTimeMarketState
from okxq.persistence.models import FeatureSchema

__all__ = [
    "FeatureComputation",
    "FeatureDefinition",
    "FeatureEngine",
    "FeatureRegistry",
    "FeatureResult",
    "MissingPolicy",
    "TemporalSemantics",
    "default_registry",
]

GroupComputer = Callable[[PointInTimeMarketState], dict[str, FeatureResult]]

GROUPS: dict[str, tuple[tuple[FeatureDefinition, ...], GroupComputer]] = {
    "microstructure": (microstructure.DEFINITIONS, microstructure.compute),
    "timeseries": (timeseries.DEFINITIONS, timeseries.compute),
    "cross_asset": (cross_asset.DEFINITIONS, cross_asset.compute),
    "derivatives": (derivatives.DEFINITIONS, derivatives.compute),
    "events_meta": (events.META_DEFINITIONS, events.compute_metadata),
    "events_jev": (events.JEV_DEFINITIONS, events.compute_jev),
}
DEFAULT_VERSION = "features-v1"


class FeatureRegistry:
    def __init__(self, definitions: Iterable[FeatureDefinition], *, version: str = DEFAULT_VERSION) -> None:
        defs = list(definitions)
        names = [d.name for d in defs]
        if len(set(names)) != len(names):
            dup = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"définitions de features dupliquées : {dup}")
        if not defs:
            raise ValueError("registre de features vide")
        self._definitions: tuple[FeatureDefinition, ...] = tuple(defs)
        self._by_name = {d.name: d for d in defs}
        self.version = version
        self._hash = payload_hash({"version": version, "definitions": [d.to_dict() for d in defs]})

    @property
    def definitions(self) -> tuple[FeatureDefinition, ...]:
        return self._definitions

    @property
    def names(self) -> list[str]:
        return [d.name for d in self._definitions]

    @property
    def groups(self) -> list[str]:
        seen: list[str] = []
        for d in self._definitions:
            if d.group not in seen:
                seen.append(d.group)
        return seen

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._definitions)

    def get(self, name: str) -> FeatureDefinition:
        return self._by_name[name]

    @property
    def schema_hash(self) -> str:
        return self._hash

    def subset(self, groups: Sequence[str], *, version: str | None = None) -> FeatureRegistry:
        unknown = [g for g in groups if g not in self.groups]
        if unknown:
            raise ValueError(f"groupes inconnus : {unknown}")
        return FeatureRegistry(
            [d for d in self._definitions if d.group in groups],
            version=version or f"{self.version}+{'+'.join(groups)}",
        )

    def without(self, names: Sequence[str], *, version: str | None = None) -> FeatureRegistry:
        drop = set(names)
        return FeatureRegistry(
            [d for d in self._definitions if d.name not in drop],
            version=version or f"{self.version}-{len(drop)}",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_hash": self._hash,
            "version": self.version,
            "names": self.names,
            "definitions": {d.name: d.to_dict() for d in self._definitions},
        }

    def to_schema_row(self, now: datetime) -> FeatureSchema:
        return FeatureSchema(
            schema_hash=self._hash,
            version=self.version,
            names=self.names,
            definitions={d.name: d.to_dict() for d in self._definitions},
            created_at=ensure_utc(now),
        )

    def persist(self, session: Session, now: datetime) -> str:
        """Enregistre le schéma dans ``feature_schemas`` (idempotent : même hash = même ligne)."""
        existing = session.get(FeatureSchema, self._hash)
        if existing is None:
            session.add(self.to_schema_row(now))
            session.flush()
        return self._hash

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FeatureRegistry:
        defs = [
            FeatureDefinition(
                name=d["name"],
                version=d["version"],
                source=d["source"],
                temporal_semantics=TemporalSemantics(d["temporal_semantics"]),
                lookback_s=int(d["lookback_s"]),
                normalization=d["normalization"],
                unit=d["unit"],
                missing_policy=MissingPolicy(d["missing_policy"]),
                max_age_s=d.get("max_age_s"),
                group=d.get("group", ""),
                description=d.get("description", ""),
            )
            for d in payload["definitions"].values()
        ]
        ordered = {d.name: d for d in defs}
        reg = cls([ordered[n] for n in payload["names"]], version=payload["version"])
        if payload.get("schema_hash") and payload["schema_hash"] != reg.schema_hash:
            raise ValueError("schema_hash incohérent avec les définitions rechargées")
        return reg


def default_registry(
    groups: Sequence[str] | None = None, *, version: str = DEFAULT_VERSION
) -> FeatureRegistry:
    chosen = list(groups) if groups is not None else list(GROUPS)
    unknown = [g for g in chosen if g not in GROUPS]
    if unknown:
        raise ValueError(f"groupes de features inconnus : {unknown}")
    defs: list[FeatureDefinition] = []
    for g in chosen:
        defs.extend(GROUPS[g][0])
    return FeatureRegistry(defs, version=version if groups is None else f"{version}+{'+'.join(chosen)}")


@dataclass(frozen=True, slots=True)
class FeatureComputation:
    vector: FeatureVector
    reasons: dict[str, str]

    def flags(self) -> dict[str, QualityFlag]:
        return dict(zip(self.vector.names, self.vector.masks, strict=True))

    def value(self, name: str) -> float | None:
        return self.vector.values[self.vector.names.index(name)]

    def flag(self, name: str) -> QualityFlag:
        return self.vector.masks[self.vector.names.index(name)]


class FeatureEngine:
    """Implémentation de référence : état point-in-time → ``FeatureVector`` masqué."""

    def __init__(self, registry: FeatureRegistry) -> None:
        self.registry = registry
        self._computers: list[tuple[GroupComputer, list[str]]] = []
        for group in registry.groups:
            if group not in GROUPS:
                raise ValueError(f"groupe sans calculateur : {group}")
            names = [d.name for d in registry.definitions if d.group == group]
            self._computers.append((GROUPS[group][1], names))

    def compute(
        self, state: PointInTimeMarketState, *, available_at: datetime | None = None
    ) -> FeatureComputation:
        results: dict[str, FeatureResult] = {}
        for computer, names in self._computers:
            group_out = computer(state)
            for name in names:
                if name not in group_out:
                    raise RuntimeError(f"feature définie mais non calculée : {name}")
                results[name] = group_out[name]
        values: list[float | None] = []
        masks: list[QualityFlag] = []
        reasons: dict[str, str] = {}
        for name in self.registry.names:
            r = results[name]
            values.append(r.value)
            masks.append(r.flag)
            if r.reason:
                reasons[name] = r.reason
        vector = FeatureVector(
            instrument=state.instrument,
            cutoff_at=state.cutoff_at,
            available_at=ensure_utc(available_at) if available_at is not None else state.cutoff_at,
            names=self.registry.names,
            values=values,
            masks=masks,
            schema_hash=self.registry.schema_hash,
        )
        return FeatureComputation(vector=vector, reasons=reasons)
