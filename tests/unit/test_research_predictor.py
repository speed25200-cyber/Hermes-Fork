"""P4 — prédicteur : il produit des ``Forecast`` et rien d'autre, sans contourner un seul garde-fou.

Ce que ces tests verrouillent :
- ``feature.available_at <= snapshot.cutoff_at`` (première moitié du contrat de causalité) ;
- un artefact modifié, un schéma de features incompatible ou une tâche de classification sont REFUSÉS ;
- un instrument trop masqué ne reçoit AUCUNE prévision (jamais ``mu = 0``) ;
- le prédicteur ne construit ni intention d'ordre ni approbation : il n'en importe même pas les contrats.

Base SQLite en mémoire, données synthétiques seedées, aucun réseau.
"""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

import okxq.research.predictor as predictor_module
from okxq.config import AppConfig, Mode
from okxq.domain.errors import ArtifactIntegrityError, LeakageError, ProtocolViolationError
from okxq.domain.events import CostBasis, FeatureVector, Forecast, MarketSnapshot, QualityFlag
from okxq.domain.protocols import Predictor
from okxq.persistence.db import make_session_factory, memory_engine
from okxq.research.datasets import DatasetSpec, QualityLevel, ResearchDataset, build_dataset
from okxq.research.experiment_registry import ExperimentRegistry
from okxq.research.predictor import (
    MODEL_ARTIFACT_ENV,
    MODEL_SHA256_ENV,
    PredictorPolicy,
    RegisteredModelPredictor,
    assert_snapshot_precedes_decision,
    build_predictor,
    forecast_rows,
)
from okxq.research.splits import WalkForwardSpec
from okxq.research.synthetic import SyntheticSpec, generate_synthetic_events
from okxq.research.training import (
    ModelArtifact,
    ModelStatus,
    TrainingSpec,
    artifact_from_result,
    default_label_specs,
    walk_forward_train,
)

HORIZON_S = 300
INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def dataset() -> ResearchDataset:
    syn = SyntheticSpec(minutes=300, instruments=INSTRUMENTS)
    return build_dataset(
        generate_synthetic_events(syn),
        spec=DatasetSpec(label_specs=default_label_specs([HORIZON_S])),
        source="test:synthetic",
        synthetic=True,
        latency_assumed=True,
        quality_level=QualityLevel.C,
    )


@pytest.fixture(scope="module")
def artifact(dataset: ResearchDataset) -> ModelArtifact:
    spec = TrainingSpec(
        walk_forward=WalkForwardSpec(
            train_s=4800, validation_s=1800, test_s=1800, purge_s=HORIZON_S, embargo_s=HORIZON_S
        ),
        horizon_s=HORIZON_S,
        candidates=("ridge",),
        final_test_start=dataset.period_end - timedelta(seconds=3000),
    )
    return artifact_from_result(walk_forward_train(dataset, spec))


def make_snapshot(
    dataset: ResearchDataset,
    artifact: ModelArtifact,
    *,
    cutoff_offset_s: int = 0,
    schema_hash: str | None = None,
    mask_all: bool = False,
) -> MarketSnapshot:
    """Snapshot construit depuis une ligne réelle du jeu de données (mêmes noms, même schéma)."""
    row = dataset.frame.row(0, named=True)
    cutoff = row["decision_at"]
    features: dict[str, FeatureVector] = {}
    for instrument in INSTRUMENTS:
        inst_row = dataset.frame.filter(
            (dataset.frame["instrument"] == instrument) & (dataset.frame["decision_at"] == cutoff)
        ).row(0, named=True)
        values: list[float | None] = []
        masks: list[QualityFlag] = []
        for name in artifact.feature_names:
            value = None if mask_all else inst_row[name]
            values.append(None if value is None else float(value))
            masks.append(QualityFlag.OK if value is not None else QualityFlag.MISSING)
        features[instrument] = FeatureVector(
            instrument=instrument,
            cutoff_at=cutoff,
            available_at=cutoff + timedelta(seconds=cutoff_offset_s),
            names=list(artifact.feature_names),
            values=values,
            masks=masks,
            schema_hash=schema_hash or artifact.feature_schema_hash,
        )
    return MarketSnapshot(
        snapshot_id="snap_test_0001",
        cutoff_at=cutoff,
        universe_version="uni-test",
        metadata_version="meta-test",
        equity_version="eq-test",
        eligible_instruments=list(INSTRUMENTS),
        features=features,
    )


