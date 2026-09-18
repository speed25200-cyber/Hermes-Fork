"""Prédicteur de production : charge un modèle ENREGISTRÉ et produit des ``Forecast`` (§43, §55, §56).

Ce que ce module fait : lire un artefact vérifié par son hash, reconstruire la matrice de features dans
l'ordre exact du modèle, appliquer les transformateurs ajustés puis le modèle, et rendre des ``Forecast``.

Ce que ce module ne fait PAS, et ne peut pas faire :
- il ne choisit aucun côté, aucune taille, aucun ordre : il ne construit jamais ``OrderIntent`` ni
  ``ApprovedOrder``, il n'importe même pas ces contrats. Le modèle propose, le Risk Engine décide, le
  gateway unique envoie (§1.11) ;
- il ne contourne aucun garde-fou : une feature arrivée après la coupure lève ``LeakageError`` ; un
  artefact dont le hash ne correspond pas au manifeste lève ``ArtifactIntegrityError`` ; un schéma de
  features différent de celui de l'entraînement est REFUSÉ (un rollback doit restaurer modèle ET
  transformateurs ET schéma, §56) ;
- il n'invente pas de valeur : un instrument dont trop de features manquent ne reçoit AUCUNE prévision
  (l'absence se dit par l'absence, jamais par ``mu = 0`` qui signifierait « je prédis zéro »).

Causalité vérifiée à chaque appel : ``feature.available_at <= snapshot.cutoff_at``. La seconde moitié du
contrat (``snapshot.cutoff_at <= decision.started_at``) appartient à la boucle de décision.

Unités : ``gross_mu`` et les quantiles sont des FRACTIONS de notionnel sur l'horizon du modèle, pas des
montants. Aucune valeur monétaire n'est produite ici.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from okxq.config.modes import Mode
from okxq.config.schema import AppConfig
from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import ArtifactIntegrityError, LeakageError, ProtocolViolationError
from okxq.domain.events import CostBasis, FeatureVector, Forecast, MarketSnapshot
from okxq.domain.ids import new_id
from okxq.research.experiment_registry import ExperimentRegistry
from okxq.research.training import ModelArtifact, ModelStatus

PRODUCER = "okxq.research.predictor"
DEFAULT_QUANTILES: tuple[str, ...] = ("0.1", "0.5", "0.9")


@dataclass(frozen=True, slots=True)
class PredictorPolicy:
    """Réglages de publication d'une prévision. Ils ne changent aucune décision de risque."""

    execution_policy_id: str = "exec-sim-v1"
    cost_basis: CostBasis = CostBasis.MID
    max_missing_fraction: float = 0.5
    quantile_levels: tuple[str, ...] = DEFAULT_QUANTILES

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_missing_fraction <= 1.0:
            raise ValueError("max_missing_fraction ∈ [0, 1]")


@dataclass(frozen=True, slots=True)
class SkippedInstrument:
    """Un instrument sans prévision, avec son motif. Une absence motivée vaut mieux qu'un zéro muet."""

    instrument: str
    reason: str
    missing_features: int
    total_features: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "reason": self.reason,
            "missing_features": self.missing_features,
            "total_features": self.total_features,
        }


@dataclass
class PredictionBatch:
    """Résultat d'un appel : les prévisions produites ET les instruments écartés avec leur motif."""

    forecasts: list[Forecast]
    skipped: list[SkippedInstrument] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "forecasts": [f.model_dump(mode="json") for f in self.forecasts],
            "skipped": [s.to_dict() for s in self.skipped],
        }


