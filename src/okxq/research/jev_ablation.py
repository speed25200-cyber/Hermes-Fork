"""Ablation JEV : variantes contrôlées A / B / C / D et rapports d'ablation (§40, T22).

POURQUOI ces quatre variantes, et pas simplement « avec / sans IA » :

```text
A : modèles quantitatifs seuls
B : A + métadonnées d'événements (présence, nombre, âge) — AUCUNE sémantique
C : B + variables sémantiques JEV
D : C avec un groupe de questions JEV retiré à la fois
```

B existe pour ne pas attribuer à JEV un gain qui vient de la SIMPLE PRÉSENCE d'une annonce : sans elle,
« C bat A » ne distingue pas la lecture sémantique du fait qu'il se passait quelque chose. D mesure la
contribution marginale de chaque groupe de questions.

Règles d'équité imposées par le code, pas par la bonne volonté :
- toutes les variantes partagent le MÊME jeu de données (mêmes lignes, mêmes labels, mêmes coûts) ;
  seules les colonnes de features changent ;
- toutes partagent la MÊME ``TrainingSpec`` : mêmes folds, même graine, même budget d'essais, même
  politique de signal et même multiplicateur de coûts. On ne règle pas C dix fois plus que A ;
- ``net_pnl`` vient du même simulateur pour tout le monde (``evaluate_oof``, frais et spread inclus).

Statut des résultats :
- une variante mesurée sur une période DÉJÀ consultée porte ``independent=False`` : ce n'est plus une
  preuve, c'est de l'information de développement (§39.2) ;
- sur données synthétiques, chaque rapport porte l'avertissement explicite : aucun avantage de marché
  n'est mesuré. Les probabilités JEV synthétiques de ce module sont du BRUIT seedé, indépendant du
  futur, précisément pour que C ne puisse pas « gagner » par construction ;
- la conclusion ``jev_adds_net_utility`` vaut ``None`` quand la question ne peut pas être tranchée. Elle
  n'est jamais forcée à ``True`` pour pouvoir annoncer « IA avec JEV » (§40).

Unités : ``net_pnl`` est un rendement CUMULÉ en fraction de notionnel par intervalle de décision (le
contrat de ``okxq.research.evaluation.simulate_pnl``), pas un montant. Les montants restent au ledger.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
import polars as pl

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import ProtocolViolationError
from okxq.domain.events import JevAnswer, JevAnswerType, JevEvaluation, JevStatus
from okxq.domain.ids import payload_hash
from okxq.features.events import EVENT_CATEGORIES, GROUP_JEV, GROUP_META, JEV_NAMES
from okxq.features.state import AnnouncementMeta
from okxq.research import SYNTHETIC_NOTICE
from okxq.research.datasets import ResearchDataset
from okxq.research.evaluation import SignalPolicy
from okxq.research.experiment_registry import KIND_JEV_ABLATION, ExperimentRegistry
from okxq.research.splits import Period
from okxq.research.stacking import features_by_group
from okxq.research.training import (
    TrainingResult,
    TrainingSpec,
    evaluate_oof,
    predict_holdout,
    walk_forward_train,
)

VARIANT_A = "A_quant_only"
VARIANT_B = "B_event_metadata"
VARIANT_C = "C_jev_semantic"
VARIANT_D_PREFIX = "D_without_"

SYNTHETIC_JEV_WARNING = (
    "variables sémantiques JEV SYNTHÉTIQUES : bruit seedé indépendant du futur, aucun avantage de marché"
)

# Groupes de questions JEV, retirés un à un par la variante D (§40). Les noms viennent du registre de
# features (``okxq.features.events``) : ce module ne redéfinit aucune feature.
JEV_QUESTION_GROUPS: dict[str, tuple[str, ...]] = {
    "availability": ("jev_available", "jev_age_s"),
    "event_kind": ("jev_confidence", *(f"jev_p_{c}" for c in EVENT_CATEGORIES)),
    "relevance": ("jev_relevance",),
    "severity_impact": ("jev_severity_expected", "jev_impact_expected"),
    "novelty": ("jev_novelty_expected",),
    "source": ("jev_source_confirms",),
}

# Nombre de niveaux de chaque question de score, reflet de ``configs/jev_questions.v1.json``. Un test
# vérifie l'égalité : un changement du jeu de questions impose une revalidation (§40, T53).
FALLBACK_SCORE_LEVELS: dict[str, int] = {
    "reported_severity": 5,
    "novelty_vs_prior": 3,
    "reported_operational_impact": 3,
}
SCORE_QUESTION_IDS: tuple[str, ...] = tuple(FALLBACK_SCORE_LEVELS)
NOUL_QUESTION_IDS: tuple[str, ...] = ("asset_relevance", "source_confirms")


def assert_jev_groups_cover_registry() -> None:
    """Toute feature JEV du registre appartient à exactement un groupe de questions (sinon D est aveugle)."""
    covered = [n for names in JEV_QUESTION_GROUPS.values() for n in names]
    if sorted(covered) != sorted(JEV_NAMES):
        raise ProtocolViolationError(
            "les groupes de questions JEV ne couvrent pas exactement les features JEV du registre",
            missing=sorted(set(JEV_NAMES) - set(covered)),
            unknown=sorted(set(covered) - set(JEV_NAMES)),
        )


# --- entrées JEV synthétiques (bruit, jamais un signal planté) --------------------------------------------


def question_set_score_levels(path: Path | None = None) -> dict[str, int]:
    """Niveaux de score lus dans le jeu de questions ; à défaut, la table de repli documentée."""
    if path is None or not path.exists():
        return dict(FALLBACK_SCORE_LEVELS)
    from okxq.jev.schemas import load_question_set

    question_set = load_question_set(path)
    levels: dict[str, int] = {}
    for qid, spec in question_set.questions.items():
        criteria = spec.criteria
        if spec.type == "score" and isinstance(criteria, list):
            levels[qid] = len(criteria)
    return levels or dict(FALLBACK_SCORE_LEVELS)


def _dirichlet_like(rng: np.random.Generator, size: int) -> list[float]:
    raw = rng.random(size) + 1e-6
    total = float(raw.sum())
    return [float(v / total) for v in raw]


def synthetic_jev_evaluations(
    instruments: Sequence[str],
    start: datetime,
    minutes: int,
    *,
    seed: int,
    interval_s: int = 1800,
    question_set_path: Path | None = None,
) -> dict[str, list[JevEvaluation]]:
    """Évaluations JEV SYNTHÉTIQUES, seedées et SANS information sur le futur.

    POURQUOI en produire : sans aucune évaluation, toutes les features JEV sont masquées, la variante C
    est identique à B et l'ablation ne mesure rien. Avec du bruit indépendant du futur, l'ablation est
    exécutable ET son résultat attendu est « JEV n'apporte rien » — ce qui est exactement ce que le
    protocole doit être capable de conclure.

    Causalité : ``features_committed_at = requested_at + latence`` ; une feature n'est utilisable qu'à
    partir de cet instant (``okxq.features.events.select_evaluation``), jamais à l'heure du document.
    """
    rng = np.random.default_rng(seed)
    levels = question_set_score_levels(question_set_path)
    begin = ensure_utc(start)
    end = begin + timedelta(minutes=minutes)
    out: dict[str, list[JevEvaluation]] = {}
    for instrument in instruments:
        evaluations: list[JevEvaluation] = []
        moment = begin
        index = 0
        while moment <= end:
            requested_at = moment
            inference_at = requested_at + timedelta(seconds=20)
            committed_at = inference_at + timedelta(seconds=5)
            answers: dict[str, JevAnswer] = {
                "event_kind": JevAnswer(
                    type=JevAnswerType.CHOICE,
                    probabilities=dict(
                        zip(EVENT_CATEGORIES, _dirichlet_like(rng, len(EVENT_CATEGORIES)), strict=True)
                    ),
                    confidence=float(rng.uniform(0.3, 0.9)),
                ),
            }
            for qid in NOUL_QUESTION_IDS:
                answers[qid] = JevAnswer(type=JevAnswerType.NOUL, probability=float(rng.uniform(0.0, 1.0)))
            for qid in SCORE_QUESTION_IDS:
                n_levels = int(levels.get(qid, FALLBACK_SCORE_LEVELS[qid]))
                answers[qid] = JevAnswer(
                    type=JevAnswerType.SCORE,
                    score_distribution=dict(
                        zip(
                            [str(k) for k in range(n_levels)],
                            _dirichlet_like(rng, n_levels),
                            strict=True,
                        )
                    ),
                )
            document_id = f"syndoc_{instrument}_{index:05d}"
            evaluations.append(
                JevEvaluation(
                    evaluation_id=f"synjev_{instrument}_{index:05d}",
                    document_id=document_id,
                    document_version=1,
                    question_set_hash=payload_hash({"synthetic_question_set": sorted(answers)}),
                    asset_mapping_version=f"synthetic-mapping-v1#{instrument}",
                    model_requested="synthetic-jev",
                    model_effective="synthetic-jev",
                    requested_at=requested_at,
                    completed_at=inference_at,
                    inference_completed_at=inference_at,
                    features_committed_at=committed_at,
                    answers=answers,
                    usage={"synthetic": True},
                    status=JevStatus.OK,
                    inst_id=instrument,
                )
            )
            moment = moment + timedelta(seconds=interval_s)
            index += 1
        out[instrument] = evaluations
    return out


def announcements_from_evaluations(
    evaluations: Mapping[str, Sequence[JevEvaluation]],
) -> dict[str, list[AnnouncementMeta]]:
    """Variante B : les MÉTADONNÉES des mêmes documents, sans une seule variable sémantique."""
    out: dict[str, list[AnnouncementMeta]] = {}
    for instrument, items in evaluations.items():
        metas: list[AnnouncementMeta] = []
        for evaluation in items:
            first_seen = ensure_utc(evaluation.requested_at)
            metas.append(
                AnnouncementMeta(
                    document_id=evaluation.document_id,
                    first_seen_at=first_seen,
                    available_at=first_seen,
                    published_at=None,
                )
            )
        out[instrument] = metas
    return out


# --- construction des variantes ---------------------------------------------------------------------------


def variant_feature_sets(
    dataset: ResearchDataset, *, include_d_variants: bool = True
) -> dict[str, list[str]]:
    """Jeux de features par variante. Les groupes viennent du schéma enregistré avec le jeu de données."""
    by_group = features_by_group(dataset)
    quant_groups = [g for g in by_group if g not in (GROUP_META, GROUP_JEV)]
    a_names = [n for g in quant_groups for n in by_group[g]]
    meta_names = list(by_group.get(GROUP_META, []))
    jev_names = list(by_group.get(GROUP_JEV, []))
    sets: dict[str, list[str]] = {
        VARIANT_A: a_names,
        VARIANT_B: a_names + meta_names,
        VARIANT_C: a_names + meta_names + jev_names,
    }
    if include_d_variants and jev_names:
        for group, names in JEV_QUESTION_GROUPS.items():
            dropped = set(names)
            kept = [n for n in jev_names if n not in dropped]
            if len(kept) == len(jev_names):
                continue  # ce groupe n'est pas présent dans ce jeu de données
            sets[f"{VARIANT_D_PREFIX}{group}"] = a_names + meta_names + kept
    return sets


@dataclass(frozen=True, slots=True)
class AblationSpec:
    """Réglages COMMUNS à toutes les variantes : c'est ce partage qui rend la comparaison licite."""

    training: TrainingSpec
    # ``SignalPolicy`` est immuable (dataclass gelée) : la partager entre instances est sans risque, et
    # c'est même voulu — toutes les variantes doivent appliquer la MÊME politique de signal.
    policy: SignalPolicy = field(default_factory=SignalPolicy)
    cost_multiplier: float = 1.0
    fee_rate: float | None = None
    include_d_variants: bool = True
    evaluate_on: Literal["oof", "final_test"] = "oof"

    def to_dict(self) -> dict[str, Any]:
        return {
            "training": self.training.to_dict(),
            "signal_policy": {
                "threshold_multiple": self.policy.threshold_multiple,
                "max_weight": self.policy.max_weight,
                "proportional_scale": self.policy.proportional_scale,
            },
            "cost_multiplier": self.cost_multiplier,
            "fee_rate": self.fee_rate,
            "include_d_variants": self.include_d_variants,
            "evaluate_on": self.evaluate_on,
            "equal_budget_trials": self.training.max_trials,
        }


