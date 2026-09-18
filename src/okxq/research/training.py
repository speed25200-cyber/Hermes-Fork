"""Entraînement walk-forward, sélection hors période finale, calibration et carte de modèle (§39.1, §56).

POURQUOI un module séparé du moteur de décision : un entraînement coûteux ne doit jamais tourner dans
la boucle d'une minute (§56). Ce module lit un ``ResearchDataset`` déjà assemblé point-in-time
(``okxq.research.datasets``) et n'écrit que des artefacts JSON — jamais de pickle, parce qu'un artefact
de modèle doit rester relisible, diffable et vérifiable par hash.

Garanties temporelles (chacune a un test qui échoue si on la retire) :
- ``feature.available_at <= decision_at`` est vérifié à l'extraction (``assert_features_precede_decisions``,
  T14) : une feature arrivée après la décision est une fuite, pas une donnée ;
- les frontières viennent de ``nested_walk_forward`` : elles sont COMMUNES à tous les actifs et la purge
  vaut au moins ``horizon max + dépendances de politique`` (§39.1). Aucun tirage aléatoire de lignes ;
- le pipeline de transformation (imputation, winsorisation, sélection, normalisation) est ajusté sur le
  SEUL entraînement, puis ``assert_not_fitted_on(fold.test)`` le prouve (T17) ;
- les hyperparamètres et le seuil ne sont choisis que sur la validation ; la période de test du fold
  mesure le résultat de cette sélection et alimente les prédictions OOF (§19, T20) ;
- le modèle publié est réajusté uniquement sur les lignes antérieures à la période finale gelée, purge
  comprise : il n'a jamais vu le test final.

Déterminisme : toute l'aléa vient de ``TrainingSpec.seed`` (``cfg.research.random_seed``). Même jeu de
données, même graine, même spécification ⇒ mêmes prédictions et mêmes métriques.

Unités : ``target`` et ``prediction`` sont des FRACTIONS de notionnel (rendements), pas des montants.
Aucun montant monétaire n'est produit ici ; la conversion en ``Money`` (Decimal) appartient au ledger.
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import ArtifactIntegrityError, LeakageError, ProtocolViolationError
from okxq.domain.ids import payload_hash, sha256_hex
from okxq.research import SYNTHETIC_NOTICE
from okxq.research.baselines import BaseModel, Task, candidate_grid, information_coefficient
from okxq.research.calibration import (
    IsotonicCalibrator,
    PlattCalibrator,
    brier_score,
    calibration_report,
    log_loss,
)
from okxq.research.datasets import ResearchDataset
from okxq.research.evaluation import (
    PnlResult,
    SignalPolicy,
    assert_features_precede_decisions,
    simulate_pnl,
)
from okxq.research.labels import LabelQuality, LabelSpec
from okxq.research.splits import (
    SPLIT_TEST,
    SPLIT_TRAIN,
    SPLIT_VALIDATION,
    FittedTransformer,
    Fold,
    MedianImputer,
    Period,
    Standardizer,
    TransformerPipeline,
    VarianceSelector,
    WalkForwardSpec,
    Winsorizer,
    assert_temporal_split,
    assign_split,
    nested_walk_forward,
)

DEFAULT_SEED = 25200
MIN_FIT_ROWS = 30
CLASSIFICATION_TARGETS: frozenset[str] = frozenset({"exceeds_costs", "maker_filled"})
REGRESSION_CANDIDATES: tuple[str, ...] = ("flat", "ridge", "momentum", "mean_reversion", "lightgbm")
CLASSIFICATION_CANDIDATES: tuple[str, ...] = ("logistic", "lightgbm_classifier")
TRACKED_LIBRARIES: tuple[str, ...] = ("numpy", "polars", "scikit-learn", "lightgbm")


class ModelStatus(StrEnum):
    """Statuts de promotion (§56). Aucun nom de dossier ne remplace un statut : la preuve est enregistrée."""

    CANDIDATE = "CANDIDATE"
    VALIDATED_OFFLINE = "VALIDATED_OFFLINE"
    SHADOW = "SHADOW"
    DEMO_TECH_VALIDATED = "DEMO_TECH_VALIDATED"
    LIVE_APPROVED = "LIVE_APPROVED"
    RETIRED = "RETIRED"


def library_versions() -> dict[str, str | None]:
    """Versions des bibliothèques qui changent un résultat. Une version inconnue vaut ``None``."""
    out: dict[str, str | None] = {}
    for name in TRACKED_LIBRARIES:
        try:
            out[name] = package_version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


def code_commit() -> str | None:
    """Commit du code, fourni par l'environnement (``OKXQ_CODE_COMMIT``). Inconnu ⇒ ``None``, jamais inventé."""
    value = os.environ.get("OKXQ_CODE_COMMIT", "").strip()
    return value or None


