"""P4 — entraînement walk-forward : causalité, purge, provenance, essais journalisés, reproductibilité.

Ces tests sont HERMÉTIQUES : aucun réseau, aucune base, aucun fichier du dépôt en écriture hors tmp_path.
Les données viennent du générateur synthétique seedé — elles ne mesurent aucun avantage de marché ; elles
servent à prouver que les garde-fous temporels tiennent.

Tests liés à la matrice : T14 (donnée future), T17 (transformation ajustée sur le test), T19 (labels
chevauchants et disponibilité), T20 (OOF), T21 (label censuré), T70 (même graine, même résultat).
"""

from __future__ import annotations

import json
import warnings
from dataclasses import replace
from datetime import timedelta

import polars as pl
import pytest

from okxq.domain.errors import ArtifactIntegrityError, LeakageError, ProtocolViolationError
from okxq.research.datasets import DatasetSpec, QualityLevel, ResearchDataset, build_dataset
from okxq.research.labels import LabelQuality
from okxq.research.splits import Period, WalkForwardSpec, assign_split
from okxq.research.synthetic import SyntheticSpec, generate_synthetic_events
from okxq.research.training import (
    ModelArtifact,
    ModelStatus,
    TrainingSpec,
    artifact_from_result,
    assert_oof_is_out_of_fold,
    build_matrix,
    build_pipeline,
    default_label_specs,
    evaluate_oof,
    usable_horizons,
    walk_forward_train,
)

HORIZON_S = 300
INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")


def make_dataset(minutes: int = 300) -> ResearchDataset:
    syn = SyntheticSpec(minutes=minutes, instruments=INSTRUMENTS)
    return build_dataset(
        generate_synthetic_events(syn),
        spec=DatasetSpec(label_specs=default_label_specs([HORIZON_S])),
        source="test:synthetic",
        synthetic=True,
        latency_assumed=True,
        quality_level=QualityLevel.C,
    )


@pytest.fixture(scope="module")
def dataset() -> ResearchDataset:
    return make_dataset()


@pytest.fixture
def spec(dataset: ResearchDataset) -> TrainingSpec:
    return TrainingSpec(
        walk_forward=WalkForwardSpec(
            train_s=4800, validation_s=1800, test_s=1800, purge_s=HORIZON_S, embargo_s=HORIZON_S
        ),
        horizon_s=HORIZON_S,
        candidates=("flat", "ridge"),
        final_test_start=dataset.period_end - timedelta(seconds=3000),
        policy_dependency_s=HORIZON_S,
    )


def test_le_jeu_synthetique_produit_des_labels_exploitables(dataset: ResearchDataset) -> None:
    """Prérequis de tous les autres tests : sans label OK, ils passeraient pour de mauvaises raisons."""
    assert dataset.frame.height > 100
    assert (dataset.frame["label_quality"] == LabelQuality.OK.value).sum() > 100
    assert dataset.synthetic is True
    assert dataset.manifest()["notice"] == "données synthétiques — aucune preuve d'alpha"