@dataclass
class VariantOutcome:
    """Une variante mesurée : ses features, ses métriques, sa période et son statut d'indépendance."""

    variant: str
    feature_names: list[str]
    metrics: dict[str, Any]
    period: Period
    independent: bool
    report_id: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "feature_count": len(self.feature_names),
            "period": self.period.to_dict(),
            "independent": self.independent,
            "report_id": self.report_id,
            "metrics": self.metrics,
            "notes": list(self.notes),
        }


@dataclass
class AblationResult:
    """Résultat complet de l'ablation, dans le vocabulaire que lit déjà l'interface (``variant``, ``net_pnl``)."""

    spec: dict[str, Any]
    variants: list[VariantOutcome]
    comparisons: dict[str, Any]
    synthetic: bool
    run_id: str | None = None
    notes: list[str] = field(default_factory=list)

    def rows(self) -> list[dict[str, Any]]:
        """Lignes du panneau A/B : même forme que ``jev_variant_rows`` de l'API de lecture."""
        return [
            {
                "variant": v.variant,
                "net_pnl": v.metrics.get("net_pnl"),
                "independent": v.independent,
                "period_start": v.period.start.isoformat(),
                "period_end": v.period.end.isoformat(),
                "report_id": v.report_id,
                "metrics": v.metrics,
            }
            for v in self.variants
        ]

    def by_variant(self) -> dict[str, VariantOutcome]:
        return {v.variant: v for v in self.variants}

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "variants": self.rows(),
            "comparisons": self.comparisons,
            "synthetic": self.synthetic,
            "notice": SYNTHETIC_NOTICE if self.synthetic else None,
            "notes": list(self.notes),
        }


