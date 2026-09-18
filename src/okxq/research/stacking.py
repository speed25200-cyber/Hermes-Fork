"""Stacking OOF TEMPOREL : experts spécialisés puis méta-modèle (§19, §56, T20).

POURQUOI ce module ne peut pas utiliser de fold aléatoire : sur une série temporelle, un fold tiré au
hasard met du futur dans l'entraînement de l'expert, donc le méta-modèle apprend sur des prédictions
artificiellement parfaites et le résultat hors échantillon devient inexplicable. Ici :

1. chaque expert est entraîné par ``okxq.research.training.walk_forward_train`` sur les MÊMES folds
   chronologiques ; ses prédictions retenues sont celles de la période de TEST de chaque fold, donc
   out-of-fold par construction ;
2. chaque ligne porte, POUR CHAQUE COMPOSANT, la date maximale que ce composant a utilisée
   (``<expert>__max_train_at``). ``assert_components_never_saw_the_row`` refuse la moindre ligne dont un
   composant connaissait déjà l'instant prédit (T20) ;
3. le méta-modèle est entraîné sur les folds ANTÉRIEURS et évalué sur le fold suivant (fenêtre
   croissante). ``assert_temporal_split`` interdit tout entrelacement ;
4. quand la cible est une probabilité, la calibration se règle sur un fold DISTINCT du méta-entraînement
   et du méta-test (§56) ;
5. le méta-modèle est VERROUILLÉ (un seul jeu d'hyperparamètres) : ajouter une recherche à cet étage
   créerait une seconde couche de sélection que rien ne mesurerait.

Les prédictions du méta-modèle sortent au format OOF de ``training`` : ``evaluate_oof`` les évalue
économiquement (net de frais et de spread) sans code dupliqué.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import LeakageError, ProtocolViolationError
from okxq.research import SYNTHETIC_NOTICE
from okxq.research.baselines import RidgeModel
from okxq.research.calibration import IsotonicCalibrator, PlattCalibrator, calibration_report
from okxq.research.datasets import ResearchDataset
from okxq.research.splits import (
    FittedTransformer,
    Period,
    Standardizer,
    assert_temporal_split,
)
from okxq.research.training import (
    OOF_SCHEMA,
    TrainingResult,
    TrainingSpec,
    split_metrics,
    walk_forward_train,
)

JOIN_KEYS = ("decision_at", "instrument", "horizon_s", "fold_id")
CARRIED_COLUMNS = ("target", "future_mid_return", "round_trip_cost", "label_available_at")


@dataclass(frozen=True, slots=True)
class ExpertSpec:
    """Un expert = un sous-ensemble de features nommé + une famille de candidats.

    Les experts sont spécialisés (microstructure, momentum, force relative, dérivés, événement JEV) afin
    que le méta-modèle arbitre entre des points de vue, et non entre dix variantes du même modèle.
    """

    name: str
    candidates: tuple[str, ...] = ("ridge",)
    feature_groups: tuple[str, ...] | None = None
    feature_names: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "candidates": list(self.candidates),
            "feature_groups": list(self.feature_groups) if self.feature_groups else None,
            "feature_names": list(self.feature_names) if self.feature_names else None,
        }


DEFAULT_EXPERTS: tuple[ExpertSpec, ...] = (
    ExpertSpec(name="microstructure", feature_groups=("microstructure",)),
    ExpertSpec(name="momentum_reversion", feature_groups=("timeseries",)),
    ExpertSpec(name="relative_strength", feature_groups=("cross_asset",)),
    ExpertSpec(name="derivatives", feature_groups=("derivatives",)),
    ExpertSpec(name="jev_event", feature_groups=("events_meta", "events_jev")),
)


@dataclass(frozen=True, slots=True)
class MetaSpec:
    """Méta-modèle VERROUILLÉ : aucune recherche d'hyperparamètres à cet étage (§19)."""

    alpha: float = 1.0
    min_train_folds: int = 2
    calibrator: str = "platt"
    standardize: bool = True

    def __post_init__(self) -> None:
        if self.min_train_folds < 1:
            raise ValueError("min_train_folds >= 1 requis")
        if self.calibrator not in ("platt", "isotonic", "none"):
            raise ValueError("calibrator ∈ {platt, isotonic, none}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": "ridge",
            "alpha": self.alpha,
            "locked": True,
            "min_train_folds": self.min_train_folds,
            "calibrator": self.calibrator,
            "standardize": self.standardize,
        }


