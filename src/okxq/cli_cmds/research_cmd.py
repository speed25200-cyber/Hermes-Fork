"""``okxq research`` : jobs de recherche hors ligne (§67).

Commandes : ``prepare``, ``train``, ``stack``, ``evaluate``, ``ablation`` (alias ``compare-jev``) et
``registry list|models``. Aucune n'ouvre de socket : le jeu de données vient d'un dossier d'événements
archivé ou du générateur synthétique seedé.

Conventions tenues par ces commandes :
- ``RUN_ID`` est RENDU par ``train``/``stack``/``ablation``, jamais inventé par l'opérateur (§67) ;
- le plan d'expérience est enregistré AVANT le premier essai, et tous les essais sont journalisés ;
- une sortie obtenue sur données synthétiques porte ``synthetic: true`` et l'avertissement explicite :
  elle ne mesure aucun avantage de marché ;
- consulter la période finale gelée (``evaluate --final-test``, ``ablation --on final_test``) est une
  action JOURNALISÉE qui retire à cette période son statut de test indépendant (T22).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from okxq.cli_cmds._common import emit, load_or_exit, session_factory_from_env
from okxq.config.schema import AppConfig
from okxq.domain.errors import OkxqError
from okxq.features.registry import FeatureRegistry, default_registry
from okxq.research import SYNTHETIC_NOTICE
from okxq.research.datasets import (
    DEFAULT_WARMUP_S,
    DatasetSpec,
    QualityLevel,
    ResearchDataset,
    build_dataset,
    dataset_from_folder,
)
from okxq.research.experiment_registry import (
    KIND_FINAL_TEST,
    KIND_STACKING,
    KIND_WALK_FORWARD,
    ExperimentPlan,
    ExperimentRegistry,
)
from okxq.research.jev_ablation import (
    AblationSpec,
    announcements_from_evaluations,
    run_ablation,
    synthetic_jev_evaluations,
)
from okxq.research.predictor import RegisteredModelPredictor
from okxq.research.splits import Period, WalkForwardSpec
from okxq.research.stacking import MetaSpec, stack_experts
from okxq.research.synthetic import SyntheticSpec, generate_synthetic_events
from okxq.research.training import (
    CLASSIFICATION_CANDIDATES,
    CLASSIFICATION_TARGETS,
    REGRESSION_CANDIDATES,
    ModelStatus,
    TrainingSpec,
    artifact_from_result,
    code_commit,
    default_label_specs,
    evaluate_oof,
    predict_holdout,
    usable_horizons,
    walk_forward_train,
)

app = typer.Typer(help="Recherche hors ligne : jeux de données, entraînement, stacking, ablation JEV.")
registry_app = typer.Typer(help="Registre d'expériences et de modèles (lecture).")
app.add_typer(registry_app, name="registry")

DEFAULT_SYNTHETIC_MINUTES = 420
DEFAULT_ARTIFACTS = Path("artifacts")
# Échauffement des features + au moins 30 minutes de coupures : en dessous, un dossier d'événements ne
# peut produire aucune ligne de recherche (les jeux golden de replay durent quelques minutes).
MIN_DATASET_SPAN_S = DEFAULT_WARMUP_S + 1800
# Répartition utilisée quand les fenêtres du plan (en jours) ne tiennent pas dans un jeu court. Elle
# laisse volontairement de la place pour PLUSIEURS folds : un seul fold ne permet ni stacking temporel
# ni mesure de la stabilité d'une sélection.
AUTO_TRAIN_FRACTION = 0.35
AUTO_VALIDATION_FRACTION = 0.12
AUTO_TEST_FRACTION = 0.12


def _plan_or_exit(path: Path) -> ExperimentPlan:
    try:
        return ExperimentPlan.load(path)
    except (OkxqError, ValueError) as exc:
        typer.echo(f"plan d'expérience refusé : {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _horizon(plan: ExperimentPlan, override: int | None, notes: list[str] | None = None) -> int:
    """Plus court horizon du plan RÉELLEMENT labellisable ; les horizons trop courts sont dits, pas ignorés."""
    if override is not None:
        return override
    horizons = plan.horizons_s
    if not horizons:
        typer.echo("le plan ne déclare aucun horizon (labels.horizons_seconds)", err=True)
        raise typer.Exit(code=1)
    usable = usable_horizons(horizons)
    if not usable:
        typer.echo(
            f"aucun horizon du plan {sorted(horizons)} n'est labellisable avec un chemin de prix à la "
            "minute (au moins trois pas requis entre décision et fin d'horizon)",
            err=True,
        )
        raise typer.Exit(code=1)
    dropped = sorted(set(int(h) for h in horizons) - set(usable))
    if dropped and notes is not None:
        notes.append(
            f"horizons {dropped} écartés : un chemin de prix à la minute ne peut pas les labelliser "
            "(entrée et sortie tomberaient sur la même barre)"
        )
    return usable[0]


def _registry_for_plan(plan: ExperimentPlan, cfg: AppConfig) -> FeatureRegistry:
    groups = plan.feature_groups
    # L'ablation JEV a besoin des groupes d'événements ; un plan qui ne les cite pas garde son périmètre.
    return default_registry(tuple(groups) if groups else None)


def _folder_span_s(folder: Path) -> float | None:
    """Étendue temporelle annoncée par le manifeste d'un dossier d'événements. Inconnue ⇒ ``None``."""
    manifest_path = folder / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        first = datetime.fromisoformat(str(manifest["first_available_at"]))
        last = datetime.fromisoformat(str(manifest["last_available_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    return (last - first).total_seconds()


def _build_dataset(
    plan: ExperimentPlan,
    cfg: AppConfig,
    *,
    dataset_folder: Path | None,
    minutes: int,
    with_jev: bool,
    feature_registry: FeatureRegistry | None = None,
) -> tuple[ResearchDataset, list[str]]:
    """Dossier d'événements archivé s'il est assez long, sinon générateur synthétique seedé — dit explicitement.

    POURQUOI cette distinction : un dossier demandé EXPLICITEMENT (``--dataset``) qui ne couvre pas
    l'échauffement des features est une erreur, pas une invitation à changer de source dans le dos de
    l'opérateur. Le dossier cité par défaut dans le plan (souvent un jeu golden de replay de quelques
    minutes) donne, lui, un repli synthétique ANNONCÉ dans les notes.
    """
    notes: list[str] = []
    horizons = usable_horizons(plan.horizons_s) or [300]
    spec = DatasetSpec(label_specs=default_label_specs(horizons))
    explicit = dataset_folder is not None
    folder = dataset_folder if dataset_folder is not None else plan.dataset_folder
    if folder is not None and (folder / "events.jsonl").exists():
        span = _folder_span_s(folder)
        if span is not None and span < MIN_DATASET_SPAN_S:
            message = (
                f"dossier {folder} : {span:.0f} s de données, alors que l'échauffement des features et "
                f"une fenêtre minimale de recherche en exigent {MIN_DATASET_SPAN_S}"
            )
            if explicit:
                typer.echo(f"jeu de données trop court : {message}", err=True)
                raise typer.Exit(code=1)
            notes.append(f"{message} → repli sur le générateur synthétique seedé")
        else:
            notes.append(f"jeu de données archivé : {folder}")
            if with_jev:
                notes.append(
                    "aucune évaluation JEV n'accompagne un dossier d'événements : les features JEV "
                    "resteront masquées et la variante C sera identique à B"
                )
            return dataset_from_folder(folder, spec=spec, registry=feature_registry), notes
    syn = SyntheticSpec(seed=cfg.research.random_seed, minutes=minutes)
    notes.append(
        f"aucun historique réel disponible : jeu SYNTHÉTIQUE seedé (seed={syn.seed}, minutes={minutes}). "
        f"{SYNTHETIC_NOTICE}"
    )
    events = generate_synthetic_events(syn)
    jev = None
    announcements = None
    if with_jev:
        jev = synthetic_jev_evaluations(syn.instruments, syn.start, syn.minutes, seed=syn.seed)
        announcements = announcements_from_evaluations(jev)
        notes.append(
            "variables sémantiques JEV synthétiques : bruit seedé indépendant du futur (contrôle négatif)"
        )
    dataset = build_dataset(
        events,
        spec=spec,
        registry=feature_registry,
        source=f"synthetic:seed={syn.seed}:minutes={minutes}",
        synthetic=True,
        latency_assumed=True,
        quality_level=QualityLevel.C,
        jev_by_instrument=jev,
        announcements_by_instrument=announcements,
        announcement_feed_available=with_jev,
    )
    return dataset, notes


def _walk_forward(
    plan: ExperimentPlan,
    dataset: ResearchDataset,
    *,
    train_s: int | None,
    validation_s: int | None,
    test_s: int | None,
    horizon_s: int,
    notes: list[str],
    limit: datetime | None = None,
) -> WalkForwardSpec:
    """Fenêtres du plan ; auto-échelle UNIQUEMENT sur données synthétiques, et le dit dans les notes.

    ``limit`` est le début de la période finale gelée : les folds doivent tenir AVANT elle, sinon un fold
    empiéterait sur le test réservé.
    """
    splits = plan.payload.get("splits") or {}
    purge_s = int(splits.get("purge_seconds", horizon_s))
    embargo_s = int(splits.get("embargo_seconds", horizon_s))
    plan_train = int(splits.get("train_days", 0)) * 86400
    plan_validation = int(splits.get("validation_days", 0)) * 86400
    plan_test = int(splits.get("test_days", 0)) * 86400
    end = limit if limit is not None else dataset.period_end
    span = (end - dataset.period_start).total_seconds()
    chosen_train = train_s if train_s is not None else plan_train
    chosen_validation = validation_s if validation_s is not None else plan_validation
    chosen_test = test_s if test_s is not None else plan_test
    total = chosen_train + chosen_validation + chosen_test
    if total <= 0 or total > span:
        if not dataset.synthetic:
            typer.echo(
                f"fenêtres du plan ({total:.0f} s) plus longues que les données disponibles ({span:.0f} s) : "
                "fournir --train-s/--validation-s/--test-s ou un jeu de données plus long",
                err=True,
            )
            raise typer.Exit(code=1)
        chosen_train = max(horizon_s * 4, int(span * AUTO_TRAIN_FRACTION))
        chosen_validation = max(horizon_s * 2, int(span * AUTO_VALIDATION_FRACTION))
        chosen_test = max(horizon_s * 2, int(span * AUTO_TEST_FRACTION))
        notes.append(
            f"fenêtres auto-échelonnées sur un jeu synthétique court : train={chosen_train} s, "
            f"validation={chosen_validation} s, test={chosen_test} s (les fenêtres du plan supposent un "
            "historique réel de plusieurs semaines)"
        )
    return WalkForwardSpec(
        train_s=chosen_train,
        validation_s=chosen_validation,
        test_s=chosen_test,
        purge_s=purge_s,
        embargo_s=embargo_s,
    )


def _final_test_start(plan: ExperimentPlan, dataset: ResearchDataset, notes: list[str]) -> datetime | None:
    """Période finale gelée ramenée à l'échelle du jeu de données, sans jamais la supprimer en silence."""
    if plan.final_test_start is None:
        notes.append("le plan ne gèle aucune période finale : aucun test indépendant ne sera possible")
        return None
    if dataset.period_start < plan.final_test_start < dataset.period_end:
        return plan.final_test_start
    span = (dataset.period_end - dataset.period_start).total_seconds()
    fraction = (plan.final_test_start - plan.period.start).total_seconds() / max(1.0, plan.period.seconds)
    scaled = dataset.period_start + timedelta(seconds=span * min(max(fraction, 0.5), 0.9))
    notes.append(
        f"période finale gelée du plan hors du jeu de données : repositionnée à la même fraction "
        f"({fraction:.2f}) de la période disponible, soit {scaled.isoformat()}"
    )
    return scaled


