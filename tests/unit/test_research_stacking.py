"""P4 — stacking OOF TEMPOREL (§19, §56, T20).

Ce que ces tests verrouillent : les prédictions données au méta-modèle viennent toujours de périodes que
ses producteurs n'avaient pas vues, le méta-modèle n'est entraîné que sur des folds antérieurs à celui
qu'il prédit, et aucun fold aléatoire n'existe dans l'API. Données synthétiques seedées : aucun avantage
de marché n'est mesuré ici.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from itertools import pairwise

import polars as pl
import pytest

from okxq.domain.errors import LeakageError, ProtocolViolationError
from okxq.research.datasets import DatasetSpec, QualityLevel, ResearchDataset, build_dataset
from okxq.research.splits import WalkForwardSpec, assert_temporal_split
from okxq.research.stacking import (
    DEFAULT_EXPERTS,
    ExpertSpec,
    MetaSpec,
    OofPanel,
    assert_components_never_saw_the_row,
    build_oof_panel,
    component_dates_per_row,
    features_by_group,
    resolve_expert_features,
    stack_experts,
    train_meta_model,
)
from okxq.research.synthetic import SyntheticSpec, generate_synthetic_events
from okxq.research.training import TrainingSpec, default_label_specs, evaluate_oof

HORIZON_S = 300
INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")


@pytest.fixture(scope="module")
def dataset() -> ResearchDataset:
    syn = SyntheticSpec(minutes=420, instruments=INSTRUMENTS)
    return build_dataset(
        generate_synthetic_events(syn),
        spec=DatasetSpec(label_specs=default_label_specs([HORIZON_S])),
        source="test:synthetic",
        synthetic=True,
        latency_assumed=True,
        quality_level=QualityLevel.C,
    )


@pytest.fixture(scope="module")
def spec(dataset: ResearchDataset) -> TrainingSpec:
    return TrainingSpec(
        walk_forward=WalkForwardSpec(
            train_s=4800, validation_s=1800, test_s=1800, purge_s=HORIZON_S, embargo_s=HORIZON_S
        ),
        horizon_s=HORIZON_S,
        candidates=("ridge",),
        final_test_start=dataset.period_end - timedelta(seconds=3000),
        policy_dependency_s=HORIZON_S,
    )


@pytest.fixture(scope="module")
def panel(dataset: ResearchDataset, spec: TrainingSpec) -> OofPanel:
    return build_oof_panel(dataset, spec)


def test_chaque_composant_porte_sa_date_maximale_pour_chaque_ligne(panel: OofPanel) -> None:
    """§56 : « écrire les dates maximales utilisées par chaque composant pour chaque ligne »."""
    dates = component_dates_per_row(panel)
    assert dates.height == panel.frame.height
    for name in panel.names:
        assert f"{name}__max_train_at" in dates.columns
        assert dates[f"{name}__max_train_at"].null_count() == 0
    assert "component_max_train_at" in dates.columns


def test_T20_un_composant_qui_connait_la_fenetre_predite_est_refuse(panel: OofPanel) -> None:
    """Le test qui ÉCHOUE si une fuite est introduite dans le stacking."""
    assert_components_never_saw_the_row(panel)  # le panneau honnête passe
    victim = panel.names[0]
    tampered = OofPanel(
        frame=panel.frame.with_columns(pl.col("decision_at").alias(f"{victim}__max_train_at")),
        experts=panel.experts,
        fold_ids=panel.fold_ids,
    )
    with pytest.raises(LeakageError):
        assert_components_never_saw_the_row(tampered)


def test_le_meta_modele_nest_entraine_que_sur_des_folds_anterieurs(
    panel: OofPanel, spec: TrainingSpec
) -> None:
    """Fenêtre croissante : les folds d'entraînement du méta-modèle précèdent celui qu'il prédit."""
    result = train_meta_model(panel, spec, meta=MetaSpec())
    assert result.meta_folds, "aucun méta-fold : le test ne prouverait rien"
    order = {fold_id: i for i, fold_id in enumerate(panel.fold_ids)}
    for meta_fold in result.meta_folds:
        assert meta_fold.train_fold_ids
        assert max(order[f] for f in meta_fold.train_fold_ids) < order[meta_fold.test_fold_id]
        train_times = panel.frame.filter(pl.col("fold_id").is_in(meta_fold.train_fold_ids))[
            "decision_at"
        ].to_list()
        test_times = panel.frame.filter(pl.col("fold_id") == meta_fold.test_fold_id)["decision_at"].to_list()
        assert_temporal_split(train_times, test_times)


def test_aucun_fold_aleatoire_nest_utilise(panel: OofPanel, spec: TrainingSpec) -> None:
    """Le manifeste l'affirme ET les données le confirment : les folds sont des intervalles de temps."""
    manifest = train_meta_model(panel, spec, meta=MetaSpec()).manifest()
    assert manifest["oof_temporal"] is True
    assert manifest["random_folds_used"] is False
    # Un fold temporel est un intervalle contigu : ses bornes ne se chevauchent pas avec le suivant.
    bounds = [
        (
            panel.frame.filter(pl.col("fold_id") == fold_id)["decision_at"].min(),
            panel.frame.filter(pl.col("fold_id") == fold_id)["decision_at"].max(),
        )
        for fold_id in panel.fold_ids
    ]
    for (_, end), (start, _) in pairwise(bounds):
        assert end < start