@dataclass(frozen=True, slots=True)
class TrainingSpec:
    """Plan d'entraînement : cible, horizon, candidats, budget d'essais et frontières temporelles."""

    walk_forward: WalkForwardSpec
    target: str = "future_mid_return"
    horizon_s: int = 300
    candidates: tuple[str, ...] = ("flat", "ridge")
    seed: int = DEFAULT_SEED
    max_trials: int = 30
    final_test_start: datetime | None = None
    policy_dependency_s: int = 0
    winsor_quantiles: tuple[float, float] = (0.01, 0.99)
    max_missing_fraction: float = 0.5
    calibrator: str = "platt"
    min_fit_rows: int = MIN_FIT_ROWS

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("au moins un candidat est requis")
        allowed = CLASSIFICATION_CANDIDATES if self.task == "classification" else REGRESSION_CANDIDATES
        unknown = [c for c in self.candidates if c not in allowed]
        if unknown:
            raise ValueError(
                f"candidats {unknown} incompatibles avec la tâche {self.task} (attendus : {allowed})"
            )
        if self.max_trials <= 0:
            raise ValueError("max_trials > 0 requis")
        if self.calibrator not in ("platt", "isotonic", "none"):
            raise ValueError("calibrator ∈ {platt, isotonic, none}")

    @property
    def task(self) -> Task:
        """Cible binaire ⇒ probabilité ; sinon rendement. Le nom de la cible porte cette sémantique."""
        return "classification" if self.target in CLASSIFICATION_TARGETS else "regression"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "task": self.task,
            "horizon_s": self.horizon_s,
            "candidates": list(self.candidates),
            "seed": self.seed,
            "max_trials": self.max_trials,
            "final_test_start": self.final_test_start.isoformat() if self.final_test_start else None,
            "policy_dependency_s": self.policy_dependency_s,
            "winsor_quantiles": list(self.winsor_quantiles),
            "max_missing_fraction": self.max_missing_fraction,
            "calibrator": self.calibrator,
            "walk_forward": {
                "train_s": self.walk_forward.train_s,
                "validation_s": self.walk_forward.validation_s,
                "test_s": self.walk_forward.test_s,
                "purge_s": self.walk_forward.purge_s,
                "embargo_s": self.walk_forward.embargo_s,
                "step_s": self.walk_forward.step_s,
            },
        }


@dataclass(frozen=True, slots=True)
class TrainingMatrix:
    """Matrice d'apprentissage d'UN horizon : lignes non censurées, features brutes (``NaN`` = absent)."""

    frame: pl.DataFrame
    x: np.ndarray
    y: np.ndarray
    decision_at: list[datetime]
    feature_names: list[str]

    @property
    def rows(self) -> int:
        return int(self.x.shape[0])


@dataclass(frozen=True, slots=True)
class Trial:
    """Un essai enregistré. La plateforme journalise TOUS les essais, pas seulement le gagnant (§39.2)."""

    trial_id: str
    fold_id: str
    candidate: str
    model_name: str
    hyperparameters: dict[str, Any]
    train_rows: int
    validation_rows: int
    validation_metrics: dict[str, Any]
    selection_score: float
    selected: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "fold_id": self.fold_id,
            "candidate": self.candidate,
            "model_name": self.model_name,
            "hyperparameters": self.hyperparameters,
            "train_rows": self.train_rows,
            "validation_rows": self.validation_rows,
            "validation_metrics": self.validation_metrics,
            "selection_score": self.selection_score,
            "selected": self.selected,
        }


@dataclass
class FoldOutcome:
    """Résultat d'un fold : essais, modèle retenu, métriques de test et prédictions OOF."""

    fold: Fold
    trials: list[Trial]
    selected: Trial
    model: BaseModel
    pipeline: TransformerPipeline
    calibrator: FittedTransformer | None
    test_metrics: dict[str, Any]
    oof: pl.DataFrame
    max_train_at: datetime
    max_label_available_at: datetime | None
    rows_by_split: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold.to_dict(),
            "rows_by_split": self.rows_by_split,
            "selected": self.selected.to_dict(),
            "trials": [t.to_dict() for t in self.trials],
            "test_metrics": self.test_metrics,
            "transformations": self.pipeline.describe(),
            "calibrator": self.calibrator.to_dict()["fit_period"] if self.calibrator else None,
            "max_train_at": self.max_train_at.isoformat(),
            "max_label_available_at": (
                self.max_label_available_at.isoformat() if self.max_label_available_at else None
            ),
        }


OOF_SCHEMA: dict[str, pl.DataType] = {
    "decision_at": pl.Datetime("us", "UTC"),
    "instrument": pl.String(),
    "horizon_s": pl.Int64(),
    "fold_id": pl.String(),
    "producer": pl.String(),
    "prediction": pl.Float64(),
    "target": pl.Float64(),
    "future_mid_return": pl.Float64(),
    "round_trip_cost": pl.Float64(),
    "label_available_at": pl.Datetime("us", "UTC"),
    "max_train_at": pl.Datetime("us", "UTC"),
}


def empty_oof() -> pl.DataFrame:
    return pl.DataFrame(schema=OOF_SCHEMA)