def _effective_plan(
    plan: ExperimentPlan,
    dataset: ResearchDataset,
    final_start: datetime | None,
    notes: list[str],
) -> ExperimentPlan:
    """Plan tel qu'il est RÉELLEMENT exécuté : période et test final gelé à l'échelle du jeu de données."""
    if plan.period.start == dataset.period_start and plan.final_test_start == final_start:
        return plan
    effective = plan.rescaled(
        start=dataset.period_start,
        end=dataset.period_end + timedelta(microseconds=1),
        final_test_start=final_start,
        reason=(
            "période du plan hors du jeu de données disponible : le plan enregistré décrit ce qui a été "
            "exécuté, et conserve le hash du plan d'origine"
        ),
    )
    notes.append(
        f"plan enregistré à l'échelle des données (plan d'origine : {plan.plan_hash[:12]}…, "
        f"plan exécuté : {effective.plan_hash[:12]}…)"
    )
    return effective


def _training_spec(
    plan: ExperimentPlan,
    cfg: AppConfig,
    dataset: ResearchDataset,
    *,
    horizon_s: int,
    walk_forward: WalkForwardSpec,
    final_test_start: datetime | None,
    candidates: tuple[str, ...] | None,
    notes: list[str] | None = None,
) -> TrainingSpec:
    """Spécification d'entraînement dérivée du plan, avec le budget d'essais le plus CONTRAIGNANT."""
    log = notes if notes is not None else []
    target = plan.target or "future_mid_return"
    task = "classification" if target in CLASSIFICATION_TARGETS else "regression"
    allowed = CLASSIFICATION_CANDIDATES if task == "classification" else REGRESSION_CANDIDATES
    requested = list(candidates) if candidates else plan.candidates
    chosen = tuple(c for c in requested if c in allowed)
    dropped = [c for c in requested if c not in allowed]
    if dropped:
        # On ne réinterprète pas le plan en silence : le candidat écarté et son motif sont publiés.
        log.append(
            f"candidats écartés {dropped} : incompatibles avec la cible {target} ({task}). Le plan mélange "
            f"les tâches ; candidats retenus : {list(chosen) or ['flat', 'ridge']}"
        )
    if not chosen:
        chosen = CLASSIFICATION_CANDIDATES[:1] if task == "classification" else ("flat", "ridge")
    budget = min(
        cfg.research.max_trials_per_experiment, plan.max_trials or cfg.research.max_trials_per_experiment
    )
    return TrainingSpec(
        walk_forward=walk_forward,
        target=target,
        horizon_s=horizon_s,
        candidates=chosen,
        seed=cfg.research.random_seed,
        max_trials=budget,
        final_test_start=final_test_start,
        policy_dependency_s=horizon_s,
    )


