"""Assemblage point-in-time features + labels (polars), hash de jeu de données, niveau de qualité.

- Un dossier d'événements golden (``events.jsonl`` + ``manifest.json``, docs/event_schemas.md) ou le
  générateur synthétique seedé (``okxq.research.synthetic``) fournit les enveloppes.
- Les features sont calculées à chaque coupure de la grille (``cutoff_interval_s``) par le moteur de
  référence ; les labels par ``okxq.research.labels`` sur le chemin de prix ; la jointure se fait sur
  ``(instrument, decision_at)``. ``assert_label_causality`` (T19) est exécuté à l'assemblage.
- ``dataset_hash`` : SHA-256 du contenu canonique (CSV trié) + hash du schéma de features + spécifications.
- Qualité : ``A`` = carnet + trades horodatés par l'exchange, ``B`` = bougies seules, ``C`` = synthétique
  ou incomplet. ``LATENCY_ASSUMED`` : les ``available_at`` ne sont pas mesurés mais supposés.
- Chevauchement des horizons : ``overlap[h] = h / cutoff_interval_s`` labels simultanément ouverts ; le
  nombre effectif d'observations en tient compte (``okxq.research.evaluation``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import polars as pl

from okxq.domain.clocks import ensure_utc, floor_to_interval
from okxq.domain.events import EventEnvelope, JevEvaluation
from okxq.domain.ids import payload_hash, sha256_hex
from okxq.features.batch import BatchStateBuilder, compute_batch, mask_summary
from okxq.features.registry import FeatureEngine, FeatureRegistry, default_registry
from okxq.features.state import AnnouncementMeta
from okxq.research import SYNTHETIC_NOTICE
from okxq.research.labels import (
    LABEL_VALUE_COLUMNS,
    CostFunction,
    LabelSpec,
    SpreadPlusFeeCost,
    assert_label_causality,
    compute_labels,
    overlap_factor,
)
from okxq.research.synthetic import (
    SyntheticSpec,
    generate_synthetic_events,
    read_event_folder,
)

DEFAULT_WARMUP_S = 3660
#: Horizons de label par défaut.
#:
#: L'horizon de 60 s a été retiré : le chemin de prix des labels est échantillonné au pas de décision
#: (60 s), donc la fenêtre d'entrée ne peut pas être plus courte, et un horizon de 60 s ne laisserait
#: aucune durée de détention entre l'entrée et la sortie. Il produisait une ligne NO_ENTRY par
#: coupure — un tiers du jeu de données en pur bruit. `LabelSpec` refuse désormais cette combinaison.
DEFAULT_LABEL_SPECS: tuple[LabelSpec, ...] = (
    LabelSpec(horizon_s=300, take_profit=0.002, stop_loss=0.002),
    LabelSpec(horizon_s=900, take_profit=0.004, stop_loss=0.004),
)


class QualityLevel(StrEnum):
    A = "A"
    B = "B"
    C = "C"


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    cutoff_interval_s: int = 60
    warmup_s: int = DEFAULT_WARMUP_S
    label_specs: tuple[LabelSpec, ...] = DEFAULT_LABEL_SPECS
    feature_groups: tuple[str, ...] | None = None
    instruments: tuple[str, ...] | None = None
    start: datetime | None = None
    end: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cutoff_interval_s": self.cutoff_interval_s,
            "warmup_s": self.warmup_s,
            "label_specs": [s.to_dict() for s in self.label_specs],
            "feature_groups": list(self.feature_groups) if self.feature_groups else None,
            "instruments": list(self.instruments) if self.instruments else None,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
        }


@dataclass
class ResearchDataset:
    frame: pl.DataFrame
    feature_names: list[str]
    schema_hash: str
    dataset_hash: str
    quality_level: QualityLevel
    latency_assumed: bool
    horizons_s: list[int]
    cutoff_interval_s: int
    instruments: list[str]
    period_start: datetime
    period_end: datetime
    source: str
    spec: dict[str, Any]
    cost_version: str
    synthetic: bool
    notes: list[str] = field(default_factory=list)
    mask_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    registry_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def overlap(self) -> dict[int, float]:
        return {h: overlap_factor(h, self.cutoff_interval_s) for h in self.horizons_s}

    @property
    def label_columns(self) -> list[str]:
        return list(LABEL_VALUE_COLUMNS)

    def rows_for_horizon(self, horizon_s: int) -> pl.DataFrame:
        return self.frame.filter(pl.col("horizon_s") == horizon_s)

    def manifest(self) -> dict[str, Any]:
        return {
            "dataset_hash": self.dataset_hash,
            "schema_hash": self.schema_hash,
            "quality_level": self.quality_level.value,
            "LATENCY_ASSUMED": self.latency_assumed,
            "synthetic": self.synthetic,
            "notice": SYNTHETIC_NOTICE if self.synthetic else None,
            "rows": self.frame.height,
            "instruments": self.instruments,
            "horizons_s": self.horizons_s,
            "cutoff_interval_s": self.cutoff_interval_s,
            "overlap_factor_by_horizon": {str(h): v for h, v in self.overlap.items()},
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "source": self.source,
            "spec": self.spec,
            "cost_version": self.cost_version,
            "feature_count": len(self.feature_names),
            "notes": self.notes,
            "mask_counts": self.mask_counts,
        }

    def save(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        self.frame.write_parquet(folder / "dataset.parquet")
        (folder / "dataset.manifest.json").write_text(
            json.dumps(self.manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (folder / "feature_schema.json").write_text(
            json.dumps(self.registry_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return folder

    @classmethod
    def load(cls, folder: Path) -> ResearchDataset:
        frame = pl.read_parquet(folder / "dataset.parquet")
        manifest = json.loads((folder / "dataset.manifest.json").read_text(encoding="utf-8"))
        registry_payload = json.loads((folder / "feature_schema.json").read_text(encoding="utf-8"))
        names = list(registry_payload["names"])
        recomputed = dataset_hash(frame, manifest["schema_hash"], manifest["spec"])
        if recomputed != manifest["dataset_hash"]:
            raise ValueError("dataset.parquet ne correspond pas au dataset_hash du manifeste")
        return cls(
            frame=frame,
            feature_names=names,
            schema_hash=manifest["schema_hash"],
            dataset_hash=manifest["dataset_hash"],
            quality_level=QualityLevel(manifest["quality_level"]),
            latency_assumed=bool(manifest["LATENCY_ASSUMED"]),
            horizons_s=[int(h) for h in manifest["horizons_s"]],
            cutoff_interval_s=int(manifest["cutoff_interval_s"]),
            instruments=list(manifest["instruments"]),
            period_start=datetime.fromisoformat(manifest["period_start"]),
            period_end=datetime.fromisoformat(manifest["period_end"]),
            source=manifest["source"],
            spec=manifest["spec"],
            cost_version=manifest["cost_version"],
            synthetic=bool(manifest["synthetic"]),
            notes=list(manifest.get("notes", [])),
            mask_counts=manifest.get("mask_counts", {}),
            registry_payload=registry_payload,
        )


def dataset_hash(frame: pl.DataFrame, schema_hash: str, spec: Mapping[str, Any]) -> str:
    """Hash du contenu : lignes triées, sérialisées en CSV (déterministe pour une version de polars)."""
    key = [c for c in ("instrument", "horizon_s", "decision_at") if c in frame.columns]
    body = frame.sort(key).write_csv() if frame.height else ""
    return sha256_hex(f"{schema_hash}\n{payload_hash(dict(spec))}\n{body}")


def infer_quality_level(builder: BatchStateBuilder, *, synthetic: bool) -> QualityLevel:
    if synthetic:
        return QualityLevel.C
    has_books = any(
        builder.candles_for(i, builder.last_available_at or datetime.max).size for i in builder.instruments
    )
    return QualityLevel.A if has_books else QualityLevel.B


def cutoff_grid(start: datetime, end: datetime, interval_s: int) -> list[datetime]:
    first = floor_to_interval(start, interval_s) + timedelta(seconds=interval_s)
    out: list[datetime] = []
    t = first
    while t <= end:
        out.append(t)
        t += timedelta(seconds=interval_s)
    return out


def build_dataset(
    events: Sequence[EventEnvelope],
    *,
    spec: DatasetSpec = DatasetSpec(),
    registry: FeatureRegistry | None = None,
    cost_fn: CostFunction | None = None,
    source: str,
    synthetic: bool,
    latency_assumed: bool,
    quality_level: QualityLevel | None = None,
    jev_by_instrument: Mapping[str, Sequence[JevEvaluation]] | None = None,
    announcements_by_instrument: Mapping[str, Sequence[AnnouncementMeta]] | None = None,
    announcement_feed_available: bool = False,
) -> ResearchDataset:
    reg = registry if registry is not None else default_registry(spec.feature_groups)
    cost = cost_fn if cost_fn is not None else SpreadPlusFeeCost()
    builder = BatchStateBuilder(events)
    if builder.first_available_at is None or builder.last_available_at is None:
        raise ValueError("aucun événement de marché")
    instruments = list(spec.instruments) if spec.instruments else builder.instruments
    start = (
        ensure_utc(spec.start)
        if spec.start
        else builder.first_available_at + timedelta(seconds=spec.warmup_s)
    )
    end = ensure_utc(spec.end) if spec.end else builder.last_available_at
    cutoffs = cutoff_grid(start, end, spec.cutoff_interval_s)
    if not cutoffs:
        raise ValueError("fenêtre trop courte pour une seule coupure après échauffement")
    engine = FeatureEngine(reg)
    features, _ = compute_batch(
        engine,
        builder,
        cutoffs,
        instruments=instruments,
        jev_by_instrument=jev_by_instrument,
        announcements_by_instrument=announcements_by_instrument,
        announcement_feed_available=announcement_feed_available,
    )
    label_frames: list[pl.DataFrame] = []
    path_end = builder.last_available_at + timedelta(seconds=max(s.horizon_s for s in spec.label_specs))
    # Le chemin de prix DOIT être échantillonné sur la même horloge que les décisions. Il partait de
    # `builder.first_available_at`, c'est-à-dire l'horodatage brut du premier événement — décalé de la
    # latence d'ingestion (150 ms sur les jeux synthétiques) — alors que les coupures sont alignées sur
    # l'intervalle. Aucun point du chemin ne tombait donc dans la fenêtre d'entrée
    # ``(coupure + délai, coupure + fenêtre]``, et TOUS les labels sortaient NO_ENTRY : le jeu
    # d'entraînement était vide sans que rien ne le signale.
    path_start = floor_to_interval(builder.first_available_at, spec.cutoff_interval_s)
    for inst in instruments:
        path = builder.price_path(inst, path_start, path_end, step_s=spec.cutoff_interval_s)
        for ls in spec.label_specs:
            label_frames.append(compute_labels(path, cutoffs, ls, instrument_id=inst, cost_fn=cost))
    labels = pl.concat(label_frames)
    assert_label_causality(labels)
    frame = features.join(
        labels.rename({"instrument_id": "instrument", "decision_at": "cutoff_at"}),
        on=["instrument", "cutoff_at"],
        how="inner",
    ).rename({"cutoff_at": "decision_at"})
    frame = frame.sort(["horizon_s", "decision_at", "instrument"])
    quality = (
        quality_level if quality_level is not None else infer_quality_level(builder, synthetic=synthetic)
    )
    notes = [SYNTHETIC_NOTICE] if synthetic else []
    if latency_assumed:
        notes.append("LATENCY_ASSUMED : available_at supposé = exchange_ts + latence fixe")
    if builder.dropped_book_updates:
        notes.append(f"mises à jour de carnet ignorées sans snapshot : {builder.dropped_book_updates}")
    spec_dict = spec.to_dict()
    return ResearchDataset(
        frame=frame,
        feature_names=reg.names,
        schema_hash=reg.schema_hash,
        dataset_hash=dataset_hash(frame, reg.schema_hash, spec_dict),
        quality_level=quality,
        latency_assumed=latency_assumed,
        horizons_s=sorted({s.horizon_s for s in spec.label_specs}),
        cutoff_interval_s=spec.cutoff_interval_s,
        instruments=instruments,
        period_start=cutoffs[0],
        period_end=cutoffs[-1],
        source=source,
        spec=spec_dict,
        cost_version=cost.version,
        synthetic=synthetic,
        notes=notes,
        mask_counts=mask_summary(features),
        registry_payload=reg.to_dict(),
    )


def dataset_from_folder(
    folder: Path,
    *,
    spec: DatasetSpec = DatasetSpec(),
    registry: FeatureRegistry | None = None,
    cost_fn: CostFunction | None = None,
) -> ResearchDataset:
    events, manifest = read_event_folder(folder)
    synthetic = bool(
        manifest.get("notes", "") and SYNTHETIC_NOTICE.split(" — ")[0] in str(manifest.get("notes", ""))
    )
    quality = (
        QualityLevel(manifest["quality_level"]) if manifest.get("quality_level") in ("A", "B", "C") else None
    )
    return build_dataset(
        events,
        spec=spec,
        registry=registry,
        cost_fn=cost_fn,
        source=f"folder:{folder}",
        synthetic=synthetic or quality is QualityLevel.C,
        latency_assumed=bool(manifest.get("latency_assumed", True)),
        quality_level=quality,
    )


def synthetic_dataset(
    synthetic_spec: SyntheticSpec = SyntheticSpec(),
    *,
    spec: DatasetSpec = DatasetSpec(),
    registry: FeatureRegistry | None = None,
    cost_fn: CostFunction | None = None,
) -> ResearchDataset:
    events = generate_synthetic_events(synthetic_spec)
    return build_dataset(
        events,
        spec=spec,
        registry=registry,
        cost_fn=cost_fn,
        source=f"synthetic:seed={synthetic_spec.seed}:minutes={synthetic_spec.minutes}",
        synthetic=True,
        latency_assumed=True,
        quality_level=QualityLevel.C,
    )


def load_or_generate(
    folder: Path | None,
    *,
    spec: DatasetSpec = DatasetSpec(),
    synthetic_spec: SyntheticSpec = SyntheticSpec(),
    registry: FeatureRegistry | None = None,
) -> ResearchDataset:
    """Jeu golden s'il existe (``events.jsonl`` présent), sinon synthétique seedé — dit explicitement."""
    if folder is not None and (folder / "events.jsonl").exists():
        return dataset_from_folder(folder, spec=spec, registry=registry)
    return synthetic_dataset(synthetic_spec, spec=spec, registry=registry)
