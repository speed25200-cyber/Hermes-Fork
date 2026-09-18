"""Registre d'expériences : plan enregistré AVANT exécution, tous les essais, période finale GELÉE (§39, §56).

POURQUOI ce registre existe : sans lui, « le modèle marche » est une affirmation invérifiable. Il impose
trois choses qu'aucune convention de nommage ne remplace :

1. **Le plan précède les résultats.** ``open_run`` écrit ``ExperimentPlan`` (hypothèse, période, univers,
   features, labels, modèle, coûts, critères, budget) et son hash AVANT le premier essai. Un essai
   enregistré sans run ouvert est refusé.
2. **Tous les essais sont journalisés, pas seulement le gagnant** (§39.2) : le risque de sélection
   multiple ne se mesure qu'avec le dénominateur.
3. **La période finale est gelée.** ``consult_final_test`` inscrit ``final_test_consulted_at`` à la
   PREMIÈRE consultation et la période perd immédiatement son statut indépendant (T22). Ensuite,
   ``assert_optimization_allowed`` refuse tout nouvel essai sur cette expérience : une expérience dont le
   test final a été consulté n'est plus une preuve, elle est devenue de l'information de développement.
   Chaque consultation laisse une trace consultable (rapport ``final_test_consultation``).

Vocabulaire des statuts de modèle : celui de §56 (``ModelStatus``), écrit tel quel dans
``model_versions.status``. L'API de lecture (``/api/v1/models``) marque ``validated`` sur un vocabulaire
plus ancien et en minuscules ; elle affiche donc ``validated: false`` pour ces statuts. C'est un
sous-affichage prudent, jamais une sur-affirmation : dans cette session, aucun modèle ne dispose de
preuve indépendante.

Aucun montant monétaire ne transite ici : les métriques sont des fractions de notionnel et des comptages,
stockées en JSON. Les montants (``Money``, ``Decimal``) appartiennent au ledger, pas au registre.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import ProtocolViolationError
from okxq.domain.ids import new_id, payload_hash
from okxq.persistence.models import EvaluationReport, ExperimentRun, ModelVersion
from okxq.research import SYNTHETIC_NOTICE
from okxq.research.splits import FinalTestGuard, Period
from okxq.research.training import ModelStatus

RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_FROZEN = "final_test_consulted"

KIND_FINAL_TEST_CONSULTATION = "final_test_consultation"
KIND_WALK_FORWARD = "walk_forward_oof"
KIND_STACKING = "stacking_oof"
KIND_FINAL_TEST = "final_test"
KIND_JEV_ABLATION = "jev_ablation"

# Ces statuts exigent une preuve sur période indépendante ; aucun d'eux ne s'obtient automatiquement.
PROMOTION_REQUIRES_EVIDENCE: frozenset[ModelStatus] = frozenset(
    {
        ModelStatus.VALIDATED_OFFLINE,
        ModelStatus.SHADOW,
        ModelStatus.DEMO_TECH_VALIDATED,
    }
)


@dataclass(frozen=True, slots=True)
class ExperimentPlan:
    """Plan d'expérience (§39). Il est enregistré tel quel : les résultats ne le réécrivent jamais."""

    plan_id: str
    hypothesis: str
    payload: dict[str, Any]
    period: Period
    final_test_start: datetime | None
    seed: int

    @property
    def plan_hash(self) -> str:
        return payload_hash(self.payload)

    @property
    def max_trials(self) -> int | None:
        budget = self.payload.get("budget") or {}
        value = budget.get("max_trials") or (self.payload.get("model") or {}).get("max_trials")
        return int(value) if value is not None else None

    @property
    def horizons_s(self) -> list[int]:
        labels = self.payload.get("labels") or {}
        return [int(h) for h in labels.get("horizons_seconds", [])]

    @property
    def target(self) -> str | None:
        labels = self.payload.get("labels") or {}
        value = labels.get("target")
        return str(value) if value else None

    @property
    def candidates(self) -> list[str]:
        model = self.payload.get("model") or {}
        return [str(c) for c in model.get("candidates", [])]

    @property
    def feature_groups(self) -> list[str] | None:
        features = self.payload.get("features") or {}
        groups = features.get("groups")
        return [str(g) for g in groups] if groups else None

    @property
    def dataset_folder(self) -> Path | None:
        data = self.payload.get("data") or {}
        folder = data.get("dataset")
        return Path(str(folder)) if folder else None

    def final_test_period(self) -> Period | None:
        if self.final_test_start is None:
            return None
        return Period(self.final_test_start, self.period.end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "hypothesis": self.hypothesis,
            "period": self.period.to_dict(),
            "final_test_start": self.final_test_start.isoformat() if self.final_test_start else None,
            "seed": self.seed,
            **self.payload,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ExperimentPlan:
        body = dict(payload)
        for key in ("plan_id", "hypothesis", "period"):
            if key not in body:
                raise ProtocolViolationError("plan d'expérience incomplet", missing=key)
        period_block = dict(body["period"])
        start, end = period_block.get("start"), period_block.get("end")
        if not start or not end:
            raise ProtocolViolationError("période du plan incomplète (start/end requis)")
        frozen = period_block.get("frozen_final_test_start")
        final_test_start = ensure_utc(datetime.fromisoformat(str(frozen))) if frozen else None
        period = Period(
            ensure_utc(datetime.fromisoformat(str(start))), ensure_utc(datetime.fromisoformat(str(end)))
        )
        if final_test_start is not None and not period.contains(final_test_start):
            raise ProtocolViolationError(
                "la période finale gelée doit tomber dans la période du plan",
                final_test_start=final_test_start.isoformat(),
                period=period.to_dict(),
            )
        return cls(
            plan_id=str(body["plan_id"]),
            hypothesis=str(body["hypothesis"]),
            payload=body,
            period=period,
            final_test_start=final_test_start,
            seed=int(body.get("seed", 0)),
        )

    def rescaled(
        self,
        *,
        start: datetime,
        end: datetime,
        final_test_start: datetime | None,
        reason: str,
    ) -> ExperimentPlan:
        """Plan RÉELLEMENT exécuté quand la période disponible diffère de celle du fichier.

        POURQUOI ne pas garder la période du fichier : l'indépendance d'un rapport se juge contre la
        période FINALE réellement gelée. Enregistrer une période que les données ne couvrent pas rendrait
        tout rapport « non indépendant » pour une raison administrative, ou pire, laisserait croire qu'une
        période a été réservée alors qu'elle n'existe pas. Le hash du plan d'origine est conservé
        (``period_rescaled.source_plan_hash``) : la traçabilité ne se perd pas.
        """
        payload = dict(self.payload)
        payload["period"] = {
            "start": ensure_utc(start).isoformat(),
            "end": ensure_utc(end).isoformat(),
            "frozen_final_test_start": (
                ensure_utc(final_test_start).isoformat() if final_test_start is not None else None
            ),
        }
        payload["period_rescaled"] = {
            "reason": reason,
            "source_plan_hash": self.plan_hash,
            "source_period": self.period.to_dict(),
            "source_frozen_final_test_start": (
                self.final_test_start.isoformat() if self.final_test_start else None
            ),
        }
        return ExperimentPlan.from_mapping(payload)

    @classmethod
    def load(cls, path: Path) -> ExperimentPlan:
        """Charge un plan YAML (``configs/experiment.example.yaml``). ``safe_load`` uniquement."""
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ProtocolViolationError("plan d'expérience illisible", path=str(path))
        return cls.from_mapping(raw)


@dataclass(frozen=True, slots=True)
class FinalTestConsultation:
    """Trace d'une consultation de la période finale gelée."""

    run_id: str
    report_id: str
    consulted_at: datetime
    first_consultation: bool
    independent: bool
    purpose: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "report_id": self.report_id,
            "consulted_at": self.consulted_at.isoformat(),
            "first_consultation": self.first_consultation,
            "independent": self.independent,
            "purpose": self.purpose,
        }