def _open_registry() -> ExperimentRegistry:
    return ExperimentRegistry(session_factory_from_env())


def _persist_feature_schema(dataset: ResearchDataset, now: datetime) -> str | None:
    """Enregistre le schéma de features (clé étrangère de ``model_versions``) avant tout modèle."""
    try:
        reg = FeatureRegistry.from_dict(dataset.registry_payload)
    except (KeyError, ValueError):
        return None
    factory = session_factory_from_env()
    with factory() as session:
        schema_hash = reg.persist(session, now)
        session.commit()
    return schema_hash


@app.command("prepare")
def prepare(
    experiment: Path = typer.Option(..., "--experiment", exists=True, dir_okay=False),
    config: Path = typer.Option(Path("configs/paper.yaml"), "--config", exists=True, dir_okay=False),
    dataset: Path | None = typer.Option(None, "--dataset", help="Dossier d'événements archivé."),
    output: Path | None = typer.Option(None, "--output", help="Dossier où écrire dataset.parquet."),
    minutes: int = typer.Option(DEFAULT_SYNTHETIC_MINUTES, "--minutes", min=60),
    with_jev: bool = typer.Option(False, "--with-jev", help="Ajoute des entrées JEV synthétiques."),
) -> None:
    """Assemble le jeu de données point-in-time (features + labels) et publie son manifeste."""
    plan = _plan_or_exit(experiment)
    cfg = load_or_exit(config)
    try:
        research_dataset, notes = _build_dataset(
            plan,
            cfg,
            dataset_folder=dataset,
            minutes=minutes,
            with_jev=with_jev,
            feature_registry=_registry_for_plan(plan, cfg),
        )
    except (OkxqError, ValueError) as exc:
        typer.echo(f"préparation impossible : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "manifest": research_dataset.manifest(),
        "notes": notes,
    }
    if output is not None:
        research_dataset.save(output)
        payload["saved_to"] = str(output)
    emit(payload)