def test_T14_une_feature_disponible_apres_la_decision_est_refusee(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    """Le test qui ÉCHOUE si une fuite temporelle est introduite : la donnée future est rejetée.

    On décale ``available_at`` de deux minutes APRÈS ``decision_at`` sans rien changer d'autre. C'est
    exactement la forme qu'aurait une fuite (feature calculée avec une donnée arrivée plus tard).
    """
    build_matrix(dataset, spec)  # le jeu honnête passe
    leaked = ResearchDataset(
        **{
            **dataset.__dict__,
            "frame": dataset.frame.with_columns(
                (pl.col("decision_at") + pl.duration(minutes=2)).alias("available_at")
            ),
        }
    )
    with pytest.raises(LeakageError):
        build_matrix(leaked, spec)


def test_T19_aucune_ligne_dentrainement_ne_connait_la_periode_de_test(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    """Purge et embargo : le label d'une ligne d'entraînement est connu avant le début du test."""
    result = walk_forward_train(dataset, spec)
    assert result.folds, "aucun fold exploitable : le test ne prouverait rien"
    for fold in result.folds:
        assert fold.max_train_at < fold.fold.test.start
        assert fold.max_label_available_at is not None
        assert fold.max_label_available_at <= fold.fold.test.start


def test_T19_les_lignes_qui_chevauchent_le_test_sont_purgees(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    """Une ligne d'entraînement dont le label déborde sur la validation est PURGÉE, pas gardée."""
    matrix = build_matrix(dataset, spec)
    result = walk_forward_train(dataset, spec)
    fold = result.folds[0].fold
    split = assign_split(matrix.frame, fold)
    assert (split == "purged").sum() > 0, "sans purge, ce test ne mesure rien"
    frame = matrix.frame.with_columns(split.alias("split"))
    train_rows = frame.filter(pl.col("split") == "train")
    assert train_rows["label_available_at"].max() <= fold.validation.start


def test_T17_une_transformation_ajustee_sur_le_test_est_detectee(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    """La provenance des transformateurs refuse un pipeline qui a vu la période évaluée."""
    matrix = build_matrix(dataset, spec)
    result = walk_forward_train(dataset, spec)
    fold = result.folds[0].fold
    # Le pipeline honnête du fold ne connaît pas le test.
    result.folds[0].pipeline.assert_not_fitted_on(fold.test)
    # Le même pipeline ajusté sur les lignes de test est immédiatement détecté.
    test_mask = (matrix.frame["decision_at"] >= fold.test.start) & (
        matrix.frame["decision_at"] < fold.test.end
    )
    idx = [i for i, keep in enumerate(test_mask.to_list()) if keep]
    contaminated = build_pipeline(spec)
    with warnings.catch_warnings():
        # Les colonnes entièrement absentes font émettre un avertissement numpy attendu (médiane d'un vide).
        warnings.simplefilter("ignore", RuntimeWarning)
        contaminated.fit(matrix.x[idx], [matrix.decision_at[i] for i in idx])
    with pytest.raises(LeakageError):
        contaminated.assert_not_fitted_on(fold.test)


def test_T20_les_predictions_oof_precedent_leur_propre_entrainement(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    """Chaque ligne OOF porte la date maximale connue de son producteur ; elle précède la décision."""
    result = walk_forward_train(dataset, spec)
    assert_oof_is_out_of_fold(result.oof)
    assert bool((result.oof["max_train_at"] < result.oof["decision_at"]).all())
    tampered = result.oof.with_columns(pl.col("decision_at").alias("max_train_at"))
    with pytest.raises(LeakageError):
        assert_oof_is_out_of_fold(tampered)


def test_T21_les_labels_censures_ne_sont_jamais_remplaces_par_zero(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    """Une observation censurée est EXCLUE. Un rendement nul inventé serait un faux exemple."""
    censored = ResearchDataset(
        **{
            **dataset.__dict__,
            "frame": dataset.frame.with_columns(
                pl.when(pl.int_range(pl.len()) < 10)
                .then(pl.lit(LabelQuality.CENSORED.value))
                .otherwise(pl.col("label_quality"))
                .alias("label_quality"),
                pl.when(pl.int_range(pl.len()) < 10)
                .then(None)
                .otherwise(pl.col(spec.target))
                .alias(spec.target),
            ),
        }
    )
    matrix = build_matrix(censored, spec)
    assert matrix.rows == build_matrix(dataset, spec).rows - 10
    # Les lignes censurées sont ABSENTES de la matrice : elles n'y figurent pas avec une cible à 0.
    censored_keys = {
        (row["instrument"], row["decision_at"])
        for row in censored.frame.head(10).select(["instrument", "decision_at"]).to_dicts()
    }
    kept_keys = {
        (row["instrument"], row["decision_at"])
        for row in matrix.frame.select(["instrument", "decision_at"]).to_dicts()
    }
    assert censored_keys and not (censored_keys & kept_keys)


def test_le_modele_publie_na_jamais_vu_la_periode_finale_gelee(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    """Le modèle réajusté pour publication s'arrête avant la période gelée, purge comprise."""
    result = walk_forward_train(dataset, spec)
    assert spec.final_test_start is not None
    assert result.final_period.end <= spec.final_test_start
    guard = Period(spec.final_test_start, dataset.period_end + timedelta(microseconds=1))
    result.final_pipeline.assert_not_fitted_on(guard)
    assert result.model_card()["promotion_status"] == ModelStatus.CANDIDATE.value


def test_tous_les_essais_sont_journalises_et_un_seul_est_retenu(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    """§39.2 : le dénominateur du risque de sélection multiple doit exister."""
    result = walk_forward_train(dataset, spec)
    for fold in result.folds:
        assert len(fold.trials) >= 2, "une grille d'un seul essai ne mesure aucune sélection"
        selected = [t for t in fold.trials if t.selected]
        assert len(selected) == 1
        # Le benchmark « ne rien faire » sert de référence, jamais de modèle retenu.
        assert selected[0].candidate != "flat"
    assert result.model_card()["trials_recorded"] == len(result.trials)


def test_la_carte_de_modele_contient_ce_qui_rend_un_modele_rejouable(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    card = walk_forward_train(dataset, spec).model_card()
    for key in (
        "dataset_hash",
        "feature_schema_hash",
        "feature_names",
        "transformations",
        "labels",
        "folds",
        "seed",
        "library_versions",
        "metrics",
        "known_limits",
        "promotion_status",
    ):
        assert key in card, key
    assert card["synthetic"] is True
    assert any("synthétiques" in limit for limit in card["known_limits"])
    # Une version de bibliothèque inconnue vaut None, jamais une chaîne vide.
    assert all(v is None or isinstance(v, str) for v in card["library_versions"].values())


def test_T70_meme_jeu_meme_graine_memes_resultats(dataset: ResearchDataset, spec: TrainingSpec) -> None:
    """Reproductibilité exigée : prédictions OOF, métriques et hash d'artefact identiques."""
    first = walk_forward_train(dataset, spec)
    second = walk_forward_train(dataset, spec)
    assert first.oof["prediction"].to_list() == second.oof["prediction"].to_list()
    assert first.oof_metrics == second.oof_metrics
    assert first.residual_std == second.residual_std
    assert artifact_from_result(first).sha256() == artifact_from_result(second).sha256()
    assert first.model_card()["model_id"] == second.model_card()["model_id"]


def test_une_graine_differente_change_le_modele_mais_pas_le_jeu(dataset: ResearchDataset) -> None:
    """Contrôle du contrôle : si la graine n'avait AUCUN effet, le test de reproductibilité serait vide."""
    base = TrainingSpec(
        walk_forward=WalkForwardSpec(
            train_s=4800, validation_s=1800, test_s=1800, purge_s=HORIZON_S, embargo_s=HORIZON_S
        ),
        horizon_s=HORIZON_S,
        candidates=("ridge",),
        final_test_start=dataset.period_end - timedelta(seconds=3000),
    )
    other = replace(base, seed=base.seed + 1)
    assert base.to_dict()["seed"] != other.to_dict()["seed"]
    # Le hash de l'artefact intègre la graine : deux recherches distinctes ne partagent pas d'identité.
    assert (
        artifact_from_result(walk_forward_train(dataset, base)).model_id
        != artifact_from_result(walk_forward_train(dataset, other)).model_id
    )


def test_lartefact_est_du_json_verifie_par_son_hash(
    dataset: ResearchDataset, spec: TrainingSpec, tmp_path
) -> None:
    """Aucun pickle ; un artefact modifié est REFUSÉ au chargement (§56)."""
    artifact = artifact_from_result(walk_forward_train(dataset, spec))
    path = tmp_path / "model.json"
    digest = artifact.save(path)
    reloaded = ModelArtifact.load(path, expected_sha256=digest)
    assert reloaded.model_id == artifact.model_id
    assert reloaded.feature_schema_hash == dataset.schema_hash
    body = json.loads(path.read_text(encoding="utf-8"))
    body["residual_std"] = float(body["residual_std"]) + 1.0
    path.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        ModelArtifact.load(path, expected_sha256=digest)
    with pytest.raises(ArtifactIntegrityError):
        ModelArtifact.load(tmp_path / "absent.json")


def test_le_pnl_oof_est_net_de_frais_et_de_spread(dataset: ResearchDataset, spec: TrainingSpec) -> None:
    """Aucun résultat sans coûts : le brut et le net diffèrent dès qu'une position est prise."""
    result = walk_forward_train(dataset, spec)
    pnl = evaluate_oof(result.oof, cutoff_interval_s=dataset.cutoff_interval_s)
    assert pnl.metrics["total_costs"] > 0
    assert pnl.metrics["net_pnl"] == pytest.approx(
        pnl.metrics["gross_pnl"] - pnl.metrics["total_costs"], rel=1e-9, abs=1e-12
    )
    assert pnl.metrics["sharpe_daily_365"] is None, "un jour de données ne donne pas de Sharpe annualisé"
    assert any("sharpe non calculé" in note for note in pnl.notes)


def test_un_horizon_plus_court_que_la_grille_est_refuse() -> None:
    """Un horizon de 60 s sur un chemin de prix à la minute est impossible : on le dit, on ne l'approxime pas."""
    assert usable_horizons([60, 120, 300, 900]) == [300, 900]
    with pytest.raises(ProtocolViolationError):
        default_label_specs([60])


def test_une_cible_de_classification_refuse_les_candidats_de_regression() -> None:
    """Le nom de la cible porte la tâche ; mélanger les deux serait comparer une probabilité à un rendement."""
    walk_forward = WalkForwardSpec(train_s=4800, validation_s=1800, test_s=1800)
    with pytest.raises(ValueError, match="incompatibles"):
        TrainingSpec(walk_forward=walk_forward, target="exceeds_costs", candidates=("ridge",))
    classification = TrainingSpec(walk_forward=walk_forward, target="exceeds_costs", candidates=("logistic",))
    assert classification.task == "classification"


def test_une_cible_absente_est_refusee_explicitement(dataset: ResearchDataset, spec: TrainingSpec) -> None:
    missing_target = replace(spec, target="rendement_inexistant")
    with pytest.raises(ProtocolViolationError):
        build_matrix(dataset, missing_target)