async def test_le_predicteur_rend_des_forecast_et_respecte_le_protocole(
    dataset: ResearchDataset, artifact: ModelArtifact
) -> None:
    predictor = RegisteredModelPredictor(artifact)
    assert isinstance(predictor, Predictor)
    forecasts = await predictor.predict(make_snapshot(dataset, artifact))
    assert len(forecasts) == len(INSTRUMENTS)
    for forecast in forecasts:
        assert isinstance(forecast, Forecast)
        assert forecast.horizon_s == HORIZON_S
        assert forecast.model_id == artifact.model_id
        assert forecast.cost_basis is CostBasis.MID
        assert forecast.uncertainty >= 0.0
        assert forecast.producer == predictor_module.PRODUCER
        assert forecast.fold_id is None
    # Les quantiles viennent des résidus HORS échantillon, autour de mu.
    quantiles = forecasts[0].quantiles
    assert set(quantiles) == {"0.1", "0.5", "0.9"}
    assert quantiles["0.1"] < quantiles["0.9"]
    assert forecast_rows(predictor.forecast_batch(make_snapshot(dataset, artifact)))[0]["instrument"]


def test_une_feature_disponible_apres_la_coupure_est_une_fuite(
    dataset: ResearchDataset, artifact: ModelArtifact
) -> None:
    """Le test qui ÉCHOUE si la causalité point-in-time est relâchée dans le prédicteur."""
    predictor = RegisteredModelPredictor(artifact)
    predictor.forecast_batch(make_snapshot(dataset, artifact))  # available_at == cutoff : accepté
    with pytest.raises(LeakageError):
        predictor.forecast_batch(make_snapshot(dataset, artifact, cutoff_offset_s=60))


def test_le_contrat_de_causalite_couvre_aussi_le_debut_de_la_decision(
    dataset: ResearchDataset, artifact: ModelArtifact
) -> None:
    """``snapshot.cutoff_at <= decision.started_at`` : la seconde moitié du contrat est vérifiable."""
    snapshot = make_snapshot(dataset, artifact)
    assert_snapshot_precedes_decision(snapshot, snapshot.cutoff_at + timedelta(seconds=1))
    with pytest.raises(LeakageError):
        assert_snapshot_precedes_decision(snapshot, snapshot.cutoff_at - timedelta(seconds=1))


def test_un_schema_de_features_incompatible_est_refuse(
    dataset: ResearchDataset, artifact: ModelArtifact
) -> None:
    """Un rollback incomplet (modèle sans son schéma) rendrait les colonnes silencieusement fausses."""
    predictor = RegisteredModelPredictor(artifact)
    with pytest.raises(ArtifactIntegrityError):
        predictor.forecast_batch(make_snapshot(dataset, artifact, schema_hash="0" * 64))


def test_un_artefact_modifie_est_refuse(artifact: ModelArtifact, tmp_path) -> None:
    path = tmp_path / "model.json"
    digest = artifact.save(path)
    RegisteredModelPredictor.from_path(path, expected_sha256=digest)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["card"]["labels"]["horizon_s"] = 900
    path.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        RegisteredModelPredictor.from_path(path, expected_sha256=digest)


def test_un_instrument_trop_masque_na_pas_de_prevision(
    dataset: ResearchDataset, artifact: ModelArtifact
) -> None:
    """L'absence se dit par l'absence : ``mu = 0`` signifierait « je prédis zéro », ce qui est faux."""
    predictor = RegisteredModelPredictor(artifact)
    batch = predictor.forecast_batch(make_snapshot(dataset, artifact, mask_all=True))
    assert batch.forecasts == []
    assert {s.instrument for s in batch.skipped} == set(INSTRUMENTS)
    assert all("manquantes" in s.reason for s in batch.skipped)
    assert all(s.missing_features == len(artifact.feature_names) for s in batch.skipped)