@app.command("train")
def train(
    experiment: Path = typer.Option(..., "--experiment", exists=True, dir_okay=False),
    config: Path = typer.Option(Path("configs/paper.yaml"), "--config", exists=True, dir_okay=False),
    dataset: Path | None = typer.Option(None, "--dataset"),
    minutes: int = typer.Option(DEFAULT_SYNTHETIC_MINUTES, "--minutes", min=60),
    horizon_s: int | None = typer.Option(None, "--horizon-s", help="Horizon en secondes (défaut : plan)."),
    candidate: list[str] = typer.Option([], "--candidate", help="Candidat à essayer (répétable)."),
    train_s: int | None = typer.Option(None, "--train-s"),
    validation_s: int | None = typer.Option(None, "--validation-s"),
    test_s: int | None = typer.Option(None, "--test-s"),
    artifacts: Path = typer.Option(DEFAULT_ARTIFACTS, "--artifacts", help="Dossier des artefacts JSON."),
) -> None:
    """Entraîne en walk-forward, journalise tous les essais et enregistre le modèle (statut CANDIDATE)."""
    plan = _plan_or_exit(experiment)
    cfg = load_or_exit(config)
    now = datetime.now(tz=UTC)
    notes: list[str] = []
    try:
        research_dataset, build_notes = _build_dataset(
            plan,
            cfg,
            dataset_folder=dataset,
            minutes=minutes,
            with_jev=False,
            feature_registry=_registry_for_plan(plan, cfg),
        )
        notes.extend(build_notes)
        horizon = _horizon(plan, horizon_s, notes)
        final_start = _final_test_start(plan, research_dataset, notes)
        walk_forward = _walk_forward(
            plan,
            research_dataset,
            train_s=train_s,
            validation_s=validation_s,
            test_s=test_s,
            horizon_s=horizon,
            notes=notes,
            limit=final_start,
        )
        spec = _training_spec(
            plan,
            cfg,
            research_dataset,
            horizon_s=horizon,
            walk_forward=walk_forward,
            final_test_start=final_start,
            candidates=tuple(candidate) if candidate else None,
            notes=notes,
        )
        registry = _open_registry()
        effective_plan = _effective_plan(plan, research_dataset, final_start, notes)
        run_id = registry.open_run(
            effective_plan, now=now, seed=cfg.research.random_seed, code_commit=code_commit()
        )
        result = walk_forward_train(research_dataset, spec)
        registry.record_trials(run_id, [t.to_dict() for t in result.trials])
        pnl = evaluate_oof(result.oof, cutoff_interval_s=research_dataset.cutoff_interval_s)
        schema_hash = _persist_feature_schema(research_dataset, now)
        artifact = artifact_from_result(result)
        artifact_path = artifacts / f"{artifact.model_id}.json"
        sha = artifact.save(artifact_path)
        model_id = registry.register_model(
            artifact.card,
            now=now,
            artifact_path=str(artifact_path),
            artifact_sha256=sha,
            status=ModelStatus.CANDIDATE,
            feature_schema_hash=schema_hash,
        )
        oof_period = Period(
            min(result.oof["decision_at"].to_list()),
            max(result.oof["decision_at"].to_list()) + timedelta(microseconds=1),
        )
        report_id = registry.write_report(
            run_id,
            kind=KIND_WALK_FORWARD,
            period=oof_period,
            metrics={
                **result.oof_metrics,
                "net_pnl": pnl.metrics["net_pnl"],
                "pnl": pnl.metrics,
                "selected_by_fold": {f.fold.fold_id: f.selected.model_name for f in result.folds},
                "notes": notes + result.notes,
            },
            now=now,
            model_id=model_id,
            independent=False,
            synthetic=research_dataset.synthetic,
        )
        registry.close_run(run_id, now=datetime.now(tz=UTC))
    except (OkxqError, ValueError) as exc:
        typer.echo(f"entraînement refusé : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(
        {
            "run_id": run_id,
            "model_id": model_id,
            "report_id": report_id,
            "artifact": {"path": str(artifact_path), "sha256": sha},
            "summary": result.summary(),
            "net_pnl": pnl.metrics["net_pnl"],
            "validated": False,
            "validation_note": (
                "rapport hors échantillon mais NON indépendant : la sélection a eu lieu sur ces folds"
            ),
            "synthetic": research_dataset.synthetic,
            "notice": SYNTHETIC_NOTICE if research_dataset.synthetic else None,
            "notes": notes,
        }
    )


