"""P4 — registre d'expériences : plan avant résultats, tous les essais, période finale GELÉE (§39, §56).

Base SQLite EN MÉMOIRE, aucun réseau. Les tests liés à la matrice : T22 (le test final consulté perd son
statut indépendant et aucune optimisation n'y est plus admise).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from okxq.domain.errors import ProtocolViolationError
from okxq.persistence.db import make_session_factory, memory_engine
from okxq.research.experiment_registry import (
    KIND_FINAL_TEST,
    KIND_FINAL_TEST_CONSULTATION,
    KIND_JEV_ABLATION,
    KIND_WALK_FORWARD,
    RUN_STATUS_RUNNING,
    ExperimentPlan,
    ExperimentRegistry,
    plan_final_test_period,
)
from okxq.research.splits import Period
from okxq.research.training import ModelStatus

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
PERIOD_START = datetime(2026, 6, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 9, 1, tzinfo=UTC)
FROZEN_START = datetime(2026, 8, 15, tzinfo=UTC)

PLAN_PAYLOAD = {
    "plan_id": "exp-test",
    "hypothesis": "Les features de microstructure prédisent le rendement net à 300 s.",
    "period": {
        "start": PERIOD_START.isoformat(),
        "end": PERIOD_END.isoformat(),
        "frozen_final_test_start": FROZEN_START.isoformat(),
    },
    "labels": {"target": "future_mid_return", "horizons_seconds": [300, 900]},
    "model": {"candidates": ["ridge"]},
    "budget": {"max_trials": 4},
    "seed": 25200,
}


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = memory_engine()
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def registry(factory: sessionmaker[Session]) -> ExperimentRegistry:
    return ExperimentRegistry(factory)


@pytest.fixture
def plan() -> ExperimentPlan:
    return ExperimentPlan.from_mapping(PLAN_PAYLOAD)


def _final_period(plan: ExperimentPlan) -> Period:
    period = plan.final_test_period()
    assert period is not None
    return period


def _trials(count: int, variant: str | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for i in range(count):
        row: dict[str, object] = {"trial_id": f"tr_{i}", "candidate": "ridge", "selected": i == 0}
        if variant is not None:
            row["variant"] = variant
        rows.append(row)
    return rows


def test_le_plan_dexemple_du_depot_se_charge(registry: ExperimentRegistry) -> None:
    """``configs/experiment.example.yaml`` est le format de référence : il doit rester chargeable."""
    loaded = ExperimentPlan.load(ROOT / "configs" / "experiment.example.yaml")
    assert loaded.plan_id
    assert loaded.hypothesis
    assert loaded.final_test_start is not None
    assert loaded.horizons_s
    assert loaded.max_trials == 30
    assert len(loaded.plan_hash) == 64


def test_un_essai_sans_plan_enregistre_est_refuse(registry: ExperimentRegistry) -> None:
    """Le plan précède les résultats : sans run ouvert, rien ne s'enregistre."""
    with pytest.raises(ProtocolViolationError):
        registry.record_trials("exp_inexistant", _trials(1))


def test_tous_les_essais_sont_conserves(registry: ExperimentRegistry, plan: ExperimentPlan) -> None:
    run_id = registry.open_run(plan, now=T0)
    assert registry.get_run(run_id)["status"] == RUN_STATUS_RUNNING
    assert registry.record_trials(run_id, _trials(3)) == 3
    assert registry.record_trials(run_id, _trials(1)) == 4
    assert registry.get_run(run_id)["trials"] == 4


def test_le_budget_du_plan_borne_la_recherche(registry: ExperimentRegistry, plan: ExperimentPlan) -> None:
    run_id = registry.open_run(plan, now=T0)
    registry.record_trials(run_id, _trials(4))
    with pytest.raises(ProtocolViolationError, match="budget"):
        registry.record_trials(run_id, _trials(1))