@dataclass
class TrainingResult:
    """Sortie complète d'un entraînement walk-forward, sérialisable et rejouable."""

    spec: dict[str, Any]
    dataset_manifest: dict[str, Any]
    folds: list[FoldOutcome]
    oof: pl.DataFrame
    final_model: BaseModel
    final_pipeline: TransformerPipeline
    final_calibrator: FittedTransformer | None
    feature_names: list[str]
    final_period: Period
    residual_std: float
    residual_quantiles: dict[str, float]
    oof_metrics: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    @property
    def trials(self) -> list[Trial]:
        return [t for f in self.folds for t in f.trials]

    @property
    def synthetic(self) -> bool:
        return bool(self.dataset_manifest.get("synthetic"))

    def model_card(self) -> dict[str, Any]:
        """Carte de modèle (§56) : tout ce sans quoi un modèle n'est pas rejouable ni promouvable."""
        card: dict[str, Any] = {
            "family": self.final_model.family,
            "task": self.final_model.task,
            "model_name": self.final_model.name,
            "hyperparameters": self.final_model.hyperparameters,
            "code_commit": code_commit(),
            "dataset_hash": self.dataset_manifest.get("dataset_hash"),
            "feature_schema_hash": self.dataset_manifest.get("schema_hash"),
            "quality_level": self.dataset_manifest.get("quality_level"),
            "latency_assumed": self.dataset_manifest.get("LATENCY_ASSUMED"),
            "period_start": self.final_period.start.isoformat(),
            "period_end": self.final_period.end.isoformat(),
            "universe": self.dataset_manifest.get("instruments"),
            "universe_version": self.dataset_manifest.get("spec", {}).get("universe_version"),
            "feature_names": list(self.feature_names),
            "feature_count": len(self.feature_names),
            "transformations": self.final_pipeline.describe(),
            "labels": {
                "target": self.spec["target"],
                "horizon_s": self.spec["horizon_s"],
                "cost_version": self.dataset_manifest.get("cost_version"),
                "overlap_factor": self.dataset_manifest.get("overlap_factor_by_horizon", {}).get(
                    str(self.spec["horizon_s"])
                ),
            },
            "folds": [f.fold.to_dict() for f in self.folds],
            "fold_ids": [f.fold.fold_id for f in self.folds],
            "seed": self.spec["seed"],
            "spec": self.spec,
            "library_versions": library_versions(),
            "trials_recorded": len(self.trials),
            "metrics": self.oof_metrics,
            "residual_std": self.residual_std,
            "residual_quantiles": self.residual_quantiles,
            "promotion_status": ModelStatus.CANDIDATE.value,
            "known_limits": self.known_limits(),
            "synthetic": self.synthetic,
            "notice": SYNTHETIC_NOTICE if self.synthetic else None,
            "notes": list(self.notes),
        }
        card["model_id"] = deterministic_model_id(card)
        return card

    def known_limits(self) -> list[str]:
        """Limites connues, écrites dans la carte : un modèle sans limites déclarées n'est pas publiable."""
        limits = [
            "aucun impact de marché modélisé : les coûts sont une borne basse (cost-spread-fee-v1)",
            "labels chevauchants : le nombre effectif d'observations est divisé par le facteur de chevauchement",
            "sélection d'hyperparamètres sur la validation : le risque de sélection multiple reste à borner",
        ]
        if self.synthetic:
            limits.append(f"{SYNTHETIC_NOTICE} : ce modèle ne mesure aucun avantage de marché")
        if self.dataset_manifest.get("LATENCY_ASSUMED"):
            limits.append("LATENCY_ASSUMED : les available_at sont supposés, non mesurés")
        if self.spec.get("final_test_start") is None:
            limits.append("aucune période finale gelée déclarée : aucun test indépendant disponible")
        return limits

    def summary(self) -> dict[str, Any]:
        return {
            "target": self.spec["target"],
            "horizon_s": self.spec["horizon_s"],
            "folds": len(self.folds),
            "trials": len(self.trials),
            "oof_rows": int(self.oof.height),
            "selected_by_fold": {f.fold.fold_id: f.selected.model_name for f in self.folds},
            "oof_metrics": self.oof_metrics,
            "final_period": self.final_period.to_dict(),
            "synthetic": self.synthetic,
            "notice": SYNTHETIC_NOTICE if self.synthetic else None,
            "notes": list(self.notes),
        }


def deterministic_model_id(card: dict[str, Any]) -> str:
    """Identifiant dérivé du CONTENU de la carte : même recherche ⇒ même identifiant (T70)."""
    body = {k: v for k, v in card.items() if k not in ("model_id", "library_versions")}
    return f"mdl_{payload_hash(body)[:24]}"


# --- pipeline de transformation ---------------------------------------------------------------------------


def build_pipeline(spec: TrainingSpec) -> TransformerPipeline:
    """Imputation explicite → winsorisation → sélection de variance → normalisation.

    L'ordre compte : on impute AVANT de winsoriser pour que les quantiles ne soient pas calculés sur des
    colonnes trouées, et on sélectionne AVANT de normaliser pour ne pas diviser par un écart-type nul.
    Aucune ACP : elle détruirait la traçabilité nom↔colonne dont les baselines mono-feature ont besoin.
    """
    lower, upper = spec.winsor_quantiles
    return TransformerPipeline(
        [
            MedianImputer(),
            Winsorizer(lower_q=lower, upper_q=upper),
            VarianceSelector(max_missing_fraction=spec.max_missing_fraction),
            Standardizer(),
        ]
    )