def _variant_metrics(
    variant: str,
    result: TrainingResult,
    oof: pl.DataFrame,
    dataset: ResearchDataset,
    spec: AblationSpec,
    feature_names: Sequence[str],
) -> dict[str, Any]:
    pnl = evaluate_oof(
        oof,
        cutoff_interval_s=dataset.cutoff_interval_s,
        policy=spec.policy,
        cost_multiplier=spec.cost_multiplier,
        fee_rate=spec.fee_rate,
    )
    jev_used = [n for n in feature_names if n in set(JEV_NAMES)]
    metrics: dict[str, Any] = {
        # `variant` et `net_pnl` sont le contrat minimal lu par l'API (`jev_variant_rows`).
        "variant": variant,
        "net_pnl": pnl.metrics["net_pnl"],
        "net_pnl_unit": "fraction de notionnel cumulée par intervalle de décision",
        "gross_pnl": pnl.metrics["gross_pnl"],
        "total_costs": pnl.metrics["total_costs"],
        "cost_breakdown": pnl.metrics["cost_breakdown"],
        "cost_multiplier": spec.cost_multiplier,
        "max_drawdown": pnl.metrics["max_drawdown"],
        "expected_shortfall_5": pnl.metrics["expected_shortfall_5"],
        "turnover_per_interval": pnl.metrics["turnover_per_interval"],
        "profit_factor": pnl.metrics["profit_factor"],
        "fraction_time_exposed": pnl.metrics["fraction_time_exposed"],
        "sharpe_daily_365": pnl.metrics["sharpe_daily_365"],
        "n_effective": pnl.metrics["n_effective"],
        "rows": pnl.metrics["rows"],
        "ic": result.oof_metrics.get("ic"),
        "feature_count": len(feature_names),
        "jev_feature_count": len(jev_used),
        "uses_jev_semantics": bool(jev_used),
        "trials": len(result.trials),
        "evaluated_on": spec.evaluate_on,
        "dataset_hash": dataset.dataset_hash,
        "pnl_notes": list(pnl.notes),
    }
    return metrics