def features_by_group(dataset: ResearchDataset) -> dict[str, list[str]]:
    """Groupes du schéma de features tel qu'il a été enregistré avec le jeu de données."""
    definitions = dataset.registry_payload.get("definitions", {})
    out: dict[str, list[str]] = {}
    for name in dataset.feature_names:
        group = str(definitions.get(name, {}).get("group", ""))
        out.setdefault(group, []).append(name)
    return out


def resolve_expert_features(dataset: ResearchDataset, expert: ExpertSpec) -> list[str]:
    """Features d'un expert. Un groupe inconnu est une erreur : on ne devine jamais un périmètre."""
    if expert.feature_names is not None:
        unknown = [n for n in expert.feature_names if n not in dataset.feature_names]
        if unknown:
            raise ProtocolViolationError(
                "features inconnues pour l'expert", expert=expert.name, unknown=unknown
            )
        return list(expert.feature_names)
    if expert.feature_groups is None:
        return list(dataset.feature_names)
    by_group = features_by_group(dataset)
    unknown_groups = [g for g in expert.feature_groups if g not in by_group]
    if unknown_groups:
        raise ProtocolViolationError(
            "groupes de features absents du jeu de données", expert=expert.name, groups=unknown_groups
        )
    names: list[str] = []
    for group in expert.feature_groups:
        names.extend(by_group[group])
    return names


@dataclass
class ExpertOutcome:
    """Un expert entraîné : son résultat walk-forward et ses métriques OOF."""

    spec: ExpertSpec
    feature_names: list[str]
    result: TrainingResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "expert": self.spec.to_dict(),
            "feature_count": len(self.feature_names),
            "oof_metrics": self.result.oof_metrics,
            "selected_by_fold": {f.fold.fold_id: f.selected.model_name for f in self.result.folds},
            "max_train_at_by_fold": {f.fold.fold_id: f.max_train_at.isoformat() for f in self.result.folds},
        }


@dataclass
class OofPanel:
    """Panneau OOF large : une ligne par (décision, instrument), une colonne par expert."""

    frame: pl.DataFrame
    experts: list[ExpertOutcome]
    fold_ids: list[str]

    @property
    def names(self) -> list[str]:
        return [e.spec.name for e in self.experts]

    def matrix(self) -> np.ndarray:
        return self.frame.select([f"{n}__prediction" for n in self.names]).to_numpy().astype(float)


def assert_components_never_saw_the_row(panel: OofPanel) -> None:
    """T20 : pour CHAQUE composant et CHAQUE ligne, ``max_train_at < decision_at``."""
    for name in panel.names:
        col = f"{name}__max_train_at"
        bad = panel.frame.filter(pl.col(col) >= pl.col("decision_at"))
        if bad.height:
            row = bad.row(0, named=True)
            raise LeakageError(
                "un composant du stacking connaissait déjà la fenêtre prédite",
                expert=name,
                rows=int(bad.height),
                decision_at=str(row["decision_at"]),
                max_train_at=str(row[col]),
            )