@app.command("stack")
def stack(
    experiment: Path = typer.Option(..., "--experiment", exists=True, dir_okay=False),
    config: Path = typer.Option(Path("configs/paper.yaml"), "--config", exists=True, dir_okay=False),
    dataset: Path | None = typer.Option(None, "--dataset"),
    minutes: int = typer.Option(DEFAULT_SYNTHETIC_MINUTES, "--minutes", min=60),
    horizon_s: int | None = typer.Option(None, "--horizon-s"),
    train_s: int | None = typer.Option(None, "--train-s"),
    validation_s: int | None = typer.Option(None, "--validation-s"),
    test_s: int | None = typer.Option(None, "--test-s"),
    with_jev: bool = typer.Option(False, "--with-jev", help="Active l'expert JEV (entrées synthétiques)."),
) -> None:
    """Stacking OOF TEMPOREL : experts spécialisés puis méta-modèle verrouillé (jamais de fold aléatoire)."""
    plan = _plan_or_exit(experiment)
    cfg = load_or_exit(config)
    now = datetime.now(tz=UTC)
    notes: list[str] = []
    try:
        research_dataset, build_notes = _build_dataset(
            plan,
            cfg,
            dataset_folder=dataset,
            minutes=minutes,
            with_jev=with_jev,
            feature_registry=default_registry(),
        )
        notes.extend(build_notes)
        horizon = _horizon(plan, horizon_s, notes)
        final_start = _final_test_start(plan, research_dataset, notes)
        walk_forward = _walk_forward(
            plan,
            research_dataset,
            train_s=train_s,
            validation_s=validation_s,
            test_s=test_s,
            horizon_s=horizon,
            notes=notes,
            limit=final_start,
        )
        spec = _training_spec(
            plan,
            cfg,
            research_dataset,
            horizon_s=horizon,
            walk_forward=walk_forward,
            final_test_start=final_start,
            candidates=("ridge",),
            notes=notes,
        )
        registry = _open_registry()
        effective_plan = _effective_plan(plan, research_dataset, final_start, notes)
        run_id = registry.open_run(
            effective_plan, now=now, seed=cfg.research.random_seed, code_commit=code_commit()
        )
        result = stack_experts(research_dataset, spec, meta=MetaSpec())
        pnl = evaluate_oof(result.meta_oof, cutoff_interval_s=research_dataset.cutoff_interval_s)
        period = Period(
            min(result.meta_oof["decision_at"].to_list()),
            max(result.meta_oof["decision_at"].to_list()) + timedelta(microseconds=1),
        )
        report_id = registry.write_report(
            run_id,
            kind=KIND_STACKING,
            period=period,
            metrics={
                **result.meta_metrics,
                "net_pnl": pnl.metrics["net_pnl"],
                "pnl": pnl.metrics,
                "experts": result.panel.names,
                "expert_metrics": result.expert_metrics,
                "oof_temporal": True,
                "random_folds_used": False,
                "notes": notes + result.notes,
            },
            now=now,
            independent=False,
            synthetic=research_dataset.synthetic,
        )
        registry.close_run(run_id, now=datetime.now(tz=UTC))
    except (OkxqError, ValueError) as exc:
        typer.echo(f"stacking refusé : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(
        {
            "run_id": run_id,
            "report_id": report_id,
            "summary": result.summary(),
            "net_pnl": pnl.metrics["net_pnl"],
            "validated": False,
            "synthetic": research_dataset.synthetic,
            "notice": SYNTHETIC_NOTICE if research_dataset.synthetic else None,
            "notes": notes,
        }
    )


