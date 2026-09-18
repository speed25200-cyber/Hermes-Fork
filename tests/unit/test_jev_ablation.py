"""P4 — ablation JEV A/B/C/D (§40) et rapports lus par l'interface (``kind="jev_ablation"``).

Ce que ces tests verrouillent :
- les variantes sont EMBOÎTÉES (A ⊂ B ⊂ C) et partagent budget, fenêtres et règles de coûts ;
- chaque variante produit un ``EvaluationReport`` dont ``metrics`` contient au moins ``variant`` et
  ``net_pnl``, et que la fonction de lecture de l'API sait interpréter ;
- une variante mesurée sur une période déjà consultée porte ``independent=False`` (T22) ;
- aucun résultat synthétique n'est présenté comme une preuve d'avantage de marché ;
- la conclusion sur l'apport de JEV n'est jamais forcée : elle vaut ``None`` quand rien ne la tranche.

Les évaluations JEV de ces tests sont du BRUIT seedé indépendant du futur : l'ablation doit être capable
de conclure « JEV n'apporte rien », et c'est exactement ce qu'on vérifie.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from okxq.api.routes.research import JEV_ABLATION_KINDS, jev_variant_rows
from okxq.domain.errors import ProtocolViolationError
from okxq.features.events import GROUP_JEV, GROUP_META, JEV_NAMES
from okxq.persistence.db import make_session_factory, memory_engine
from okxq.persistence.models import EvaluationReport
from okxq.research import SYNTHETIC_NOTICE
from okxq.research.datasets import DatasetSpec, QualityLevel, ResearchDataset, build_dataset
from okxq.research.experiment_registry import (
    KIND_JEV_ABLATION,
    ExperimentPlan,
    ExperimentRegistry,
)
from okxq.research.jev_ablation import (
    FALLBACK_SCORE_LEVELS,
    JEV_QUESTION_GROUPS,
    VARIANT_A,
    VARIANT_B,
    VARIANT_C,
    VARIANT_D_PREFIX,
    AblationSpec,
    announcements_from_evaluations,
    assert_jev_groups_cover_registry,
    question_set_score_levels,
    run_ablation,
    synthetic_jev_evaluations,
    variant_feature_sets,
)
from okxq.research.splits import WalkForwardSpec
from okxq.research.stacking import features_by_group
from okxq.research.synthetic import SyntheticSpec, generate_synthetic_events
from okxq.research.training import TrainingSpec, default_label_specs

ROOT = Path(__file__).resolve().parents[2]
HORIZON_S = 300
MINUTES = 300
INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def dataset() -> ResearchDataset:
    syn = SyntheticSpec(minutes=MINUTES, instruments=INSTRUMENTS)
    evaluations = synthetic_jev_evaluations(
        syn.instruments, syn.start, syn.minutes, seed=syn.seed, interval_s=1800
    )
    return build_dataset(
        generate_synthetic_events(syn),
        spec=DatasetSpec(label_specs=default_label_specs([HORIZON_S])),
        source="test:synthetic+jev",
        synthetic=True,
        latency_assumed=True,
        quality_level=QualityLevel.C,
        jev_by_instrument=evaluations,
        announcements_by_instrument=announcements_from_evaluations(evaluations),
        announcement_feed_available=True,
    )


@pytest.fixture(scope="module")
def spec(dataset: ResearchDataset) -> AblationSpec:
    training = TrainingSpec(
        walk_forward=WalkForwardSpec(
            train_s=4800, validation_s=1800, test_s=1800, purge_s=HORIZON_S, embargo_s=HORIZON_S
        ),
        horizon_s=HORIZON_S,
        candidates=("ridge",),
        final_test_start=dataset.period_end - timedelta(seconds=2400),
        policy_dependency_s=HORIZON_S,
    )
    return AblationSpec(training=training)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = memory_engine()
    yield make_session_factory(engine)
    engine.dispose()


def _plan(dataset: ResearchDataset, spec: AblationSpec) -> ExperimentPlan:
    assert spec.training.final_test_start is not None
    return ExperimentPlan.from_mapping(
        {
            "plan_id": "exp-jev-ablation-test",
            "hypothesis": "Les variables sémantiques JEV améliorent le PnL net après coûts.",
            "period": {
                "start": dataset.period_start.isoformat(),
                "end": (dataset.period_end + timedelta(microseconds=1)).isoformat(),
                "frozen_final_test_start": spec.training.final_test_start.isoformat(),
            },
            "labels": {"target": "future_mid_return", "horizons_seconds": [HORIZON_S]},
            "model": {"candidates": ["ridge"]},
            "budget": {"max_trials": 30},
            "seed": 25200,
        }
    )


def test_les_groupes_de_questions_couvrent_exactement_les_features_jev() -> None:
    """Sans couverture exacte, la variante D laisserait un groupe de questions non mesuré."""
    assert_jev_groups_cover_registry()
    covered = [n for names in JEV_QUESTION_GROUPS.values() for n in names]
    assert sorted(covered) == sorted(JEV_NAMES)
    assert len(covered) == len(set(covered)), "un groupe en recouvre un autre"


def test_les_niveaux_de_score_refletent_le_jeu_de_questions_du_depot() -> None:
    """Un changement du jeu de questions impose une revalidation (§40) : le test le rend visible."""
    real = question_set_score_levels(ROOT / "configs" / "jev_questions.v1.json")
    assert real == FALLBACK_SCORE_LEVELS


def test_les_evaluations_jev_synthetiques_sont_causales() -> None:
    """Une évaluation n'est utilisable qu'à partir de ``features_committed_at``, jamais avant."""
    syn = SyntheticSpec(minutes=120, instruments=INSTRUMENTS)
    evaluations = synthetic_jev_evaluations(syn.instruments, syn.start, syn.minutes, seed=syn.seed)
    assert set(evaluations) == set(INSTRUMENTS)
    for items in evaluations.values():
        assert items
        for evaluation in items:
            assert evaluation.features_committed_at is not None
            assert evaluation.inference_completed_at is not None
            assert evaluation.requested_at < evaluation.inference_completed_at
            assert evaluation.inference_completed_at < evaluation.features_committed_at
            assert evaluation.inst_id in INSTRUMENTS


def test_les_features_jev_sont_disponibles_dans_le_jeu_de_test(dataset: ResearchDataset) -> None:
    """Prérequis : sans feature JEV exploitable, l'ablation comparerait C à lui-même."""
    by_group = features_by_group(dataset)
    assert by_group.get(GROUP_JEV)
    assert by_group.get(GROUP_META)
    assert (dataset.frame["jev_available__mask"] == "OK").all()