def test_les_predictions_du_meta_modele_sont_evaluables_economiquement(
    panel: OofPanel, spec: TrainingSpec, dataset: ResearchDataset
) -> None:
    """Le méta-modèle sort au format OOF : le même simulateur l'évalue, frais et spread compris."""
    result = train_meta_model(panel, spec, meta=MetaSpec())
    pnl = evaluate_oof(result.meta_oof, cutoff_interval_s=dataset.cutoff_interval_s)
    assert pnl.metrics["total_costs"] > 0
    assert set(result.meta_oof.columns) >= {"prediction", "target", "round_trip_cost", "max_train_at"}
    assert bool((result.meta_oof["max_train_at"] < result.meta_oof["decision_at"]).all())


def test_le_meta_modele_est_verrouille(panel: OofPanel, spec: TrainingSpec) -> None:
    """Pas de seconde couche de recherche d'hyperparamètres : elle ne serait mesurée par rien."""
    meta_spec = train_meta_model(panel, spec, meta=MetaSpec()).meta_spec
    assert meta_spec["locked"] is True
    assert meta_spec["family"] == "ridge"


def test_T70_meme_graine_memes_predictions_de_meta_modele(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    first = stack_experts(dataset, spec)
    second = stack_experts(dataset, spec)
    assert first.meta_oof["prediction"].to_list() == second.meta_oof["prediction"].to_list()
    assert first.meta_metrics == second.meta_metrics
    assert first.expert_metrics == second.expert_metrics


def test_un_expert_sans_feature_exploitable_est_ecarte_avec_son_motif(
    dataset: ResearchDataset, spec: TrainingSpec
) -> None:
    """Un expert muet n'est pas remplacé par des zéros : le méta-modèle ne reçoit pas d'avis inventé."""
    notes: list[str] = []
    panel = build_oof_panel(dataset, spec, experts=DEFAULT_EXPERTS, notes=notes)
    # Sans évaluation JEV dans ce jeu, toutes les features JEV sont masquées : l'expert est écarté.
    assert "jev_event" not in panel.names
    assert any("jev_event" in note for note in notes)
    assert all(f"{name}__prediction" in panel.frame.columns for name in panel.names)


def test_un_groupe_de_features_inconnu_est_une_erreur(dataset: ResearchDataset) -> None:
    with pytest.raises(ProtocolViolationError):
        resolve_expert_features(dataset, ExpertSpec(name="x", feature_groups=("groupe_inexistant",)))
    with pytest.raises(ProtocolViolationError):
        resolve_expert_features(dataset, ExpertSpec(name="x", feature_names=("feature_inexistante",)))


def test_les_experts_sont_specialises_sur_des_groupes_disjoints(dataset: ResearchDataset) -> None:
    """Un expert = un point de vue. Deux experts ne doivent pas être deux fois le même modèle."""
    by_group = features_by_group(dataset)
    assert {"microstructure", "timeseries", "cross_asset", "derivatives"} <= set(by_group)
    seen: set[str] = set()
    for expert in DEFAULT_EXPERTS:
        names = set(resolve_expert_features(dataset, expert))
        assert not (names & seen), f"{expert.name} recouvre un autre expert"
        seen |= names


def test_moins_de_deux_experts_refuse_le_stacking(dataset: ResearchDataset, spec: TrainingSpec) -> None:
    with pytest.raises(ProtocolViolationError):
        build_oof_panel(dataset, spec, experts=(ExpertSpec(name="solo", feature_groups=("microstructure",)),))


def test_trop_peu_de_folds_refuse_un_meta_modele(dataset: ResearchDataset, spec: TrainingSpec) -> None:
    """Deux folds ne permettent pas d'entraîner puis d'évaluer un méta-modèle : on le dit."""
    narrow = replace(spec, walk_forward=WalkForwardSpec(train_s=9000, validation_s=1800, test_s=1800))
    panel = build_oof_panel(dataset, narrow)
    with pytest.raises(ProtocolViolationError):
        train_meta_model(panel, narrow, meta=MetaSpec(min_train_folds=len(panel.fold_ids)))