def test_un_instrument_sans_vecteur_de_features_est_ecarte_avec_son_motif(
    dataset: ResearchDataset, artifact: ModelArtifact
) -> None:
    snapshot = make_snapshot(dataset, artifact)
    without = snapshot.model_copy(update={"features": {INSTRUMENTS[0]: snapshot.features[INSTRUMENTS[0]]}})
    batch = RegisteredModelPredictor(artifact).forecast_batch(without)
    assert [f.instrument for f in batch.forecasts] == [INSTRUMENTS[0]]
    assert [s.instrument for s in batch.skipped] == [INSTRUMENTS[1]]
    assert "aucun vecteur" in batch.skipped[0].reason


def test_un_artefact_de_classification_est_refuse(artifact: ModelArtifact) -> None:
    """Une probabilité n'est pas un rendement attendu : la convertir sans modèle serait une invention."""
    classification = ModelArtifact.from_dict(
        {**artifact.to_dict(), "card": {**artifact.card, "task": "classification"}}
    )
    with pytest.raises(ProtocolViolationError, match="probabilité"):
        RegisteredModelPredictor(classification)


def test_un_artefact_sans_horizon_est_refuse(artifact: ModelArtifact) -> None:
    without_horizon = ModelArtifact.from_dict({**artifact.to_dict(), "card": {**artifact.card, "labels": {}}})
    with pytest.raises(ProtocolViolationError, match="horizon"):
        RegisteredModelPredictor(without_horizon)


FORBIDDEN_NAMES = frozenset(
    {"OrderIntent", "ApprovedOrder", "RiskDecision", "ExecutionGateway", "OrderRequest", "SubmissionResult"}
)


def test_le_predicteur_ne_peut_construire_aucun_ordre() -> None:
    """Le modèle propose, le Risk Engine décide, le gateway envoie (§1.11) : la frontière est structurelle.

    On inspecte l'AST (pas le texte : les docstrings citent ces noms pour expliquer l'interdiction) : aucun
    contrat d'ordre n'est importé, aucun identifiant de ce genre n'est utilisé, et rien de tel n'est lié
    dans l'espace de noms du module.
    """
    tree = ast.parse(inspect.getsource(predictor_module))
    imported: set[str] = set()
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            imported |= {alias.name.rsplit(".", 1)[-1] for alias in node.names}
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    assert not (imported & FORBIDDEN_NAMES)
    assert not (used & FORBIDDEN_NAMES)
    assert not (set(vars(predictor_module)) & FORBIDDEN_NAMES)
    public = {
        name
        for name in dir(RegisteredModelPredictor)
        if not name.startswith("_") and callable(getattr(RegisteredModelPredictor, name, None))
    }
    assert public == {"predict", "forecast_batch", "describe", "from_path", "from_registry"}


def test_la_carte_didentite_publie_les_limites_connues(artifact: ModelArtifact) -> None:
    described = RegisteredModelPredictor(artifact).describe()
    assert described["decides_nothing"] is True
    assert described["synthetic_training_data"] is True
    assert described["notice"] == "données synthétiques — aucune preuve d'alpha"
    assert described["known_limits"]
    assert described["artifact_sha256"] == artifact.sha256()


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = memory_engine()
    yield make_session_factory(engine)
    engine.dispose()


def test_le_chargement_depuis_le_registre_verifie_le_hash_enregistre(
    artifact: ModelArtifact, factory: sessionmaker[Session], tmp_path
) -> None:
    """Le chemin ET le hash viennent du registre : un artefact remplacé sur le disque est détecté."""
    registry = ExperimentRegistry(factory)
    path = tmp_path / "model.json"
    digest = artifact.save(path)
    model_id = registry.register_model(artifact.card, now=T0, artifact_path=str(path), artifact_sha256=digest)
    loaded = RegisteredModelPredictor.from_registry(registry, model_id)
    assert loaded.model_id == model_id
    body = json.loads(path.read_text(encoding="utf-8"))
    body["residual_std"] = float(body["residual_std"]) * 2.0
    path.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        RegisteredModelPredictor.from_registry(registry, model_id)