def test_les_variantes_sont_emboitees_et_isolent_la_semantique(dataset: ResearchDataset) -> None:
    """B existe pour ne pas attribuer à JEV le simple fait qu'une annonce existait (§40)."""
    sets = variant_feature_sets(dataset)
    a, b, c = set(sets[VARIANT_A]), set(sets[VARIANT_B]), set(sets[VARIANT_C])
    assert a < b < c
    by_group = features_by_group(dataset)
    assert not (a & set(by_group[GROUP_META]))
    assert not (a & set(by_group[GROUP_JEV]))
    assert b - a == set(by_group[GROUP_META])
    assert c - b == set(by_group[GROUP_JEV])
    for group, names in JEV_QUESTION_GROUPS.items():
        variant = f"{VARIANT_D_PREFIX}{group}"
        assert variant in sets
        assert not (set(sets[variant]) & set(names))
        assert set(sets[variant]) < c


def test_les_variantes_D_peuvent_etre_desactivees(dataset: ResearchDataset) -> None:
    sets = variant_feature_sets(dataset, include_d_variants=False)
    assert set(sets) == {VARIANT_A, VARIANT_B, VARIANT_C}


def test_lablation_ecrit_des_rapports_que_lapi_sait_lire(
    dataset: ResearchDataset, spec: AblationSpec, factory: sessionmaker[Session]
) -> None:
    """Contrat minimal exigé : ``kind="jev_ablation"``, ``metrics.variant`` et ``metrics.net_pnl``."""
    registry = ExperimentRegistry(factory)
    run_id = registry.open_run(_plan(dataset, spec), now=T0)
    result = run_ablation(
        dataset, replace(spec, include_d_variants=False), now=T0, registry=registry, run_id=run_id
    )
    assert {o.variant for o in result.variants} == {VARIANT_A, VARIANT_B, VARIANT_C}
    for outcome in result.variants:
        assert outcome.report_id is not None
        assert outcome.metrics["variant"] == outcome.variant
        assert isinstance(outcome.metrics["net_pnl"], float)
    with factory() as session:
        rows = list(session.scalars(select(EvaluationReport)).all())
    ablation_rows = [r for r in rows if r.kind == KIND_JEV_ABLATION]
    assert KIND_JEV_ABLATION in JEV_ABLATION_KINDS
    assert len(ablation_rows) == 3
    # La fonction de lecture de l'API (non modifiée) doit interpréter ces lignes telles quelles.
    api_rows = jev_variant_rows(rows)
    assert {row["variant"] for row in api_rows} == {VARIANT_A, VARIANT_B, VARIANT_C}
    assert all(row["net_pnl"] is not None for row in api_rows)
    assert all(row["period_start"] and row["period_end"] for row in api_rows)
    # Chaque ligne reste sérialisable : l'API la renvoie en JSON sans conversion supplémentaire.
    json.dumps(api_rows, ensure_ascii=False)