class RegisteredModelPredictor:
    """``Predictor`` (``okxq.domain.protocols``) alimenté par un artefact de recherche enregistré."""

    def __init__(
        self,
        artifact: ModelArtifact,
        *,
        policy: PredictorPolicy | None = None,
        horizon_s: int | None = None,
    ) -> None:
        self.artifact = artifact
        self.policy = policy if policy is not None else PredictorPolicy()
        card = artifact.card
        task = str(card.get("task", "regression"))
        if task != "regression":
            # Une probabilité n'est pas un rendement : la convertir sans modèle de gain serait une
            # invention. Un artefact de classification s'utilise via l'Expected Edge Engine, pas ici.
            raise ProtocolViolationError(
                "artefact de classification : une probabilité n'est pas un rendement attendu",
                model_id=artifact.model_id,
                task=task,
            )
        labels = card.get("labels") or {}
        resolved = horizon_s if horizon_s is not None else labels.get("horizon_s")
        if resolved is None:
            raise ProtocolViolationError(
                "artefact sans horizon de label : impossible de publier une prévision datée",
                model_id=artifact.model_id,
            )
        self.horizon_s = int(resolved)
        if self.horizon_s <= 0:
            raise ProtocolViolationError(
                "horizon de prévision non strictement positif", horizon_s=self.horizon_s
            )
        self.model_id = artifact.model_id
        self.feature_names = list(artifact.feature_names)
        self.feature_schema_hash = artifact.feature_schema_hash
        self.last_batch: PredictionBatch | None = None

    # --- construction -------------------------------------------------------------------------------

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        expected_sha256: str | None = None,
        policy: PredictorPolicy | None = None,
        horizon_s: int | None = None,
    ) -> RegisteredModelPredictor:
        """Charge un artefact depuis le disque. Le hash attendu, s'il est fourni, est VÉRIFIÉ."""
        artifact = ModelArtifact.load(path, expected_sha256=expected_sha256)
        return cls(artifact, policy=policy, horizon_s=horizon_s)

    @classmethod
    def from_registry(
        cls,
        registry: ExperimentRegistry,
        model_id: str,
        *,
        root: Path | None = None,
        policy: PredictorPolicy | None = None,
    ) -> RegisteredModelPredictor:
        """Charge le modèle tel qu'il est ENREGISTRÉ : chemin ET hash viennent du registre, pas du disque."""
        row = registry.model(model_id)
        artifact_path = row.get("artifact_path")
        if not artifact_path:
            raise ArtifactIntegrityError(
                "modèle enregistré sans artefact : inutilisable en prévision", model_id=model_id
            )
        path = Path(str(artifact_path))
        if root is not None and not path.is_absolute():
            path = root / path
        artifact = ModelArtifact.load(path, expected_sha256=row.get("artifact_sha256"))
        if artifact.model_id != model_id:
            raise ArtifactIntegrityError(
                "l'artefact ne porte pas l'identifiant demandé",
                requested=model_id,
                found=artifact.model_id,
            )
        return cls(artifact, policy=policy)

    # --- extraction point-in-time -------------------------------------------------------------------

    def _row(self, vector: FeatureVector, cutoff_at: Any) -> tuple[np.ndarray, int]:
        """Une ligne de features dans l'ordre du modèle. ``NaN`` = absent (l'imputation est explicite)."""
        if vector.schema_hash != self.feature_schema_hash:
            raise ArtifactIntegrityError(
                "schéma de features incompatible avec le modèle : rollback incomplet (§56)",
                model_id=self.model_id,
                expected=self.feature_schema_hash,
                found=vector.schema_hash,
            )
        # Contrat de causalité : une feature disponible après la coupure n'existait pas pour le système.
        if ensure_utc(vector.available_at) > ensure_utc(cutoff_at):
            raise LeakageError(
                "feature disponible après la coupure du snapshot",
                instrument=vector.instrument,
                available_at=ensure_utc(vector.available_at).isoformat(),
                cutoff_at=ensure_utc(cutoff_at).isoformat(),
            )
        values = vector.as_dict()
        missing_names = [n for n in self.feature_names if n not in values]
        if missing_names:
            raise ArtifactIntegrityError(
                "features du modèle absentes du vecteur fourni",
                model_id=self.model_id,
                missing=missing_names[:10],
            )
        # ``NaN`` marque une feature ABSENTE ; l'imputation appartient au pipeline, pas à cette lecture.
        cells: list[float] = []
        for name in self.feature_names:
            cell = values[name]
            cells.append(float("nan") if cell is None else float(cell))
        row = np.array(cells, dtype=float)
        return row, int(np.count_nonzero(~np.isfinite(row)))

    # --- prévision ----------------------------------------------------------------------------------

    def forecast_batch(self, snapshot: MarketSnapshot) -> PredictionBatch:
        """Prévisions du snapshot. Version synchrone : ``predict`` n'en est que l'enveloppe asynchrone."""
        cutoff = ensure_utc(snapshot.cutoff_at)
        total = len(self.feature_names)
        limit = int(self.policy.max_missing_fraction * total)
        rows: list[np.ndarray] = []
        instruments: list[str] = []
        skipped: list[SkippedInstrument] = []
        for instrument in snapshot.eligible_instruments:
            vector = snapshot.features.get(instrument)
            if vector is None:
                skipped.append(
                    SkippedInstrument(
                        instrument=instrument,
                        reason="aucun vecteur de features dans le snapshot",
                        missing_features=total,
                        total_features=total,
                    )
                )
                continue
            row, missing = self._row(vector, cutoff)
            if missing > limit:
                skipped.append(
                    SkippedInstrument(
                        instrument=instrument,
                        reason=f"trop de features manquantes ({missing}/{total} > {limit})",
                        missing_features=missing,
                        total_features=total,
                    )
                )
                continue
            rows.append(row)
            instruments.append(instrument)
        forecasts: list[Forecast] = []
        if rows:
            mu = self.artifact.predict(np.vstack(rows))
            uncertainty = max(0.0, float(self.artifact.residual_std))
            for instrument, value in zip(instruments, mu.tolist(), strict=True):
                forecasts.append(
                    Forecast(
                        forecast_id=new_id("fc"),
                        model_id=self.model_id,
                        snapshot_id=snapshot.snapshot_id,
                        instrument=instrument,
                        horizon_s=self.horizon_s,
                        gross_mu=float(value),
                        quantiles=self._quantiles(float(value)),
                        uncertainty=uncertainty,
                        execution_policy_id=self.policy.execution_policy_id,
                        cost_basis=self.policy.cost_basis,
                        available_at=cutoff,
                        fold_id=None,
                        producer=PRODUCER,
                    )
                )
        batch = PredictionBatch(forecasts=forecasts, skipped=skipped)
        self.last_batch = batch
        return batch

    def _quantiles(self, mu: float) -> dict[str, float]:
        """Quantiles = ``mu`` + quantiles des RÉSIDUS hors échantillon. Absents ⇒ dictionnaire vide."""
        out: dict[str, float] = {}
        for level in self.policy.quantile_levels:
            offset = self.artifact.residual_quantiles.get(level)
            if offset is None:
                continue
            out[level] = mu + float(offset)
        return out

    async def predict(self, snapshot: MarketSnapshot) -> list[Forecast]:
        """Implémentation du protocole ``Predictor``. Aucune entrée/sortie : le calcul est local."""
        return self.forecast_batch(snapshot).forecasts

    def describe(self) -> dict[str, Any]:
        """Carte d'identité publiable du prédicteur (journalisée avec chaque décision)."""
        card = self.artifact.card
        return {
            "model_id": self.model_id,
            "family": card.get("family"),
            "task": card.get("task"),
            "horizon_s": self.horizon_s,
            "feature_schema_hash": self.feature_schema_hash,
            "feature_count": len(self.feature_names),
            "dataset_hash": card.get("dataset_hash"),
            "code_commit": card.get("code_commit"),
            "promotion_status": card.get("promotion_status"),
            "residual_std": self.artifact.residual_std,
            "artifact_sha256": self.artifact.sha256(),
            "synthetic_training_data": bool(card.get("synthetic")),
            "notice": card.get("notice"),
            "known_limits": card.get("known_limits", []),
            "decides_nothing": True,
        }