def test_un_modele_enregistre_sans_artefact_est_inutilisable(
    artifact: ModelArtifact, factory: sessionmaker[Session]
) -> None:
    registry = ExperimentRegistry(factory)
    model_id = registry.register_model(artifact.card, now=T0)
    with pytest.raises(ArtifactIntegrityError, match="sans artefact"):
        RegisteredModelPredictor.from_registry(registry, model_id)


def test_une_fraction_de_masquage_invalide_est_refusee() -> None:
    with pytest.raises(ValueError):
        PredictorPolicy(max_missing_fraction=1.5)


# --- point d'entrée de la composition du runtime ----------------------------------------------------------


def _demo_config(paper_config: AppConfig) -> AppConfig:
    return paper_config.model_copy(
        update={"project": paper_config.project.model_copy(update={"mode": Mode.DEMO})}
    )


def test_sans_modele_designe_la_composition_degrade_vers_no_trade(
    paper_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ne rien trader est une décision valide ; charger un modèle non désigné n'en est pas une (§1.13)."""
    monkeypatch.delenv(MODEL_ARTIFACT_ENV, raising=False)
    with pytest.raises(ProtocolViolationError, match=MODEL_ARTIFACT_ENV):
        build_predictor(paper_config)


def test_en_paper_un_modele_candidat_synthetique_est_accepte(
    artifact: ModelArtifact, paper_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """PAPER n'envoie aucun ordre et ne prétend à aucune preuve : un candidat y est exploitable."""
    path = tmp_path / "model.json"
    digest = artifact.save(path)
    monkeypatch.setenv(MODEL_ARTIFACT_ENV, str(path))
    monkeypatch.setenv(MODEL_SHA256_ENV, digest)
    predictor = build_predictor(paper_config)
    assert predictor.model_id == artifact.model_id


def test_hors_paper_un_artefact_sans_hash_attendu_est_refuse(
    artifact: ModelArtifact, paper_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    path = tmp_path / "model.json"
    artifact.save(path)
    monkeypatch.setenv(MODEL_ARTIFACT_ENV, str(path))
    monkeypatch.delenv(MODEL_SHA256_ENV, raising=False)
    with pytest.raises(ArtifactIntegrityError, match=MODEL_SHA256_ENV):
        build_predictor(_demo_config(paper_config))


def test_hors_paper_un_modele_non_promu_est_refuse(
    artifact: ModelArtifact, paper_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """§56 : aucun nom de fichier ne remplace une promotion prouvée."""
    path = tmp_path / "model.json"
    digest = artifact.save(path)
    monkeypatch.setenv(MODEL_ARTIFACT_ENV, str(path))
    monkeypatch.setenv(MODEL_SHA256_ENV, digest)
    with pytest.raises(ProtocolViolationError, match="non promu"):
        build_predictor(_demo_config(paper_config))


def test_hors_paper_un_modele_entraine_sur_du_synthetique_est_refuse(
    artifact: ModelArtifact, paper_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Un modèle appris sur des données synthétiques ne sort jamais de RESEARCH/PAPER."""
    promoted = ModelArtifact.from_dict(
        {
            **artifact.to_dict(),
            "card": {
                **artifact.card,
                "promotion_status": ModelStatus.DEMO_TECH_VALIDATED.value,
                "synthetic": True,
            },
        }
    )
    path = tmp_path / "promu.json"
    digest = promoted.save(path)
    monkeypatch.setenv(MODEL_ARTIFACT_ENV, str(path))
    monkeypatch.setenv(MODEL_SHA256_ENV, digest)
    with pytest.raises(ProtocolViolationError, match="synthétiques"):
        build_predictor(_demo_config(paper_config))