def pipeline_feature_names(pipeline: TransformerPipeline, raw_names: Sequence[str]) -> list[str]:
    """Noms de colonnes en sortie de pipeline : une baseline mono-feature doit viser la BONNE colonne."""
    names = list(raw_names)
    for step in pipeline.steps:
        if isinstance(step, VarianceSelector):
            names = [names[i] for i in step.keep.tolist()]
    return names


def _fit_pipeline(pipeline: TransformerPipeline, x: np.ndarray, times: Sequence[datetime]) -> np.ndarray:
    # Les colonnes entièrement absentes font émettre un avertissement numpy attendu (médiane d'un vide) ;
    # la sélection de variance les retire juste après. On tait l'avertissement, pas le fait.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return pipeline.fit_transform(x, times)


# --- extraction -------------------------------------------------------------------------------------------


def build_matrix(
    dataset: ResearchDataset, spec: TrainingSpec, *, feature_names: Sequence[str] | None = None
) -> TrainingMatrix:
    """Lignes d'un seul horizon, labels ``OK`` uniquement, causalité des features vérifiée (T14, T21)."""
    rows = dataset.rows_for_horizon(spec.horizon_s)
    if rows.height == 0:
        raise ProtocolViolationError(
            "aucune ligne pour cet horizon", horizon_s=spec.horizon_s, available=dataset.horizons_s
        )
    if spec.target not in rows.columns:
        raise ProtocolViolationError("cible absente du jeu de données", target=spec.target)
    # T14 : une feature disponible APRÈS la décision n'existait pas pour le système à cet instant.
    assert_features_precede_decisions(rows["available_at"].to_list(), rows["decision_at"].to_list())
    usable = rows.filter(
        (pl.col("label_quality") == LabelQuality.OK.value)
        & pl.col(spec.target).is_not_null()
        & pl.col("round_trip_cost").is_not_null()
        & pl.col("future_mid_return").is_not_null()
    ).sort(["decision_at", "instrument"])
    if usable.height == 0:
        raise ProtocolViolationError(
            "toutes les lignes sont censurées ou masquées : rien à apprendre (jamais imputé par zéro)",
            horizon_s=spec.horizon_s,
        )
    names = list(feature_names) if feature_names is not None else list(dataset.feature_names)
    missing_cols = [n for n in names if n not in usable.columns]
    if missing_cols:
        raise ProtocolViolationError("features absentes du jeu de données", missing=missing_cols)
    x = usable.select(names).to_numpy().astype(float)
    y = usable[spec.target].to_numpy().astype(float)
    return TrainingMatrix(
        frame=usable, x=x, y=y, decision_at=list(usable["decision_at"].to_list()), feature_names=names
    )


# --- métriques et sélection -------------------------------------------------------------------------------