@app.command("evaluate")
def evaluate(
    run_id: str = typer.Option(..., "--run-id", help="Identifiant rendu par train/stack/ablation."),
    reports_limit: int = typer.Option(50, "--reports", min=1, max=500),
) -> None:
    """Relit une expérience et ses rapports. Lecture seule : aucune période n'est consultée ici."""
    try:
        registry = _open_registry()
        run = registry.get_run(run_id)
        reports = registry.reports(run_id, limit=reports_limit)
    except (OkxqError, ValueError) as exc:
        typer.echo(f"expérience illisible : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(
        {
            "run": run,
            "reports": reports,
            "validated": any(r["independent"] for r in reports),
            "validation_rule": "un rapport n'est une preuve que sur une période indépendante (§39.2)",
        }
    )


@app.command("final-test")
def final_test(
    run_id: str = typer.Option(..., "--run-id"),
    experiment: Path = typer.Option(..., "--experiment", exists=True, dir_okay=False),
    config: Path = typer.Option(Path("configs/paper.yaml"), "--config", exists=True, dir_okay=False),
    model_artifact: Path = typer.Option(..., "--artifact", exists=True, dir_okay=False),
    dataset: Path | None = typer.Option(None, "--dataset"),
    minutes: int = typer.Option(DEFAULT_SYNTHETIC_MINUTES, "--minutes", min=60),
    confirm: bool = typer.Option(
        False,
        "--i-understand-this-burns-the-frozen-period",
        help="Obligatoire : consulter la période finale lui retire son statut indépendant (T22).",
    ),
) -> None:
    """Mesure la période finale GELÉE une fois. La consultation est journalisée et irréversible."""
    if not confirm:
        typer.echo(
            "refus : consulter la période finale gelée la consomme. Relancer avec "
            "--i-understand-this-burns-the-frozen-period si c'est bien l'intention.",
            err=True,
        )
        raise typer.Exit(code=1)
    plan = _plan_or_exit(experiment)
    cfg = load_or_exit(config)
    now = datetime.now(tz=UTC)
    notes: list[str] = []
    try:
        research_dataset, build_notes = _build_dataset(
            plan,
            cfg,
            dataset_folder=dataset,
            minutes=minutes,
            with_jev=False,
            feature_registry=_registry_for_plan(plan, cfg),
        )
        notes.extend(build_notes)
        predictor = RegisteredModelPredictor.from_path(model_artifact)
        registry = _open_registry()
        final_start = _final_test_start(plan, research_dataset, notes)
        if final_start is None:
            typer.echo("aucune période finale gelée : rien à consulter", err=True)
            raise typer.Exit(code=1)
        period = Period(final_start, research_dataset.period_end + timedelta(microseconds=1))
        independent_before = registry.period_is_independent(run_id, period)
        horizon = predictor.horizon_s
        spec = _training_spec(
            plan,
            cfg,
            research_dataset,
            horizon_s=horizon,
            walk_forward=_walk_forward(
                plan,
                research_dataset,
                train_s=None,
                validation_s=None,
                test_s=None,
                horizon_s=horizon,
                notes=notes,
                limit=final_start,
            ),
            final_test_start=final_start,
            candidates=None,
            notes=notes,
        )
        # Le modèle publié est rejoué tel quel sur la période gelée : aucun réajustement, aucun réglage.
        result = walk_forward_train(research_dataset, spec)
        oof = predict_holdout(result, research_dataset, spec, period, producer="final_test")
        pnl = evaluate_oof(oof, cutoff_interval_s=research_dataset.cutoff_interval_s)
        report_id, consultation = registry.report_on_final_test(
            run_id,
            kind=KIND_FINAL_TEST,
            period=period,
            metrics={
                "net_pnl": pnl.metrics["net_pnl"],
                "pnl": pnl.metrics,
                "model_id": predictor.model_id,
                "independent_before_consultation": independent_before,
                "notes": notes,
            },
            now=now,
            purpose="mesure unique de la période finale gelée",
            synthetic=research_dataset.synthetic,
        )
    except (OkxqError, ValueError) as exc:
        typer.echo(f"test final refusé : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(
        {
            "run_id": run_id,
            "report_id": report_id,
            "consultation": consultation.to_dict(),
            "net_pnl": pnl.metrics["net_pnl"],
            "independent": consultation.independent,
            "consequence": (
                "cette période n'est plus un test indépendant ; toute suite exige un nouveau test "
                "indépendant ou prospectif"
            ),
            "synthetic": research_dataset.synthetic,
            "notice": SYNTHETIC_NOTICE if research_dataset.synthetic else None,
            "notes": notes,
        }
    )