def test_les_budgets_sont_identiques_entre_variantes(dataset: ResearchDataset, spec: AblationSpec) -> None:
    """« Ne pas régler C dix fois plus que A » : le budget d'essais est le même pour tous les bras."""
    result = run_ablation(dataset, replace(spec, include_d_variants=False), now=T0)
    trials = {o.variant: o.metrics["trials"] for o in result.variants}
    assert len(set(trials.values())) == 1, trials
    costs = {o.metrics["cost_multiplier"] for o in result.variants}
    assert costs == {spec.cost_multiplier}
    periods = {(o.period.start, o.period.end) for o in result.variants}
    assert len(periods) == 1, "les variantes doivent être mesurées sur la MÊME période"


def test_un_resultat_synthetique_nest_jamais_presente_comme_une_preuve(
    dataset: ResearchDataset, spec: AblationSpec
) -> None:
    result = run_ablation(dataset, replace(spec, include_d_variants=False), now=T0)
    assert result.synthetic is True
    assert SYNTHETIC_NOTICE in result.summary()["notice"]
    for outcome in result.variants:
        assert outcome.metrics["synthetic"] is True
        assert "aucune preuve d'avantage de marché" in outcome.metrics["warning"]
        if outcome.metrics["uses_jev_semantics"]:
            assert "bruit seedé" in outcome.metrics["jev_warning"]
    assert "DONNÉES SYNTHÉTIQUES" in result.comparisons["conclusion_basis"]
    assert result.comparisons["synthetic"] is True


def test_sans_periode_independante_la_conclusion_nest_pas_tranchee(
    dataset: ResearchDataset, spec: AblationSpec
) -> None:
    """La conclusion n'est jamais forcée à vrai pour pouvoir annoncer « IA avec JEV » (§40)."""
    result = run_ablation(dataset, replace(spec, include_d_variants=False), now=T0)
    assert all(o.independent is False for o in result.variants)
    assert result.comparisons["jev_adds_net_utility"] is None
    assert "non tranchable" in result.comparisons["conclusion_basis"]
    assert result.comparisons["semantic_gain_C_minus_B"] is not None