def build_oof_panel(
    dataset: ResearchDataset,
    spec: TrainingSpec,
    *,
    experts: Sequence[ExpertSpec] = DEFAULT_EXPERTS,
    notes: list[str] | None = None,
) -> OofPanel:
    """Entraîne chaque expert sur les MÊMES folds et assemble leurs prédictions out-of-fold."""
    collected: list[ExpertOutcome] = []
    log = notes if notes is not None else []
    for expert in experts:
        names = resolve_expert_features(dataset, expert)
        if not names:
            log.append(f"expert {expert.name} ignoré : aucune feature dans ses groupes")
            continue
        expert_spec = TrainingSpec(
            walk_forward=spec.walk_forward,
            target=spec.target,
            horizon_s=spec.horizon_s,
            candidates=expert.candidates,
            seed=spec.seed,
            max_trials=spec.max_trials,
            final_test_start=spec.final_test_start,
            policy_dependency_s=spec.policy_dependency_s,
            winsor_quantiles=spec.winsor_quantiles,
            max_missing_fraction=spec.max_missing_fraction,
            calibrator=spec.calibrator,
            min_fit_rows=spec.min_fit_rows,
        )
        try:
            result = walk_forward_train(dataset, expert_spec, feature_names=names, producer=expert.name)
        except (ProtocolViolationError, ValueError) as exc:
            # Un expert sans données exploitables est ÉCARTÉ et dit pourquoi ; il n'est jamais remplacé
            # par des zéros, ce qui donnerait au méta-modèle une opinion qui n'existe pas.
            log.append(f"expert {expert.name} écarté : {exc}")
            continue
        collected.append(ExpertOutcome(spec=expert, feature_names=names, result=result))
    if len(collected) < 2:
        raise ProtocolViolationError(
            "stacking impossible : moins de deux experts exploitables",
            experts=[e.spec.name for e in collected],
            notes=log,
        )
    wide: pl.DataFrame | None = None
    for outcome in collected:
        name = outcome.spec.name
        part = outcome.result.oof.select(
            *[pl.col(k) for k in JOIN_KEYS],
            pl.col("prediction").alias(f"{name}__prediction"),
            pl.col("max_train_at").alias(f"{name}__max_train_at"),
            *[pl.col(c) for c in CARRIED_COLUMNS],
        )
        if wide is None:
            wide = part
            continue
        wide = wide.join(part.drop(list(CARRIED_COLUMNS)), on=list(JOIN_KEYS), how="inner")
    assert wide is not None
    names = [e.spec.name for e in collected]
    wide = wide.with_columns(
        pl.max_horizontal([pl.col(f"{n}__max_train_at") for n in names]).alias("component_max_train_at")
    ).sort(["decision_at", "instrument"])
    if wide.height == 0:
        raise ProtocolViolationError("aucune ligne OOF commune à tous les experts")
    panel = OofPanel(frame=wide, experts=collected, fold_ids=sorted(set(wide["fold_id"].to_list())))
    assert_components_never_saw_the_row(panel)
    return panel


def _meta_calibrator(spec: MetaSpec, task: str) -> FittedTransformer | None:
    if task != "classification" or spec.calibrator == "none":
        return None
    return PlattCalibrator() if spec.calibrator == "platt" else IsotonicCalibrator()


@dataclass
class MetaFold:
    """Un pas du méta-walk-forward : folds d'entraînement, fold de calibration, fold évalué."""

    train_fold_ids: list[str]
    calibration_fold_id: str | None
    test_fold_id: str
    rows_train: int
    rows_test: int
    metrics: dict[str, Any]
    component_max_train_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_fold_ids": list(self.train_fold_ids),
            "calibration_fold_id": self.calibration_fold_id,
            "test_fold_id": self.test_fold_id,
            "rows_train": self.rows_train,
            "rows_test": self.rows_test,
            "metrics": self.metrics,
            "component_max_train_at": self.component_max_train_at,
        }


@dataclass
class StackingResult:
    """Sortie du stacking : panneau OOF, méta-folds, prédictions du méta-modèle et manifeste."""

    spec: dict[str, Any]
    meta_spec: dict[str, Any]
    panel: OofPanel
    meta_folds: list[MetaFold]
    meta_oof: pl.DataFrame
    meta_metrics: dict[str, Any]
    expert_metrics: dict[str, Any]
    synthetic: bool
    notes: list[str] = field(default_factory=list)

    def manifest(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "meta": self.meta_spec,
            "experts": [e.to_dict() for e in self.panel.experts],
            "fold_ids": list(self.panel.fold_ids),
            "meta_folds": [f.to_dict() for f in self.meta_folds],
            "panel_rows": int(self.panel.frame.height),
            "meta_oof_rows": int(self.meta_oof.height),
            "meta_metrics": self.meta_metrics,
            "expert_metrics": self.expert_metrics,
            "oof_temporal": True,
            "random_folds_used": False,
            "synthetic": self.synthetic,
            "notice": SYNTHETIC_NOTICE if self.synthetic else None,
            "notes": list(self.notes),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "experts": self.panel.names,
            "meta_folds": len(self.meta_folds),
            "meta_oof_rows": int(self.meta_oof.height),
            "meta_metrics": self.meta_metrics,
            "expert_metrics": self.expert_metrics,
            "synthetic": self.synthetic,
            "notice": SYNTHETIC_NOTICE if self.synthetic else None,
            "notes": list(self.notes),
        }