def test_le_budget_sapplique_par_bras_dexperience(registry: ExperimentRegistry, plan: ExperimentPlan) -> None:
    """§40 : des budgets COMPARABLES par variante, pas un budget global consommé par la première."""
    run_id = registry.open_run(plan, now=T0)
    registry.record_trials(run_id, _trials(4, variant="A"), budget_scope="variant")
    registry.record_trials(run_id, _trials(4, variant="C"), budget_scope="variant")
    assert registry.get_run(run_id)["trials"] == 8
    with pytest.raises(ProtocolViolationError, match="budget"):
        registry.record_trials(run_id, _trials(1, variant="C"), budget_scope="variant")


def test_T22_la_premiere_consultation_du_test_final_le_consomme(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    """Le cœur de T22 : une consultation, un instant enregistré, et la période cesse d'être une preuve."""
    run_id = registry.open_run(plan, now=T0)
    period = _final_period(plan)
    assert registry.final_test_consulted_at(run_id) is None
    assert registry.period_is_independent(run_id, period) is True

    first = registry.consult_final_test(run_id, period, now=T0, purpose="mesure unique")
    assert first.first_consultation is True
    assert first.independent is True
    assert registry.final_test_consulted_at(run_id) == T0
    assert registry.period_is_independent(run_id, period) is False

    later = T0 + timedelta(hours=3)
    second = registry.consult_final_test(run_id, period, now=later, purpose="deuxième regard")
    assert second.first_consultation is False
    assert second.independent is False
    # L'instant de PREMIÈRE consultation ne bouge pas : il date la perte d'indépendance.
    assert registry.final_test_consulted_at(run_id) == T0
    consultations = [r for r in registry.reports(run_id) if r["kind"] == KIND_FINAL_TEST_CONSULTATION]
    assert len(consultations) == 2
    assert [c["independent"] for c in consultations].count(True) == 1


def test_T22_aucune_optimisation_apres_consultation_du_test_final(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    """« Une expérience dont le test final a été consulté n'est plus une preuve » : plus aucun essai."""
    run_id = registry.open_run(plan, now=T0)
    registry.record_trials(run_id, _trials(1))
    registry.assert_optimization_allowed(run_id)
    registry.consult_final_test(run_id, _final_period(plan), now=T0, purpose="mesure unique")
    with pytest.raises(ProtocolViolationError, match="consulté"):
        registry.assert_optimization_allowed(run_id)
    with pytest.raises(ProtocolViolationError, match="consulté"):
        registry.record_trials(run_id, _trials(1))


def test_T22_une_autre_execution_du_meme_plan_ne_rend_pas_la_periode_independante(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    """La période gelée appartient au PLAN : relancer l'expérience ne la remet pas à neuf."""
    first_run = registry.open_run(plan, now=T0)
    registry.consult_final_test(first_run, _final_period(plan), now=T0, purpose="mesure unique")
    second_run = registry.open_run(plan, now=T0 + timedelta(days=1))
    assert registry.period_is_independent(second_run, _final_period(plan)) is False
    consultation = registry.consult_final_test(
        second_run, _final_period(plan), now=T0 + timedelta(days=1), purpose="nouvelle tentative"
    )
    assert consultation.independent is False


def test_un_rapport_nest_pas_une_preuve_par_defaut(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    run_id = registry.open_run(plan, now=T0)
    report_id = registry.write_report(
        run_id,
        kind=KIND_WALK_FORWARD,
        period=Period(PERIOD_START, FROZEN_START),
        metrics={"net_pnl": -0.001},
        now=T0,
        synthetic=True,
    )
    report = next(r for r in registry.reports(run_id) if r["report_id"] == report_id)
    assert report["independent"] is False
    assert report["validated"] is False
    assert report["metrics"]["synthetic"] is True
    assert "synthétiques" in report["metrics"]["warning"]


def test_un_rapport_sur_le_test_final_journalise_la_consultation_avant_decrire(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    run_id = registry.open_run(plan, now=T0)
    period = _final_period(plan)
    report_id, consultation = registry.report_on_final_test(
        run_id,
        kind=KIND_FINAL_TEST,
        period=period,
        metrics={"net_pnl": -0.002},
        now=T0,
        purpose="mesure unique",
    )
    report = next(r for r in registry.reports(run_id) if r["report_id"] == report_id)
    assert consultation.first_consultation is True
    assert report["independent"] is True
    assert report["metrics"]["final_test_consulted_at"] == T0.isoformat()

    second_id, second = registry.report_on_final_test(
        run_id,
        kind=KIND_FINAL_TEST,
        period=period,
        metrics={"net_pnl": -0.002},
        now=T0 + timedelta(hours=1),
        purpose="deuxième regard",
    )
    second_report = next(r for r in registry.reports(run_id) if r["report_id"] == second_id)
    assert second.independent is False
    assert second_report["independent"] is False
    assert "information de développement" in second_report["metrics"]["warning"]


def test_un_rapport_lie_a_un_modele_inconnu_est_refuse(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    run_id = registry.open_run(plan, now=T0)
    with pytest.raises(ProtocolViolationError):
        registry.write_report(
            run_id,
            kind=KIND_JEV_ABLATION,
            period=Period(PERIOD_START, FROZEN_START),
            metrics={"variant": "C_jev_semantic", "net_pnl": -0.001},
            now=T0,
            model_id="mdl_inexistant",
        )


def _card(*, synthetic: bool = True) -> dict[str, object]:
    return {
        "model_id": "mdl_test_0001",
        "family": "ridge",
        "task": "regression",
        "dataset_hash": "a" * 64,
        "feature_schema_hash": None,
        "period_start": PERIOD_START.isoformat(),
        "period_end": FROZEN_START.isoformat(),
        "synthetic": synthetic,
        "known_limits": ["aucun impact de marché modélisé"],
    }


def test_un_modele_senregistre_en_CANDIDATE_et_de_facon_idempotente(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    model_id = registry.register_model(_card(), now=T0)
    assert registry.register_model(_card(), now=T0) == model_id
    row = registry.model(model_id)
    assert row["status"] == ModelStatus.CANDIDATE.value
    assert row["synthetic"] is True
    assert row["known_limits"]


def test_une_carte_sans_hash_de_jeu_de_donnees_est_refusee(registry: ExperimentRegistry) -> None:
    card = _card()
    card.pop("dataset_hash")
    with pytest.raises(ProtocolViolationError):
        registry.register_model(card, now=T0)


def test_la_promotion_est_refusee_sans_preuve_independante(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    run_id = registry.open_run(plan, now=T0)
    model_id = registry.register_model(_card(), now=T0)
    registry.write_report(
        run_id,
        kind=KIND_WALK_FORWARD,
        period=Period(PERIOD_START, FROZEN_START),
        metrics={"net_pnl": 0.01},
        now=T0,
        model_id=model_id,
        independent=False,
    )
    with pytest.raises(ProtocolViolationError, match="indépendante"):
        registry.promote(model_id, status=ModelStatus.VALIDATED_OFFLINE, actor="opérateur", now=T0)


def test_la_promotion_est_refusee_si_la_seule_preuve_est_synthetique(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    """Exigence non négociable : un résultat synthétique n'est jamais une preuve d'avantage de marché."""
    run_id = registry.open_run(plan, now=T0)
    model_id = registry.register_model(_card(), now=T0)
    registry.report_on_final_test(
        run_id,
        kind=KIND_FINAL_TEST,
        period=_final_period(plan),
        metrics={"net_pnl": 0.01},
        now=T0,
        purpose="mesure unique",
        model_id=model_id,
        synthetic=True,
    )
    with pytest.raises(ProtocolViolationError, match="synthétiques"):
        registry.promote(model_id, status=ModelStatus.VALIDATED_OFFLINE, actor="opérateur", now=T0)


def test_la_promotion_accepte_une_preuve_independante_non_synthetique(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    run_id = registry.open_run(plan, now=T0)
    model_id = registry.register_model(_card(synthetic=False), now=T0)
    registry.report_on_final_test(
        run_id,
        kind=KIND_FINAL_TEST,
        period=_final_period(plan),
        metrics={"net_pnl": 0.01},
        now=T0,
        purpose="mesure unique",
        model_id=model_id,
        synthetic=False,
    )
    promoted = registry.promote(model_id, status=ModelStatus.VALIDATED_OFFLINE, actor="opérateur", now=T0)
    assert promoted["status"] == ModelStatus.VALIDATED_OFFLINE.value
    assert promoted["promoted_by"] == "opérateur"
    assert promoted["promoted_at"] == T0.isoformat()


def test_LIVE_APPROVED_ne_sobtient_jamais_dans_le_registre_de_recherche(
    registry: ExperimentRegistry,
) -> None:
    model_id = registry.register_model(_card(synthetic=False), now=T0)
    with pytest.raises(ProtocolViolationError, match="§71"):
        registry.promote(model_id, status=ModelStatus.LIVE_APPROVED, actor="opérateur", now=T0)


def test_une_promotion_exige_un_operateur_nomme(registry: ExperimentRegistry) -> None:
    """``promotion_auto_approve`` est faux par configuration : personne ne s'auto-approuve."""
    model_id = registry.register_model(_card(synthetic=False), now=T0)
    with pytest.raises(ProtocolViolationError, match="opérateur nommé"):
        registry.promote(model_id, status=ModelStatus.SHADOW, actor="   ", now=T0)


def test_un_plan_reechelonne_conserve_le_hash_dorigine(plan: ExperimentPlan) -> None:
    """La période exécutée peut différer de celle du fichier ; la traçabilité ne se perd pas."""
    start = datetime(2026, 6, 1, 1, tzinfo=UTC)
    end = datetime(2026, 6, 1, 7, tzinfo=UTC)
    frozen = datetime(2026, 6, 1, 6, tzinfo=UTC)
    rescaled = plan.rescaled(start=start, end=end, final_test_start=frozen, reason="jeu court")
    assert rescaled.plan_id == plan.plan_id
    assert rescaled.plan_hash != plan.plan_hash
    assert rescaled.payload["period_rescaled"]["source_plan_hash"] == plan.plan_hash
    assert rescaled.final_test_start == frozen
    assert plan_final_test_period(rescaled.to_dict()) == Period(frozen, end)


def test_un_plan_incomplet_ou_incoherent_est_refuse() -> None:
    with pytest.raises(ProtocolViolationError, match="incomplet"):
        ExperimentPlan.from_mapping({"plan_id": "x", "hypothesis": "y"})
    bad_frozen = {
        **PLAN_PAYLOAD,
        "period": {
            "start": PERIOD_START.isoformat(),
            "end": PERIOD_END.isoformat(),
            "frozen_final_test_start": "2027-01-01T00:00:00+00:00",
        },
    }
    with pytest.raises(ProtocolViolationError, match="période finale"):
        ExperimentPlan.from_mapping(bad_frozen)


def test_un_plan_sans_periode_gelee_ne_produit_aucune_preuve(registry: ExperimentRegistry) -> None:
    """Sans période réservée, aucune mesure n'est indépendante — et le registre ne le prétend pas."""
    payload = {
        **PLAN_PAYLOAD,
        "period": {"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
    }
    plan = ExperimentPlan.from_mapping(payload)
    assert plan.final_test_period() is None
    run_id = registry.open_run(plan, now=T0)
    assert registry.period_is_independent(run_id, Period(FROZEN_START, PERIOD_END)) is False


def test_le_garde_de_periode_finale_reflete_le_journal(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    """L'état en mémoire ne peut pas contredire la base : le garde est reconstruit depuis le journal."""
    run_id = registry.open_run(plan, now=T0)
    period = _final_period(plan)
    assert registry.final_test_guard(run_id, period).independent is True
    registry.consult_final_test(run_id, period, now=T0, purpose="mesure unique")
    restored = registry.final_test_guard(run_id, period)
    assert restored.independent is False
    assert restored.consulted_at == T0
    with pytest.raises(ProtocolViolationError):
        restored.assert_untouched()


def test_les_executions_se_listent_avec_letat_du_test_final(
    registry: ExperimentRegistry, plan: ExperimentPlan
) -> None:
    clean = registry.open_run(plan, now=T0)
    burned = registry.open_run(plan, now=T0 + timedelta(minutes=1))
    registry.consult_final_test(burned, _final_period(plan), now=T0, purpose="mesure unique")
    rows = {row["run_id"]: row for row in registry.list_runs()}
    assert rows[clean]["final_test_independent"] is True
    assert rows[burned]["final_test_independent"] is False
    assert rows[burned]["final_test_consulted_at"] == T0.isoformat()