def _oof_period(frame: pl.DataFrame) -> Period:
    times = [ensure_utc(t) for t in frame["decision_at"].to_list()]
    return Period(min(times), max(times) + timedelta(microseconds=1))


def _delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    if not (math.isfinite(left) and math.isfinite(right)):
        return None
    return left - right


def _comparisons(by_variant: Mapping[str, VariantOutcome], *, synthetic: bool) -> dict[str, Any]:
    """Écarts NETS entre variantes et conclusion explicitement prudente (§40)."""

    def net(name: str) -> float | None:
        outcome = by_variant.get(name)
        if outcome is None:
            return None
        value = outcome.metrics.get("net_pnl")
        return float(value) if value is not None else None

    net_a, net_b, net_c = net(VARIANT_A), net(VARIANT_B), net(VARIANT_C)
    semantic_gain = _delta(net_c, net_b)
    presence_gain = _delta(net_b, net_a)
    independent_c = bool(by_variant[VARIANT_C].independent) if VARIANT_C in by_variant else False
    conclusion: bool | None
    if semantic_gain is None or not independent_c:
        # Sans période indépendante, la question n'est pas tranchable : on le dit, on ne conclut pas.
        conclusion = None
    else:
        conclusion = semantic_gain > 0.0
    groups: dict[str, Any] = {}
    for name, outcome in by_variant.items():
        if name.startswith(VARIANT_D_PREFIX):
            groups[name[len(VARIANT_D_PREFIX) :]] = {
                "net_pnl": outcome.metrics.get("net_pnl"),
                "marginal_contribution_vs_C": _delta(net_c, net(name)),
                "independent": outcome.independent,
            }
    basis = (
        "écart net C−B mesuré sur période indépendante"
        if conclusion is not None
        else "non tranchable : aucune période indépendante, ou écart non calculable"
    )
    if synthetic:
        basis += (
            " — DONNÉES SYNTHÉTIQUES : ce chiffre ne dit rien d'un avantage de marché, il vérifie "
            "seulement que le protocole d'ablation fonctionne"
        )
    return {
        "net_pnl_A": net_a,
        "net_pnl_B": net_b,
        "net_pnl_C": net_c,
        "presence_gain_B_minus_A": presence_gain,
        "semantic_gain_C_minus_B": semantic_gain,
        "jev_adds_net_utility": conclusion,
        "conclusion_basis": basis,
        "synthetic": synthetic,
        "per_question_group": groups,
        "policy": (
            "si JEV n'améliore pas significativement l'utilité nette, le laisser en shadow ou désactiver "
            "son influence ; ne jamais forcer un poids non nul (§40)"
        ),
    }