def _ablation(
    experiment: Path,
    config: Path,
    dataset: Path | None,
    minutes: int,
    horizon_s: int | None,
    on: str,
) -> None:
    plan = _plan_or_exit(experiment)
    cfg = load_or_exit(config)
    if on not in ("oof", "final_test"):
        typer.echo("--on attend 'oof' ou 'final_test'", err=True)
        raise typer.Exit(code=1)
    now = datetime.now(tz=UTC)
    notes: list[str] = []
    try:
        research_dataset, build_notes = _build_dataset(
            plan,
            cfg,
            dataset_folder=dataset,
            minutes=minutes,
            with_jev=True,
            feature_registry=default_registry(),
        )
        notes.extend(build_notes)
        horizon = _horizon(plan, horizon_s, notes)
        final_start = _final_test_start(plan, research_dataset, notes)
        walk_forward = _walk_forward(
            plan,
            research_dataset,
            train_s=None,
            validation_s=None,
            test_s=None,
            horizon_s=horizon,
            notes=notes,
            limit=final_start,
        )
        spec = _training_spec(
            plan,
            cfg,
            research_dataset,
            horizon_s=horizon,
            walk_forward=walk_forward,
            final_test_start=final_start,
            candidates=("ridge",),
            notes=notes,
        )
        registry = _open_registry()
        effective_plan = _effective_plan(plan, research_dataset, final_start, notes)
        run_id = registry.open_run(
            effective_plan, now=now, seed=cfg.research.random_seed, code_commit=code_commit()
        )
        result = run_ablation(
            research_dataset,
            AblationSpec(training=spec, evaluate_on="final_test" if on == "final_test" else "oof"),
            now=now,
            registry=registry,
            run_id=run_id,
        )
        registry.close_run(run_id, now=datetime.now(tz=UTC))
    except (OkxqError, ValueError) as exc:
        typer.echo(f"ablation refusée : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload = result.summary()
    payload["notes"] = notes + list(result.notes)
    emit(payload)


@app.command("ablation")
def ablation(
    experiment: Path = typer.Option(..., "--experiment", exists=True, dir_okay=False),
    config: Path = typer.Option(Path("configs/paper.yaml"), "--config", exists=True, dir_okay=False),
    dataset: Path | None = typer.Option(None, "--dataset"),
    minutes: int = typer.Option(DEFAULT_SYNTHETIC_MINUTES, "--minutes", min=60),
    horizon_s: int | None = typer.Option(None, "--horizon-s"),
    on: str = typer.Option("oof", "--on", help="Période mesurée : oof (défaut) ou final_test."),
) -> None:
    """Ablation JEV A/B/C/D : budgets, fenêtres et coûts identiques pour toutes les variantes (§40)."""
    _ablation(experiment, config, dataset, minutes, horizon_s, on)


@app.command("compare-jev")
def compare_jev(
    experiment: Path = typer.Option(..., "--experiment", exists=True, dir_okay=False),
    config: Path = typer.Option(Path("configs/paper.yaml"), "--config", exists=True, dir_okay=False),
    dataset: Path | None = typer.Option(None, "--dataset"),
    minutes: int = typer.Option(DEFAULT_SYNTHETIC_MINUTES, "--minutes", min=60),
    horizon_s: int | None = typer.Option(None, "--horizon-s"),
    on: str = typer.Option("oof", "--on"),
) -> None:
    """Alias de ``ablation`` sous le nom attendu par §67."""
    _ablation(experiment, config, dataset, minutes, horizon_s, on)


@registry_app.command("list")
def registry_list(
    limit: int = typer.Option(25, "--limit", min=1, max=200),
) -> None:
    """Liste les expériences enregistrées avec leur budget d'essais et l'état du test final."""
    try:
        runs = _open_registry().list_runs(limit=limit)
    except (OkxqError, ValueError) as exc:
        typer.echo(f"registre illisible : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(
        {
            "count": len(runs),
            "items": runs,
            "note": (
                "final_test_independent=false signifie que la période finale a été consultée : "
                "cette expérience n'est plus une preuve indépendante"
            ),
        }
    )


@registry_app.command("models")
def registry_models(
    limit: int = typer.Option(25, "--limit", min=1, max=200),
) -> None:
    """Liste les versions de modèles enregistrées, avec statut §56 et limites connues."""
    try:
        models = _open_registry().models(limit=limit)
    except (OkxqError, ValueError) as exc:
        typer.echo(f"registre illisible : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit({"count": len(models), "items": models, "statuses": [s.value for s in ModelStatus]})


@registry_app.command("promote")
def registry_promote(
    model_id: str = typer.Option(..., "--model-id"),
    status: str = typer.Option(..., "--status", help=f"Un de : {', '.join(s.value for s in ModelStatus)}"),
    actor: str = typer.Option(..., "--actor", help="Opérateur nommé : aucune auto-approbation."),
) -> None:
    """Promotion explicite. Refusée sans rapport sur période indépendante ; LIVE passe par §71."""
    try:
        target = ModelStatus(status)
    except ValueError as exc:
        typer.echo(f"statut inconnu : {status}", err=True)
        raise typer.Exit(code=1) from exc
    try:
        model = _open_registry().promote(model_id, status=target, actor=actor, now=datetime.now(tz=UTC))
    except (OkxqError, ValueError) as exc:
        typer.echo(f"promotion refusée : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit({"model": model})