def assert_snapshot_precedes_decision(snapshot: MarketSnapshot, decision_started_at: Any) -> None:
    """Seconde moitié du contrat de causalité : ``snapshot.cutoff_at <= decision.started_at`` (§34)."""
    cutoff = ensure_utc(snapshot.cutoff_at)
    started = ensure_utc(decision_started_at)
    if cutoff > started:
        raise LeakageError(
            "snapshot postérieur au début de la décision",
            cutoff_at=cutoff.isoformat(),
            started_at=started.isoformat(),
        )


def forecast_rows(batch: PredictionBatch) -> list[Mapping[str, Any]]:
    """Lignes machine-readable pour la CLI et les rapports (aucune décision, aucune taille)."""
    return [
        {
            "instrument": f.instrument,
            "horizon_s": f.horizon_s,
            "gross_mu": f.gross_mu,
            "uncertainty": f.uncertainty,
            "quantiles": dict(f.quantiles),
            "cost_basis": f.cost_basis.value,
            "available_at": f.available_at.isoformat(),
            "model_id": f.model_id,
        }
        for f in batch.forecasts
    ]


# --- point d'entrée attendu par la composition du runtime -------------------------------------------------

MODEL_ARTIFACT_ENV = "OKXQ_MODEL_ARTIFACT"
MODEL_SHA256_ENV = "OKXQ_MODEL_ARTIFACT_SHA256"
# Modes où un modèle CANDIDATE, ou entraîné sur des données synthétiques, reste acceptable : ce sont
# ceux qui n'envoient aucun ordre et ne prétendent à aucune preuve.
RESEARCH_GRADE_MODES = (Mode.RESEARCH, Mode.PAPER)
PROMOTED_STATUSES = frozenset(
    {
        ModelStatus.VALIDATED_OFFLINE.value,
        ModelStatus.SHADOW.value,
        ModelStatus.DEMO_TECH_VALIDATED.value,
        ModelStatus.LIVE_APPROVED.value,
    }
)