def test_sur_le_test_final_la_premiere_passe_est_independante_et_la_suivante_non(
    dataset: ResearchDataset, spec: AblationSpec, factory: sessionmaker[Session]
) -> None:
    """T22 appliqué aux variantes : une période déjà consultée ne produit plus de preuve."""
    registry = ExperimentRegistry(factory)
    plan = _plan(dataset, spec)
    final_spec = replace(spec, include_d_variants=False, evaluate_on="final_test")

    first_run = registry.open_run(plan, now=T0)
    first = run_ablation(dataset, final_spec, now=T0, registry=registry, run_id=first_run)
    assert all(o.independent is True for o in first.variants)
    assert first.comparisons["jev_adds_net_utility"] is not None
    assert registry.final_test_consulted_at(first_run) == T0

    second_run = registry.open_run(plan, now=T0 + timedelta(days=1))
    second = run_ablation(
        dataset, final_spec, now=T0 + timedelta(days=1), registry=registry, run_id=second_run
    )
    assert all(o.independent is False for o in second.variants)
    assert second.comparisons["jev_adds_net_utility"] is None
    assert any("déjà consultée" in note for note in second.notes)
    with factory() as session:
        rows = list(session.scalars(select(EvaluationReport)).all())
    api_rows = jev_variant_rows(rows)
    assert {row["independent"] for row in api_rows} == {True, False}


def test_les_essais_sont_journalises_avant_la_consultation_du_test_final(
    dataset: ResearchDataset, spec: AblationSpec, factory: sessionmaker[Session]
) -> None:
    """L'ordre compte : consulter d'abord fermerait l'expérience et empêcherait d'enregistrer les essais."""
    registry = ExperimentRegistry(factory)
    run_id = registry.open_run(_plan(dataset, spec), now=T0)
    run_ablation(
        dataset,
        replace(spec, include_d_variants=False, evaluate_on="final_test"),
        now=T0,
        registry=registry,
        run_id=run_id,
    )
    run = registry.get_run(run_id)
    assert run["trials"] > 0
    assert run["final_test_consulted_at"] == T0.isoformat()
    with pytest.raises(ProtocolViolationError):
        registry.assert_optimization_allowed(run_id)


def test_un_test_final_non_declare_refuse_lablation_sur_periode_gelee(
    dataset: ResearchDataset, spec: AblationSpec
) -> None:
    without_frozen = replace(
        spec,
        include_d_variants=False,
        evaluate_on="final_test",
        training=replace(spec.training, final_test_start=None),
    )
    with pytest.raises(ProtocolViolationError, match="période finale"):
        run_ablation(dataset, without_frozen, now=T0)


def test_un_registre_sans_run_est_refuse(dataset: ResearchDataset, spec: AblationSpec) -> None:
    """Un rapport sans expérience enregistrée n'existe pas : le plan précède toujours les résultats."""
    with pytest.raises(ProtocolViolationError):
        run_ablation(dataset, spec, now=T0, run_id="exp_orphelin")


def test_T70_lablation_est_reproductible(dataset: ResearchDataset, spec: AblationSpec) -> None:
    light = replace(spec, include_d_variants=False)
    first = run_ablation(dataset, light, now=T0)
    second = run_ablation(dataset, light, now=T0)
    assert [o.metrics["net_pnl"] for o in first.variants] == [o.metrics["net_pnl"] for o in second.variants]
    assert first.comparisons == second.comparisons


def test_la_contribution_marginale_de_chaque_groupe_est_publiee(
    dataset: ResearchDataset, spec: AblationSpec
) -> None:
    """Variante D : chaque groupe de questions retiré donne un écart net mesuré, pas une intuition."""
    result = run_ablation(dataset, spec, now=T0)
    groups = result.comparisons["per_question_group"]
    assert set(groups) == set(JEV_QUESTION_GROUPS)
    for payload in groups.values():
        assert payload["net_pnl"] is not None
        assert payload["marginal_contribution_vs_C"] is not None