def _period_of(frame: pl.DataFrame) -> Period:
    times = [ensure_utc(t) for t in frame["decision_at"].to_list()]
    return Period(min(times), max(times) + timedelta(microseconds=1))


def train_meta_model(
    panel: OofPanel,
    spec: TrainingSpec,
    *,
    meta: MetaSpec = MetaSpec(),
    notes: list[str] | None = None,
) -> StackingResult:
    """Méta-modèle à fenêtre croissante : entraîné sur les folds passés, évalué sur le fold suivant."""
    log = notes if notes is not None else []
    folds = panel.fold_ids
    if len(folds) < meta.min_train_folds + 1:
        raise ProtocolViolationError(
            "pas assez de folds pour un méta-modèle temporel",
            folds=len(folds),
            minimum=meta.min_train_folds + 1,
        )
    x_all = panel.matrix()
    y_all = panel.frame["target"].to_numpy().astype(float)
    fold_col = panel.frame["fold_id"].to_numpy()
    times_all = [ensure_utc(t) for t in panel.frame["decision_at"].to_list()]
    meta_folds: list[MetaFold] = []
    predictions = np.full(x_all.shape[0], np.nan)
    for k in range(meta.min_train_folds, len(folds)):
        test_fold = folds[k]
        train_folds = folds[:k]
        calibration_fold: str | None = None
        if spec.task == "classification" and meta.calibrator != "none" and len(train_folds) >= 2:
            calibration_fold = train_folds[-1]
            train_folds = train_folds[:-1]
        idx_train = np.nonzero(np.isin(fold_col, train_folds))[0]
        idx_test = np.nonzero(fold_col == test_fold)[0]
        if idx_train.size < spec.min_fit_rows or idx_test.size == 0:
            log.append(
                f"méta-fold {test_fold} ignoré : train={idx_train.size}, test={idx_test.size} "
                f"(minimum {spec.min_fit_rows})"
            )
            continue
        train_times = [times_all[i] for i in idx_train.tolist()]
        test_times = [times_all[i] for i in idx_test.tolist()]
        # Aucun entrelacement : le méta-entraînement précède strictement le fold évalué.
        assert_temporal_split(train_times, test_times)
        test_period = Period(min(test_times), max(test_times) + timedelta(microseconds=1))
        standardizer: FittedTransformer | None = None
        x_train, x_test = x_all[idx_train], x_all[idx_test]
        if meta.standardize:
            standardizer = Standardizer()
            standardizer.fit(x_train, train_times)
            standardizer.assert_not_fitted_on(test_period)
            x_train, x_test = standardizer.transform(x_train), standardizer.transform(x_test)
        model = RidgeModel(alpha=meta.alpha)
        model.fit(x_train, y_all[idx_train])
        pred = model.predict(x_test)
        calibrator = _meta_calibrator(meta, spec.task)
        if calibrator is not None and calibration_fold is not None:
            idx_cal = np.nonzero(fold_col == calibration_fold)[0]
            if idx_cal.size:
                cal_times = [times_all[i] for i in idx_cal.tolist()]
                x_cal = x_all[idx_cal]
                if standardizer is not None:
                    x_cal = standardizer.transform(x_cal)
                p_cal = np.clip(model.predict(x_cal), 0.0, 1.0)
                calibrator.fit(np.column_stack([p_cal, y_all[idx_cal]]), cal_times)
                calibrator.assert_not_fitted_on(test_period)
                pred = np.asarray(
                    calibrator.transform(np.column_stack([np.clip(pred, 0.0, 1.0), np.zeros_like(pred)])),
                    dtype=float,
                )
            else:
                calibration_fold = None
                log.append(f"méta-fold {test_fold} : fold de calibration vide, prédictions non calibrées")
        predictions[idx_test] = pred
        component_max = max(
            ensure_utc(t) for t in panel.frame["component_max_train_at"].gather(idx_test.tolist()).to_list()
        )
        meta_folds.append(
            MetaFold(
                train_fold_ids=list(train_folds),
                calibration_fold_id=calibration_fold,
                test_fold_id=test_fold,
                rows_train=int(idx_train.size),
                rows_test=int(idx_test.size),
                metrics=split_metrics(spec.task, pred, y_all[idx_test]),
                component_max_train_at=component_max.isoformat(),
            )
        )
    if not meta_folds:
        raise ProtocolViolationError("aucun méta-fold exploitable", notes=log)
    evaluated = np.nonzero(np.isfinite(predictions))[0]
    meta_oof = (
        panel.frame.gather(evaluated.tolist())
        .select(
            pl.col("decision_at"),
            pl.col("instrument"),
            pl.col("horizon_s"),
            pl.col("fold_id"),
            pl.lit("meta").alias("producer"),
            pl.Series("prediction", predictions[evaluated]),
            pl.col("target"),
            pl.col("future_mid_return"),
            pl.col("round_trip_cost"),
            pl.col("label_available_at"),
            pl.col("component_max_train_at").alias("max_train_at"),
        )
        .cast(OOF_SCHEMA)  # type: ignore[arg-type]
    )
    meta_metrics = split_metrics(spec.task, predictions[evaluated], y_all[evaluated])
    if spec.task == "classification":
        meta_metrics["calibration"] = calibration_report(
            np.clip(predictions[evaluated], 0.0, 1.0), y_all[evaluated]
        )
    expert_metrics: dict[str, Any] = {}
    for name in panel.names:
        col = panel.frame[f"{name}__prediction"].to_numpy().astype(float)
        expert_metrics[name] = split_metrics(spec.task, col[evaluated], y_all[evaluated])
    synthetic = any(e.result.synthetic for e in panel.experts)
    if synthetic:
        log.append(SYNTHETIC_NOTICE)
    return StackingResult(
        spec=spec.to_dict(),
        meta_spec=meta.to_dict(),
        panel=panel,
        meta_folds=meta_folds,
        meta_oof=meta_oof,
        meta_metrics=meta_metrics,
        expert_metrics=expert_metrics,
        synthetic=synthetic,
        notes=log,
    )