class ExperimentRegistry:
    """Accès transactionnel aux tables ``experiment_runs``, ``evaluation_reports``, ``model_versions``."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    # --- runs ---------------------------------------------------------------------------------------

    def open_run(
        self,
        plan: ExperimentPlan,
        *,
        now: datetime,
        seed: int | None = None,
        code_commit: str | None = None,
    ) -> str:
        """Enregistre le plan AVANT toute exécution et rend le ``run_id`` (§67 : il n'est pas inventé)."""
        run_id = new_id("exp")
        with self._factory() as session:
            session.add(
                ExperimentRun(
                    run_id=run_id,
                    plan_id=plan.plan_id,
                    plan_hash=plan.plan_hash,
                    plan=plan.to_dict(),
                    started_at=ensure_utc(now),
                    finished_at=None,
                    status=RUN_STATUS_RUNNING,
                    trials=[],
                    final_test_consulted_at=None,
                    code_commit=code_commit,
                    seed=int(seed if seed is not None else plan.seed),
                )
            )
            session.commit()
        return run_id

    def _run(self, session: Session, run_id: str) -> ExperimentRun:
        run = session.get(ExperimentRun, run_id)
        if run is None:
            raise ProtocolViolationError("expérience inconnue", run_id=run_id)
        return run

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._factory() as session:
            run = self._run(session, run_id)
            return _run_to_dict(run)

    def final_test_consulted_at(self, run_id: str) -> datetime | None:
        with self._factory() as session:
            run = self._run(session, run_id)
            return ensure_utc(run.final_test_consulted_at) if run.final_test_consulted_at else None

    def assert_optimization_allowed(self, run_id: str) -> None:
        """Refuse toute optimisation supplémentaire après consultation du test final (T22)."""
        consulted = self.final_test_consulted_at(run_id)
        if consulted is not None:
            raise ProtocolViolationError(
                "le test final de cette expérience a déjà été consulté : plus aucune optimisation n'y est "
                "admise, un nouveau test indépendant (ou prospectif) est exigé",
                run_id=run_id,
                final_test_consulted_at=consulted.isoformat(),
            )

    def record_trials(
        self,
        run_id: str,
        trials: Sequence[Mapping[str, Any]],
        *,
        budget_scope: str | Sequence[str] | None = None,
    ) -> int:
        """Ajoute des essais au journal. Tous les essais comptent, pas seulement celui qui gagne.

        ``budget_scope`` nomme le ou les champs qui identifient une DÉCISION DE SÉLECTION : ``"fold_id"``
        pour un walk-forward (chaque fold choisit ses hyperparamètres), ``("variant", "fold_id")`` pour
        une ablation (chaque variante rechoisit à chaque fold). Le budget du plan s'applique alors à
        chacune de ces décisions séparément.

        POURQUOI pas un budget global : §40 exige des budgets de recherche COMPARABLES entre variantes.
        Un budget global laisserait régler la première variante dix fois plus que la dernière, puis
        présenter l'écart comme un effet causal. Le total reste journalisé, donc auditable.
        """
        self.assert_optimization_allowed(run_id)
        with self._factory() as session:
            run = self._run(session, run_id)
            if run.status != RUN_STATUS_RUNNING:
                raise ProtocolViolationError(
                    "expérience close : aucun essai supplémentaire", run_id=run_id, status=run.status
                )
            budget = (run.plan.get("budget") or {}).get("max_trials")
            merged = [*run.trials, *[dict(t) for t in trials]]
            if budget is not None:
                fields = (
                    ()
                    if budget_scope is None
                    else ((budget_scope,) if isinstance(budget_scope, str) else tuple(budget_scope))
                )
                counts: dict[str, int] = {}
                for trial in merged:
                    key = "|".join(str(trial.get(f, "")) for f in fields)
                    counts[key] = counts.get(key, 0) + 1
                over = {k: v for k, v in counts.items() if v > int(budget)}
                if over:
                    raise ProtocolViolationError(
                        "budget d'essais dépassé : le plan borne la recherche",
                        run_id=run_id,
                        budget=int(budget),
                        budget_scope=list(fields) or None,
                        over_budget=over,
                    )
            run.trials = merged  # réaffectation explicite : SQLAlchemy ne suit pas une mutation en place
            session.commit()
            return len(merged)

    def close_run(self, run_id: str, *, now: datetime, status: str = RUN_STATUS_COMPLETED) -> None:
        with self._factory() as session:
            run = self._run(session, run_id)
            run.status = status
            run.finished_at = ensure_utc(now)
            session.commit()

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._factory() as session:
            rows = session.scalars(
                select(ExperimentRun).order_by(ExperimentRun.started_at.desc()).limit(limit)
            ).all()
            return [_run_to_dict(r) for r in rows]

    # --- période finale gelée -----------------------------------------------------------------------

    def consult_final_test(
        self, run_id: str, period: Period, *, now: datetime, purpose: str
    ) -> FinalTestConsultation:
        """Consulte la période finale. Première fois ⇒ indépendante ; ensuite ⇒ plus jamais une preuve."""
        moment = ensure_utc(now)
        report_id = new_id("rep")
        with self._factory() as session:
            run = self._run(session, run_id)
            first = run.final_test_consulted_at is None
            if first:
                run.final_test_consulted_at = moment
                run.status = RUN_STATUS_FROZEN if run.status == RUN_STATUS_RUNNING else run.status
            previous = int(
                session.scalar(
                    select(func.count())
                    .select_from(EvaluationReport)
                    .where(
                        EvaluationReport.run_id == run_id,
                        EvaluationReport.kind == KIND_FINAL_TEST_CONSULTATION,
                    )
                )
                or 0
            )
            other_runs = self._consultations_for_plan(session, run.plan_id, exclude_run_id=run_id)
            first = first and other_runs == 0
            session.add(
                EvaluationReport(
                    report_id=report_id,
                    run_id=run_id,
                    model_id=None,
                    kind=KIND_FINAL_TEST_CONSULTATION,
                    period_start=period.start,
                    period_end=period.end,
                    independent=first,
                    metrics={
                        "purpose": purpose,
                        "first_consultation": first,
                        "previous_consultations": previous,
                        "previous_consultations_other_runs_same_plan": other_runs,
                        "consulted_at": moment.isoformat(),
                        "consequence": (
                            "période finale consommée : toute mesure ultérieure sur cette période n'est "
                            "plus un test indépendant"
                        ),
                    },
                    created_at=moment,
                )
            )
            session.commit()
        return FinalTestConsultation(
            run_id=run_id,
            report_id=report_id,
            consulted_at=moment,
            first_consultation=first,
            independent=first,
            purpose=purpose,
        )

    def final_test_guard(self, run_id: str, period: Period) -> FinalTestGuard:
        """``FinalTestGuard`` synchronisé avec la base : l'état en mémoire ne contredit jamais le journal."""
        guard = FinalTestGuard(period)
        consulted = self.final_test_consulted_at(run_id)
        if consulted is not None:
            guard.consulted_at = consulted
            guard.consultations.append({"at": consulted.isoformat(), "purpose": "restauré depuis le journal"})
        return guard

    def period_is_independent(self, run_id: str, period: Period) -> bool:
        """Indépendante ⇔ la période est DANS le test final gelé ET ce test n'a pas encore été consulté.

        Dès la première consultation, la période devient de l'information de développement : toute mesure
        ultérieure, même identique, cesse d'être une preuve (§39.2, T22).
        """
        with self._factory() as session:
            run = self._run(session, run_id)
            final = plan_final_test_period(run.plan)
            if final is None or run.final_test_consulted_at is not None:
                return False
            if not (period.start >= final.start and period.end <= final.end):
                return False
            # La période gelée appartient au PLAN, pas à une exécution : si une autre exécution du même
            # plan l'a déjà consultée, relancer une expérience ne lui rend pas son statut de preuve.
            return self._consultations_for_plan(session, run.plan_id, exclude_run_id=run_id) == 0

    @staticmethod
    def _consultations_for_plan(session: Session, plan_id: str, *, exclude_run_id: str | None) -> int:
        query = (
            select(func.count())
            .select_from(ExperimentRun)
            .where(ExperimentRun.plan_id == plan_id, ExperimentRun.final_test_consulted_at.is_not(None))
        )
        if exclude_run_id is not None:
            query = query.where(ExperimentRun.run_id != exclude_run_id)
        return int(session.scalar(query) or 0)

    # --- rapports -----------------------------------------------------------------------------------

    def write_report(
        self,
        run_id: str,
        *,
        kind: str,
        period: Period,
        metrics: Mapping[str, Any],
        now: datetime,
        model_id: str | None = None,
        independent: bool = False,
        synthetic: bool = False,
    ) -> str:
        """Écrit un rapport. ``independent`` est FAUX par défaut : une mesure n'est pas une preuve d'office."""
        report_id = new_id("rep")
        body = dict(metrics)
        if synthetic:
            body.setdefault("synthetic", True)
            body.setdefault("notice", SYNTHETIC_NOTICE)
            body.setdefault(
                "warning",
                "résultat obtenu sur données synthétiques : aucune preuve d'avantage de marché",
            )
        with self._factory() as session:
            self._run(session, run_id)
            if model_id is not None and session.get(ModelVersion, model_id) is None:
                raise ProtocolViolationError(
                    "rapport lié à un modèle non enregistré : enregistrer le modèle d'abord",
                    model_id=model_id,
                )
            session.add(
                EvaluationReport(
                    report_id=report_id,
                    run_id=run_id,
                    model_id=model_id,
                    kind=kind,
                    period_start=period.start,
                    period_end=period.end,
                    independent=bool(independent),
                    metrics=body,
                    created_at=ensure_utc(now),
                )
            )
            session.commit()
        return report_id

    def report_on_final_test(
        self,
        run_id: str,
        *,
        kind: str,
        period: Period,
        metrics: Mapping[str, Any],
        now: datetime,
        purpose: str,
        model_id: str | None = None,
        synthetic: bool = False,
    ) -> tuple[str, FinalTestConsultation]:
        """Mesure sur la période finale : la consultation est journalisée AVANT l'écriture du rapport."""
        consultation = self.consult_final_test(run_id, period, now=now, purpose=purpose)
        body = dict(metrics)
        body["final_test_consulted_at"] = consultation.consulted_at.isoformat()
        body["first_consultation"] = consultation.first_consultation
        if not consultation.first_consultation:
            body["warning"] = (
                "période finale déjà consultée : ce chiffre est de l'information de développement, "
                "pas un test indépendant"
            )
        report_id = self.write_report(
            run_id,
            kind=kind,
            period=period,
            metrics=body,
            now=now,
            model_id=model_id,
            independent=consultation.independent,
            synthetic=synthetic,
        )
        return report_id, consultation

    def reports(self, run_id: str | None = None, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._factory() as session:
            query = select(EvaluationReport)
            if run_id is not None:
                query = query.where(EvaluationReport.run_id == run_id)
            rows = session.scalars(query.order_by(EvaluationReport.created_at.desc()).limit(limit)).all()
            return [_report_to_dict(r) for r in rows]

    # --- modèles ------------------------------------------------------------------------------------

    def register_model(
        self,
        card: Mapping[str, Any],
        *,
        now: datetime,
        artifact_path: str | None = None,
        artifact_sha256: str | None = None,
        status: ModelStatus = ModelStatus.CANDIDATE,
        feature_schema_hash: str | None = None,
    ) -> str:
        """Enregistre une version de modèle avec sa carte complète (§56). Statut initial : CANDIDATE."""
        model_id = str(card.get("model_id") or new_id("mdl"))
        dataset_hash = card.get("dataset_hash")
        if not dataset_hash:
            raise ProtocolViolationError("carte de modèle sans hash de jeu de données")
        period_start, period_end = card.get("period_start"), card.get("period_end")
        if not period_start or not period_end:
            raise ProtocolViolationError("carte de modèle sans période d'entraînement")
        with self._factory() as session:
            existing = session.get(ModelVersion, model_id)
            if existing is not None:
                # Idempotence : la même recherche produit le même identifiant déterministe.
                return model_id
            session.add(
                ModelVersion(
                    model_id=model_id,
                    family=str(card.get("family", "unknown")),
                    status=status.value,
                    code_commit=card.get("code_commit"),
                    dataset_hash=str(dataset_hash),
                    feature_schema_hash=feature_schema_hash,
                    period_start=ensure_utc(datetime.fromisoformat(str(period_start))),
                    period_end=ensure_utc(datetime.fromisoformat(str(period_end))),
                    universe_version=card.get("universe_version"),
                    manifest=dict(card),
                    artifact_path=artifact_path,
                    artifact_sha256=artifact_sha256,
                    created_at=ensure_utc(now),
                    promoted_at=None,
                    promoted_by=None,
                )
            )
            session.commit()
        return model_id

    def promote(self, model_id: str, *, status: ModelStatus, actor: str, now: datetime) -> dict[str, Any]:
        """Promotion explicite et prouvée. Aucune promotion automatique, aucun ``--force`` (§56, §71)."""
        if not actor.strip():
            raise ProtocolViolationError("une promotion exige un opérateur nommé (aucune auto-approbation)")
        if status is ModelStatus.LIVE_APPROVED:
            raise ProtocolViolationError(
                "LIVE_APPROVED ne s'obtient pas dans le registre de recherche : il passe par le workflow "
                "d'autorisation humaine (§71) et son manifeste signé",
                model_id=model_id,
            )
        with self._factory() as session:
            model = session.get(ModelVersion, model_id)
            if model is None:
                raise ProtocolViolationError("modèle inconnu", model_id=model_id)
            if status in PROMOTION_REQUIRES_EVIDENCE:
                proofs = session.scalars(
                    select(EvaluationReport).where(
                        EvaluationReport.model_id == model_id,
                        EvaluationReport.independent.is_(True),
                    )
                ).all()
                if not proofs:
                    raise ProtocolViolationError(
                        "promotion refusée : aucun rapport sur période indépendante pour ce modèle",
                        model_id=model_id,
                        requested=status.value,
                    )
                if any(bool(p.metrics.get("synthetic")) for p in proofs):
                    raise ProtocolViolationError(
                        "promotion refusée : les seules preuves disponibles viennent de données "
                        "synthétiques, qui ne mesurent aucun avantage de marché",
                        model_id=model_id,
                    )
            model.status = status.value
            model.promoted_at = ensure_utc(now)
            model.promoted_by = actor
            manifest = dict(model.manifest)
            manifest["promotion_status"] = status.value
            model.manifest = manifest
            session.commit()
            return _model_to_dict(model)

    def models(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._factory() as session:
            rows = session.scalars(
                select(ModelVersion).order_by(ModelVersion.created_at.desc()).limit(limit)
            ).all()
            return [_model_to_dict(m) for m in rows]

    def model(self, model_id: str) -> dict[str, Any]:
        with self._factory() as session:
            model = session.get(ModelVersion, model_id)
            if model is None:
                raise ProtocolViolationError("modèle inconnu", model_id=model_id)
            return _model_to_dict(model)


def plan_final_test_period(plan: Mapping[str, Any]) -> Period | None:
    """Période finale gelée telle qu'elle a été ENREGISTRÉE dans le plan (jamais recalculée après coup)."""
    block = plan.get("period") or {}
    start, end = block.get("frozen_final_test_start"), block.get("end")
    if not start or not end:
        return None
    return Period(
        ensure_utc(datetime.fromisoformat(str(start))), ensure_utc(datetime.fromisoformat(str(end)))
    )


def _run_to_dict(run: ExperimentRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "plan_id": run.plan_id,
        "plan_hash": run.plan_hash,
        "hypothesis": run.plan.get("hypothesis"),
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "trials": len(run.trials),
        "final_test_consulted_at": (
            run.final_test_consulted_at.isoformat() if run.final_test_consulted_at else None
        ),
        "final_test_independent": run.final_test_consulted_at is None,
        "code_commit": run.code_commit,
        "seed": run.seed,
    }


def _report_to_dict(report: EvaluationReport) -> dict[str, Any]:
    return {
        "report_id": report.report_id,
        "run_id": report.run_id,
        "model_id": report.model_id,
        "kind": report.kind,
        "period_start": report.period_start.isoformat(),
        "period_end": report.period_end.isoformat(),
        "independent": report.independent,
        "validated": bool(report.independent),
        "metrics": report.metrics,
        "created_at": report.created_at.isoformat(),
    }


def _model_to_dict(model: ModelVersion) -> dict[str, Any]:
    return {
        "model_id": model.model_id,
        "family": model.family,
        "status": model.status,
        "code_commit": model.code_commit,
        "dataset_hash": model.dataset_hash,
        "feature_schema_hash": model.feature_schema_hash,
        "period_start": model.period_start.isoformat(),
        "period_end": model.period_end.isoformat(),
        "artifact_path": model.artifact_path,
        "artifact_sha256": model.artifact_sha256,
        "created_at": model.created_at.isoformat(),
        "promoted_at": model.promoted_at.isoformat() if model.promoted_at else None,
        "promoted_by": model.promoted_by,
        "known_limits": model.manifest.get("known_limits", []),
        "synthetic": bool(model.manifest.get("synthetic")),
    }