def build_predictor(cfg: AppConfig) -> RegisteredModelPredictor:
    """Construit le prédicteur attendu par ``okxq.runtime.composition.load_predictor``.

    Le modèle est DÉSIGNÉ par l'environnement (``OKXQ_MODEL_ARTIFACT``, et son hash attendu
    ``OKXQ_MODEL_ARTIFACT_SHA256``) : rien n'est découvert en balayant un dossier, parce qu'un nom de
    fichier comme ``best_model.json`` ne prouve rien (§56).

    Toute erreur levée ici est une ``OkxqError`` : la composition la journalise et dégrade vers
    ``NoModelPredictor``, qui produit NO_TRADE. Ne pas trader est une décision valide (§1.13) ; charger un
    modèle non prouvé n'en est pas une.
    """
    raw_path = os.environ.get(MODEL_ARTIFACT_ENV, "").strip()
    if not raw_path:
        raise ProtocolViolationError(
            f"aucun modèle désigné : poser {MODEL_ARTIFACT_ENV} sur un artefact enregistré",
            mode=cfg.mode.value,
        )
    expected = os.environ.get(MODEL_SHA256_ENV, "").strip() or None
    research_grade = cfg.mode in RESEARCH_GRADE_MODES
    if expected is None and not research_grade:
        raise ArtifactIntegrityError(
            f"hors RESEARCH/PAPER, un artefact doit venir avec son hash attendu ({MODEL_SHA256_ENV})",
            mode=cfg.mode.value,
        )
    artifact = ModelArtifact.load(Path(raw_path), expected_sha256=expected)
    if not research_grade:
        status = str(artifact.card.get("promotion_status", ""))
        if status not in PROMOTED_STATUSES:
            raise ProtocolViolationError(
                "modèle non promu : ce mode exige une promotion prouvée (§56)",
                mode=cfg.mode.value,
                promotion_status=status or None,
            )
        if artifact.card.get("synthetic"):
            raise ProtocolViolationError(
                "modèle entraîné sur des données synthétiques : jamais hors RESEARCH/PAPER, il ne mesure "
                "aucun avantage de marché",
                mode=cfg.mode.value,
            )
    return RegisteredModelPredictor(artifact)