def stack_experts(
    dataset: ResearchDataset,
    spec: TrainingSpec,
    *,
    experts: Sequence[ExpertSpec] = DEFAULT_EXPERTS,
    meta: MetaSpec = MetaSpec(),
) -> StackingResult:
    """Chaîne complète : experts → panneau OOF temporel → méta-modèle verrouillé."""
    notes: list[str] = []
    panel = build_oof_panel(dataset, spec, experts=experts, notes=notes)
    return train_meta_model(panel, spec, meta=meta, notes=notes)


def component_dates_per_row(panel: OofPanel) -> pl.DataFrame:
    """Dates maximales utilisées par chaque composant, ligne par ligne (§56) : la preuve est consultable."""
    cols = ["decision_at", "instrument", "fold_id", "component_max_train_at"]
    cols += [f"{n}__max_train_at" for n in panel.names]
    return panel.frame.select(cols)


def periods_covered(panel: OofPanel) -> dict[str, dict[str, str]]:
    """Périodes couvertes par fold : utile pour lier un rapport à une période (registre d'expériences)."""
    out: dict[str, dict[str, str]] = {}
    for fold_id in panel.fold_ids:
        rows = panel.frame.filter(pl.col("fold_id") == fold_id)
        out[fold_id] = _period_of(rows).to_dict()
    return out


def oof_period(frame: pl.DataFrame) -> Period:
    """Période couverte par un ensemble de prédictions OOF (pour l'enregistrement d'un rapport)."""
    if frame.height == 0:
        raise ProtocolViolationError("aucune prédiction : pas de période à déclarer")
    return _period_of(frame)


def latest_component_date(panel: OofPanel) -> datetime:
    return max(ensure_utc(t) for t in panel.frame["component_max_train_at"].to_list())