def run_ablation(
    dataset: ResearchDataset,
    spec: AblationSpec,
    *,
    now: datetime | None = None,
    registry: ExperimentRegistry | None = None,
    run_id: str | None = None,
    model_id: str | None = None,
) -> AblationResult:
    """Exécute l'ablation complète et, si un registre est fourni, écrit un ``EvaluationReport`` par variante."""
    assert_jev_groups_cover_registry()
    moment = ensure_utc(now) if now is not None else datetime.now(tz=UTC)
    if (registry is None) != (run_id is None):
        raise ProtocolViolationError(
            "registre et run_id vont ensemble : un rapport sans expérience n'existe pas"
        )
    sets = variant_feature_sets(dataset, include_d_variants=spec.include_d_variants)
    notes: list[str] = []
    if dataset.synthetic:
        notes.append(SYNTHETIC_NOTICE)
    jev_names = set(JEV_NAMES)
    if not any(n in jev_names for n in sets[VARIANT_C]):
        notes.append("aucune feature JEV dans ce jeu de données : la variante C est identique à B")
    final_period: Period | None = None
    if spec.evaluate_on == "final_test":
        if spec.training.final_test_start is None:
            raise ProtocolViolationError(
                "évaluation sur test final demandée sans période finale gelée déclarée dans la spécification"
            )
        final_period = Period(
            ensure_utc(spec.training.final_test_start),
            dataset.period_end + timedelta(microseconds=1),
        )
    # ORDRE IMPOSÉ : on entraîne d'abord (c'est l'optimisation, elle n'a droit qu'aux données antérieures
    # à la période gelée), on journalise les essais, ET SEULEMENT ENSUITE on regarde la période finale.
    # L'inverse rendrait impossible l'enregistrement des essais, puisque consulter le test final ferme
    # définitivement l'expérience à toute optimisation (T22).
    trained: list[tuple[str, list[str], TrainingResult]] = []
    for variant, names in sets.items():
        if not names:
            notes.append(f"variante {variant} ignorée : aucune feature")
            continue
        result = walk_forward_train(dataset, spec.training, feature_names=names, producer=variant)
        trained.append((variant, list(names), result))
        if registry is not None and run_id is not None:
            registry.record_trials(
                run_id,
                [{**t.to_dict(), "variant": variant} for t in result.trials],
                budget_scope=("variant", "fold_id"),
            )
    independent = False
    if registry is not None and run_id is not None and final_period is not None:
        # Une passe d'ablation = UN regard sur la période, pas un regard par variante.
        independent = registry.period_is_independent(run_id, final_period)
        consultation = registry.consult_final_test(
            run_id, final_period, now=moment, purpose=f"ablation JEV ({spec.evaluate_on})"
        )
        independent = independent and consultation.first_consultation
        if not independent:
            notes.append(
                "période finale déjà consultée : ces variantes ne sont pas des preuves indépendantes (T22)"
            )
    outcomes: list[VariantOutcome] = []
    for variant, names, result in trained:
        if final_period is not None:
            oof = predict_holdout(
                result, dataset, spec.training, final_period, feature_names=names, producer=variant
            )
        else:
            oof = result.oof
        metrics = _variant_metrics(variant, result, oof, dataset, spec, names)
        variant_notes = list(result.notes)
        if dataset.synthetic:
            metrics["synthetic"] = True
            metrics["notice"] = SYNTHETIC_NOTICE
            metrics["warning"] = (
                "résultat obtenu sur données synthétiques : aucune preuve d'avantage de marché"
            )
            if metrics["uses_jev_semantics"]:
                metrics["jev_warning"] = SYNTHETIC_JEV_WARNING
                variant_notes.append(SYNTHETIC_JEV_WARNING)
        metrics["independent"] = independent
        if not independent:
            metrics.setdefault(
                "independence_note",
                "mesure hors échantillon mais NON indépendante : la sélection a eu lieu sur ces folds",
            )
        outcomes.append(
            VariantOutcome(
                variant=variant,
                feature_names=list(names),
                metrics=metrics,
                period=_oof_period(oof),
                independent=independent,
                notes=variant_notes,
            )
        )
    if VARIANT_A not in {o.variant for o in outcomes} or VARIANT_C not in {o.variant for o in outcomes}:
        raise ProtocolViolationError(
            "ablation incomplète : les variantes A et C sont obligatoires",
            measured=[o.variant for o in outcomes],
        )
    if registry is not None and run_id is not None:
        for outcome in outcomes:
            outcome.report_id = registry.write_report(
                run_id,
                kind=KIND_JEV_ABLATION,
                period=outcome.period,
                metrics=outcome.metrics,
                now=moment,
                model_id=model_id,
                independent=outcome.independent,
                synthetic=dataset.synthetic,
            )
    result_obj = AblationResult(
        spec=spec.to_dict(),
        variants=outcomes,
        comparisons=_comparisons({o.variant: o for o in outcomes}, synthetic=dataset.synthetic),
        synthetic=dataset.synthetic,
        run_id=run_id,
        notes=notes,
    )
    return result_obj