def split_metrics(task: Task, pred: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """Métriques d'un sous-ensemble. Une métrique non calculable vaut ``None``, jamais 0."""
    out: dict[str, Any] = {"rows": int(pred.size)}
    if pred.size == 0:
        return out
    out["ic"] = information_coefficient(pred, y)
    if task == "classification":
        p = np.clip(pred, 0.0, 1.0)
        out["brier"] = brier_score(p, y)
        out["log_loss"] = log_loss(p, y)
        out["base_rate"] = float(np.mean(y))
    else:
        out["mse"] = float(np.mean((pred - y) ** 2))
        out["mean_prediction"] = float(np.mean(pred))
        out["std_prediction"] = float(np.std(pred))
    return out


def selection_score(task: Task, metrics: dict[str, Any]) -> float:
    """Score de sélection : IC en régression, ``-Brier`` en classification. Plus grand = meilleur."""
    if task == "classification":
        brier = metrics.get("brier")
        return -float(brier) if brier is not None else float("-inf")
    ic = metrics.get("ic")
    return float(ic) if ic is not None else float("-inf")


def _calibrator(spec: TrainingSpec) -> FittedTransformer | None:
    if spec.task != "classification" or spec.calibrator == "none":
        return None
    return PlattCalibrator() if spec.calibrator == "platt" else IsotonicCalibrator()


def _apply_calibrator(calibrator: FittedTransformer | None, pred: np.ndarray) -> np.ndarray:
    if calibrator is None:
        return pred
    stacked = np.column_stack([np.clip(pred, 0.0, 1.0), np.zeros_like(pred)])
    return np.asarray(calibrator.transform(stacked), dtype=float)


# --- entraînement d'un fold -------------------------------------------------------------------------------


def _fit_fold(
    matrix: TrainingMatrix, fold: Fold, spec: TrainingSpec, *, producer: str, trial_offset: int
) -> FoldOutcome | str:
    """Ajuste un fold. Rend une chaîne de motif si le fold est inexploitable (jamais un résultat inventé)."""
    split = assign_split(matrix.frame, fold)
    frame = matrix.frame.with_columns(split.alias("split"))
    mask = split.to_numpy()
    idx_train = np.nonzero(mask == SPLIT_TRAIN)[0]
    idx_val = np.nonzero(mask == SPLIT_VALIDATION)[0]
    idx_test = np.nonzero(mask == SPLIT_TEST)[0]
    rows_by_split = {str(name): int(np.sum(mask == name)) for name in sorted(set(mask.tolist()))}
    if idx_train.size < spec.min_fit_rows or idx_val.size == 0 or idx_test.size == 0:
        return (
            f"{fold.fold_id} ignoré : train={idx_train.size}, validation={idx_val.size}, "
            f"test={idx_test.size} (minimum {spec.min_fit_rows} lignes d'entraînement)"
        )
    times = matrix.decision_at
    train_times = [times[i] for i in idx_train.tolist()]
    val_times = [times[i] for i in idx_val.tolist()]
    test_times = [times[i] for i in idx_test.tolist()]
    # §39.1 : aucune ligne d'entraînement ne suit le début du test, et l'écart vaut au moins la purge.
    assert_temporal_split(train_times, test_times, gap_s=fold.purge_s)
    assert_temporal_split(val_times, test_times)
    pipeline = build_pipeline(spec)
    x_train = _fit_pipeline(pipeline, matrix.x[idx_train], train_times)
    # T17 : la provenance prouve que la transformation n'a pas vu la période évaluée.
    pipeline.assert_not_fitted_on(fold.test)
    x_val = pipeline.transform(matrix.x[idx_val])
    x_test = pipeline.transform(matrix.x[idx_test])
    y_train, y_val, y_test = matrix.y[idx_train], matrix.y[idx_val], matrix.y[idx_test]
    post_names = pipeline_feature_names(pipeline, matrix.feature_names)
    trials: list[Trial] = []
    best: tuple[float, BaseModel, Trial] | None = None
    budget = spec.max_trials
    for candidate in spec.candidates:
        try:
            grid = candidate_grid(candidate, seed=spec.seed, feature_names=post_names)
        except ValueError as exc:  # feature exigée par la baseline absente après sélection
            trials.append(
                Trial(
                    trial_id=f"tr_{trial_offset + len(trials):04d}",
                    fold_id=fold.fold_id,
                    candidate=candidate,
                    model_name=candidate,
                    hyperparameters={},
                    train_rows=int(idx_train.size),
                    validation_rows=int(idx_val.size),
                    validation_metrics={"error": str(exc)},
                    selection_score=float("-inf"),
                    selected=False,
                )
            )
            continue
        for model in grid:
            if len(trials) >= budget:
                break
            model.fit(x_train, y_train)
            pred_val = model.predict(x_val)
            metrics = split_metrics(spec.task, pred_val, y_val)
            score = selection_score(spec.task, metrics)
            trial = Trial(
                trial_id=f"tr_{trial_offset + len(trials):04d}",
                fold_id=fold.fold_id,
                candidate=candidate,
                model_name=model.name,
                hyperparameters=dict(model.hyperparameters),
                train_rows=int(idx_train.size),
                validation_rows=int(idx_val.size),
                validation_metrics=metrics,
                selection_score=score,
                selected=False,
            )
            trials.append(trial)
            # Un benchmark (« ne rien faire ») sert de référence, il n'est jamais sélectionné comme modèle.
            if not model.is_benchmark and (best is None or score > best[0]):
                best = (score, model, trial)
    if best is None:
        return f"{fold.fold_id} ignoré : aucun candidat ajustable (essais enregistrés : {len(trials)})"
    _score, model, selected_trial = best
    selected = replace(selected_trial, selected=True)
    trials = [selected if t.trial_id == selected_trial.trial_id else t for t in trials]
    calibrator = _calibrator(spec)
    if calibrator is not None:
        # §56 : la calibration se règle sur un ensemble DISTINCT (ici la validation), jamais sur le test.
        pred_val = np.clip(model.predict(x_val), 0.0, 1.0)
        calibrator.fit(np.column_stack([pred_val, y_val]), val_times)
        calibrator.assert_not_fitted_on(fold.test)
    pred_test = _apply_calibrator(calibrator, model.predict(x_test))
    test_metrics = split_metrics(spec.task, pred_test, y_test)
    if spec.task == "classification":
        test_metrics["calibration"] = calibration_report(np.clip(pred_test, 0.0, 1.0), y_test)
    max_train_at = max(ensure_utc(t) for t in train_times + val_times)
    label_avail = frame.filter(pl.col("split").is_in([SPLIT_TRAIN, SPLIT_VALIDATION]))[
        "label_available_at"
    ].drop_nulls()
    max_label_available_at = ensure_utc(max(label_avail.to_list())) if label_avail.len() else None
    test_rows = frame.filter(pl.col("split") == SPLIT_TEST)
    oof = test_rows.select(
        pl.col("decision_at"),
        pl.col("instrument"),
        pl.col("horizon_s"),
        pl.lit(fold.fold_id).alias("fold_id"),
        pl.lit(producer).alias("producer"),
        pl.Series("prediction", pred_test),
        pl.col(spec.target).alias("target"),
        pl.col("future_mid_return"),
        pl.col("round_trip_cost"),
        pl.col("label_available_at"),
        pl.lit(max_train_at).cast(pl.Datetime("us", "UTC")).alias("max_train_at"),
    ).cast(OOF_SCHEMA)  # type: ignore[arg-type]
    return FoldOutcome(
        fold=fold,
        trials=trials,
        selected=selected,
        model=model,
        pipeline=pipeline,
        calibrator=calibrator,
        test_metrics=test_metrics,
        oof=oof,
        max_train_at=max_train_at,
        max_label_available_at=max_label_available_at,
        rows_by_split=rows_by_split,
    )


def assert_oof_is_out_of_fold(oof: pl.DataFrame) -> None:
    """T20 : aucun composant ne connaît la fenêtre qu'il prédit (``max_train_at < decision_at``)."""
    if oof.height == 0:
        return
    bad = oof.filter(pl.col("max_train_at") >= pl.col("decision_at"))
    if bad.height:
        first = bad.row(0, named=True)
        raise LeakageError(
            "prédiction OOF produite par un modèle entraîné jusqu'à (ou après) la décision prédite",
            rows=int(bad.height),
            decision_at=str(first["decision_at"]),
            max_train_at=str(first["max_train_at"]),
        )


def _final_fit_indices(matrix: TrainingMatrix, spec: TrainingSpec, folds: Sequence[Fold]) -> np.ndarray:
    """Lignes autorisées pour le modèle publié : strictement avant la période finale gelée, purge incluse."""
    limit = ensure_utc(spec.final_test_start) if spec.final_test_start is not None else None
    if limit is None:
        limit = max(f.test.end for f in folds) if folds else None
    if limit is None:
        return np.arange(matrix.rows)
    purge = max(f.purge_s for f in folds) if folds else spec.horizon_s + spec.policy_dependency_s
    cut = limit - timedelta(seconds=purge)
    times = np.array([ensure_utc(t).timestamp() for t in matrix.decision_at], dtype=float)
    avail = matrix.frame["label_available_at"].to_list()
    avail_s = np.array([ensure_utc(a).timestamp() if a is not None else -np.inf for a in avail], dtype=float)
    ok = (times < cut.timestamp()) & (avail_s <= limit.timestamp())
    return np.nonzero(ok)[0]


def walk_forward_train(
    dataset: ResearchDataset,
    spec: TrainingSpec,
    *,
    feature_names: Sequence[str] | None = None,
    producer: str = "model",
) -> TrainingResult:
    """Entraînement walk-forward complet : essais, OOF temporel, modèle publiable et carte de modèle."""
    matrix = build_matrix(dataset, spec, feature_names=feature_names)
    folds = nested_walk_forward(
        dataset.period_start,
        dataset.period_end,
        spec.walk_forward,
        max_horizon_s=spec.horizon_s,
        policy_dependency_s=spec.policy_dependency_s,
        final_test_start=spec.final_test_start,
    )
    outcomes: list[FoldOutcome] = []
    notes: list[str] = []
    for fold in folds:
        result = _fit_fold(
            matrix,
            fold,
            spec,
            producer=producer,
            trial_offset=sum(len(o.trials) for o in outcomes),
        )
        if isinstance(result, str):
            notes.append(result)
            continue
        outcomes.append(result)
    if not outcomes:
        raise ProtocolViolationError(
            "aucun fold exploitable : période trop courte ou lignes trop peu nombreuses", notes=notes
        )
    oof = pl.concat([o.oof for o in outcomes]).sort(["decision_at", "instrument"])
    assert_oof_is_out_of_fold(oof)
    # Modèle publié : on reprend le candidat retenu par le fold le PLUS RÉCENT (celui dont la sélection
    # a été mesurée le plus près du présent), réajusté sur toutes les lignes antérieures au test final.
    last = outcomes[-1]
    final_idx = _final_fit_indices(matrix, spec, folds)
    if final_idx.size < spec.min_fit_rows:
        raise ProtocolViolationError(
            "trop peu de lignes hors période finale pour publier un modèle",
            rows=int(final_idx.size),
            minimum=spec.min_fit_rows,
        )
    final_times = [matrix.decision_at[i] for i in final_idx.tolist()]
    final_pipeline = build_pipeline(spec)
    x_final = _fit_pipeline(final_pipeline, matrix.x[final_idx], final_times)
    final_names = pipeline_feature_names(final_pipeline, matrix.feature_names)
    grid = candidate_grid(last.selected.candidate, seed=spec.seed, feature_names=final_names)
    final_model = next(
        (m for m in grid if m.name == last.selected.model_name),
        grid[0],
    )
    final_model.fit(x_final, matrix.y[final_idx])
    final_period = Period(min(final_times), max(final_times) + timedelta(microseconds=1))
    final_calibrator = _calibrator(spec)
    if final_calibrator is not None:
        pred = np.clip(final_model.predict(x_final), 0.0, 1.0)
        final_calibrator.fit(np.column_stack([pred, matrix.y[final_idx]]), final_times)
    if spec.final_test_start is not None:
        guard = Period(
            ensure_utc(spec.final_test_start),
            max(ensure_utc(spec.final_test_start) + timedelta(microseconds=1), dataset.period_end),
        )
        final_pipeline.assert_not_fitted_on(guard)
    # Incertitude publiée : dispersion des résidus HORS échantillon (OOF), jamais in-sample.
    residuals = (oof["target"] - oof["prediction"]).to_numpy().astype(float)
    residuals = residuals[np.isfinite(residuals)]
    residual_std = float(np.std(residuals)) if residuals.size > 1 else 0.0
    residual_quantiles = (
        {str(q): float(np.quantile(residuals, q)) for q in (0.1, 0.5, 0.9)} if residuals.size else {}
    )
    oof_metrics = split_metrics(
        spec.task, oof["prediction"].to_numpy().astype(float), oof["target"].to_numpy().astype(float)
    )
    oof_metrics["folds"] = len(outcomes)
    overlap = dataset.overlap.get(spec.horizon_s, 1.0)
    oof_metrics["n_effective"] = float(oof.height / overlap)
    if dataset.synthetic:
        notes.append(SYNTHETIC_NOTICE)
    return TrainingResult(
        spec=spec.to_dict(),
        dataset_manifest=dataset.manifest(),
        folds=outcomes,
        oof=oof,
        final_model=final_model,
        final_pipeline=final_pipeline,
        final_calibrator=final_calibrator,
        feature_names=list(matrix.feature_names),
        final_period=final_period,
        residual_std=residual_std,
        residual_quantiles=residual_quantiles,
        oof_metrics=oof_metrics,
        notes=notes,
    )


# --- évaluation économique des prédictions OOF ------------------------------------------------------------


def oof_weights(oof: pl.DataFrame, policy: SignalPolicy) -> np.ndarray:
    """Poids signés depuis ``prediction`` : le seuil est un multiple du coût aller-retour (§21)."""
    mu = oof["prediction"].to_numpy().astype(float)
    cost = oof["round_trip_cost"].to_numpy().astype(float)
    return policy.weights(mu, cost)


def evaluate_oof(
    oof: pl.DataFrame,
    *,
    cutoff_interval_s: int,
    policy: SignalPolicy | None = None,
    cost_multiplier: float = 1.0,
    fee_rate: float | None = None,
) -> PnlResult:
    """PnL NET des prédictions OOF (frais + spread inclus par construction, §37). Fractions, pas de montants."""
    pol = policy if policy is not None else SignalPolicy()
    frame = oof.with_columns(pl.Series("weight", oof_weights(oof, pol)))
    return simulate_pnl(
        frame,
        cutoff_interval_s=cutoff_interval_s,
        cost_multiplier=cost_multiplier,
        fee_rate=fee_rate,
    )


# --- artefact de modèle -----------------------------------------------------------------------------------


@dataclass
class ModelArtifact:
    """Artefact complet : modèle ET transformateurs ET schéma de features ET carte.

    Un rollback qui ne restaure pas les quatre est inutilisable en direct (§56) : ils voyagent ensemble
    dans un unique fichier JSON, protégé par son SHA-256.
    """

    model_id: str
    card: dict[str, Any]
    model: BaseModel
    pipeline: TransformerPipeline
    calibrator: FittedTransformer | None
    feature_names: list[str]
    feature_schema_hash: str
    residual_std: float
    residual_quantiles: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_version": 1,
            "model_id": self.model_id,
            "card": self.card,
            "model": self.model.to_dict(),
            "pipeline": self.pipeline.to_dict(),
            "calibrator": self.calibrator.to_dict() if self.calibrator else None,
            "feature_names": list(self.feature_names),
            "feature_schema_hash": self.feature_schema_hash,
            "residual_std": self.residual_std,
            "residual_quantiles": self.residual_quantiles,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ModelArtifact:
        if int(payload.get("artifact_version", 0)) != 1:
            raise ArtifactIntegrityError(
                "version d'artefact inconnue", version=payload.get("artifact_version")
            )
        calibrator_payload = payload.get("calibrator")
        return cls(
            model_id=str(payload["model_id"]),
            card=dict(payload["card"]),
            model=BaseModel.from_dict(payload["model"]),
            pipeline=TransformerPipeline.from_dict(payload["pipeline"]),
            calibrator=(
                FittedTransformer.from_dict(calibrator_payload) if calibrator_payload is not None else None
            ),
            feature_names=list(payload["feature_names"]),
            feature_schema_hash=str(payload["feature_schema_hash"]),
            residual_std=float(payload["residual_std"]),
            residual_quantiles={str(k): float(v) for k, v in dict(payload["residual_quantiles"]).items()},
        )

    def body(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)

    def sha256(self) -> str:
        return sha256_hex(self.body())

    def save(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = self.body()
        path.write_text(body, encoding="utf-8")
        return sha256_hex(body)

    @classmethod
    def load(cls, path: Path, *, expected_sha256: str | None = None) -> ModelArtifact:
        """Chargement vérifié : un artefact dont le hash ne correspond pas au manifeste est REFUSÉ."""
        if not path.exists():
            raise ArtifactIntegrityError("artefact de modèle absent", path=str(path))
        body = path.read_text(encoding="utf-8")
        digest = sha256_hex(body)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ArtifactIntegrityError(
                "hash d'artefact différent du manifeste : chargement refusé",
                path=str(path),
                expected=expected_sha256,
                found=digest,
            )
        return cls.from_dict(json.loads(body))

    def predict(self, x: np.ndarray) -> np.ndarray:
        """Prédiction brute : transformations puis modèle puis calibration éventuelle."""
        out = self.model.predict(self.pipeline.transform(x))
        return _apply_calibrator(self.calibrator, out)


def artifact_from_result(result: TrainingResult) -> ModelArtifact:
    card = result.model_card()
    schema_hash = card.get("feature_schema_hash")
    if not schema_hash:
        raise ProtocolViolationError(
            "jeu de données sans hash de schéma de features : artefact non publiable"
        )
    return ModelArtifact(
        model_id=str(card["model_id"]),
        card=card,
        model=result.final_model,
        pipeline=result.final_pipeline,
        calibrator=result.final_calibrator,
        feature_names=list(result.feature_names),
        feature_schema_hash=str(schema_hash),
        residual_std=result.residual_std,
        residual_quantiles=result.residual_quantiles,
    )


# --- spécification de labels utilisée par les jobs de recherche -------------------------------------------

RESEARCH_ENTRY_DELAY_S = 1
RESEARCH_ENTRY_WINDOW_S = 120
# Horizon minimal exploitable avec un chemin de prix échantillonné à la minute : il faut une barre
# d'entrée ET une barre de sortie postérieure, donc au moins trois pas de grille. Un horizon de 60 s
# exigerait un chemin de prix plus fin que la minute ; le prétendre serait inventer une observation.
MIN_RESEARCH_HORIZON_S = 3 * 60


def usable_horizons(horizons_s: Sequence[int], *, cutoff_interval_s: int = 60) -> list[int]:
    """Horizons réellement labellisables avec ce pas de grille. Les autres sont RETIRÉS, pas approximés."""
    floor = max(MIN_RESEARCH_HORIZON_S, 3 * cutoff_interval_s)
    return sorted({int(h) for h in horizons_s if int(h) >= floor})


def default_label_specs(horizons_s: Sequence[int], *, cutoff_interval_s: int = 60) -> tuple[LabelSpec, ...]:
    """Labels des jobs de recherche : entrée retardée d'une seconde, cherchée dans une fenêtre de 120 s.

    POURQUOI 120 s et non 60 s : le chemin de prix des labels est échantillonné depuis la PREMIÈRE
    disponibilité réelle des données (latence d'ingestion incluse), donc ses points tombent quelques
    centaines de millisecondes après les coupures de décision. Une fenêtre d'entrée de 60 s rejetterait
    alors toutes les entrées (``NO_ENTRY``) pour un décalage purement technique. Élargir la fenêtre
    n'avance aucune donnée : l'entrée reste strictement postérieure à la décision.
    """
    usable = usable_horizons(horizons_s, cutoff_interval_s=cutoff_interval_s)
    if not usable:
        raise ProtocolViolationError(
            "aucun horizon labellisable avec ce pas de grille : il faut au moins trois pas entre la "
            "décision et la fin d'horizon",
            requested=[int(h) for h in horizons_s],
            cutoff_interval_s=cutoff_interval_s,
            minimum_horizon_s=max(MIN_RESEARCH_HORIZON_S, 3 * cutoff_interval_s),
        )
    return tuple(
        LabelSpec(
            horizon_s=h,
            entry_delay_s=RESEARCH_ENTRY_DELAY_S,
            entry_window_s=RESEARCH_ENTRY_WINDOW_S,
        )
        for h in usable
    )


def predict_holdout(
    result: TrainingResult,
    dataset: ResearchDataset,
    spec: TrainingSpec,
    period: Period,
    *,
    feature_names: Sequence[str] | None = None,
    producer: str = "final_model",
) -> pl.DataFrame:
    """Prédit une période RÉSERVÉE avec le modèle publié, au format OOF.

    POURQUOI c'est sûr : le modèle publié a été ajusté uniquement sur des lignes antérieures à la période
    finale gelée (purge comprise). ``assert_oof_is_out_of_fold`` revérifie ici, ligne par ligne, que la
    date maximale connue du modèle précède strictement chaque décision prédite.
    """
    matrix = build_matrix(dataset, spec, feature_names=feature_names)
    times = np.array([ensure_utc(t).timestamp() for t in matrix.decision_at], dtype=float)
    selected = np.nonzero((times >= period.start.timestamp()) & (times < period.end.timestamp()))[0]
    if selected.size == 0:
        raise ProtocolViolationError("aucune ligne dans la période demandée", period=period.to_dict())
    rows = matrix.frame.gather(selected.tolist())
    pred = _apply_calibrator(
        result.final_calibrator,
        result.final_model.predict(result.final_pipeline.transform(matrix.x[selected])),
    )
    max_train_at = result.final_period.end - timedelta(microseconds=1)
    out = rows.select(
        pl.col("decision_at"),
        pl.col("instrument"),
        pl.col("horizon_s"),
        pl.lit("holdout").alias("fold_id"),
        pl.lit(producer).alias("producer"),
        pl.Series("prediction", pred),
        pl.col(spec.target).alias("target"),
        pl.col("future_mid_return"),
        pl.col("round_trip_cost"),
        pl.col("label_available_at"),
        pl.lit(max_train_at).cast(pl.Datetime("us", "UTC")).alias("max_train_at"),
    ).cast(OOF_SCHEMA)  # type: ignore[arg-type]
    assert_oof_is_out_of_fold(out)
    return out
