"""Features d'événements (§40, §50.4).

Variante B — métadonnées seules : présence, nombre et âge des annonces (aucun texte, aucune sémantique).
Variante C — évaluations JEV : à partir d'une ``Sequence[JevEvaluation]`` dont ``features_committed_at``
est antérieur ou égal à la coupure et dont le statut est ``ok``.

Absence d'évaluation ou de flux : chaque feature est MISSING avec sa raison. Aucune valeur n'est
remplacée par zéro : un modèle voit l'absence par le masque, jamais par un « 0 » ambigu.

Espérances de score : ``E[s] = Σ k·p_k`` sur ``score_distribution`` (clés « 0 », « 1 », ...) ; à défaut
le score ponctuel ; à défaut MISSING. Catégories : ``EVENT_CATEGORIES`` = liste du jeu de questions v1
(vérifiée par test contre ``configs/jev_questions.v1.json``).
"""

from __future__ import annotations

from okxq.domain.events import JevAnswer, JevEvaluation, JevStatus
from okxq.features.definitions import (
    FeatureDefinition,
    FeatureResult,
    MissingPolicy,
    TemporalSemantics,
    invalid,
    missing,
    ok,
    stale,
)
from okxq.features.state import PointInTimeMarketState

GROUP_META = "events_meta"
GROUP_JEV = "events_jev"
VERSION = "events-v1"
EVENT_CATEGORIES: tuple[str, ...] = (
    "security_incident",
    "listing",
    "delisting",
    "maintenance",
    "tokenomics",
    "regulatory",
    "partnership",
    "technical",
    "other",
    "none",
)
ANNOUNCEMENT_WINDOWS_S = (3600, 86400)
JEV_MAX_AGE_S = 3600
SCORE_QUESTIONS = {
    "jev_severity_expected": "reported_severity",
    "jev_impact_expected": "reported_operational_impact",
    "jev_novelty_expected": "novelty_vs_prior",
}
NOUL_QUESTIONS = {"jev_relevance": "asset_relevance", "jev_source_confirms": "source_confirms"}


def _d(name: str, group: str, lookback_s: int, unit: str, description: str) -> FeatureDefinition:
    return FeatureDefinition(
        name=name,
        version=VERSION,
        source="announcements" if group == GROUP_META else "jev_evaluations",
        temporal_semantics=TemporalSemantics.EVENT,
        lookback_s=lookback_s,
        normalization="none",
        unit=unit,
        missing_policy=MissingPolicy.MASK,
        group=group,
        description=description,
    )


META_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    _d("ann_present_1h", GROUP_META, 3600, "indicator", "au moins une annonce vue dans l'heure"),
    _d("ann_count_1h", GROUP_META, 3600, "count", "annonces vues dans l'heure"),
    _d("ann_count_24h", GROUP_META, 86400, "count", "annonces vues sur 24 h"),
    _d("ann_age_s", GROUP_META, 86400, "seconds", "âge de la dernière annonce vue"),
)


def _jev_definitions() -> tuple[FeatureDefinition, ...]:
    defs = [
        _d("jev_available", GROUP_JEV, JEV_MAX_AGE_S, "indicator", "évaluation JEV engagée avant la coupure"),
        _d("jev_age_s", GROUP_JEV, JEV_MAX_AGE_S, "seconds", "âge de l'évaluation (features_committed_at)"),
        _d("jev_confidence", GROUP_JEV, JEV_MAX_AGE_S, "probability", "confiance déclarée sur event_kind"),
    ]
    defs.extend(
        _d(f"jev_p_{c}", GROUP_JEV, JEV_MAX_AGE_S, "probability", f"P(event_kind={c})")
        for c in EVENT_CATEGORIES
    )
    defs.extend(
        [
            _d("jev_relevance", GROUP_JEV, JEV_MAX_AGE_S, "probability", "P(document concerne l'actif)"),
            _d(
                "jev_severity_expected",
                GROUP_JEV,
                JEV_MAX_AGE_S,
                "score",
                "espérance de la sévérité rapportée",
            ),
            _d(
                "jev_impact_expected", GROUP_JEV, JEV_MAX_AGE_S, "score", "espérance de l'impact opérationnel"
            ),
            _d("jev_novelty_expected", GROUP_JEV, JEV_MAX_AGE_S, "score", "espérance de la nouveauté"),
            _d("jev_source_confirms", GROUP_JEV, JEV_MAX_AGE_S, "probability", "P(source confirme)"),
        ]
    )
    return tuple(defs)


JEV_DEFINITIONS: tuple[FeatureDefinition, ...] = _jev_definitions()
DEFINITIONS: tuple[FeatureDefinition, ...] = META_DEFINITIONS + JEV_DEFINITIONS
META_NAMES = [d.name for d in META_DEFINITIONS]
JEV_NAMES = [d.name for d in JEV_DEFINITIONS]


def compute_metadata(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    if not state.announcement_feed_available:
        return {n: missing("announcement_feed_unavailable") for n in META_NAMES}
    cutoff_s = state.cutoff_s
    ages = [cutoff_s - a.first_seen_at.timestamp() for a in state.announcements]
    ages = [a for a in ages if a >= 0]
    in_1h = [a for a in ages if a <= 3600]
    in_24h = [a for a in ages if a <= 86400]
    out: dict[str, FeatureResult] = {
        "ann_present_1h": ok(1.0 if in_1h else 0.0),
        "ann_count_1h": ok(float(len(in_1h))),
        "ann_count_24h": ok(float(len(in_24h))),
        "ann_age_s": ok(min(in_24h)) if in_24h else missing("no_announcement_24h"),
    }
    return out


def expected_score(answer: JevAnswer) -> float | None:
    if answer.score_distribution:
        total = sum(answer.score_distribution.values())
        if total <= 0:
            return None
        return sum(int(k) * p for k, p in answer.score_distribution.items()) / total
    if answer.score is not None:
        return float(answer.score)
    return None


def select_evaluation(state: PointInTimeMarketState) -> tuple[JevEvaluation | None, str | None]:
    usable = [
        e
        for e in state.jev_evaluations
        if e.status is JevStatus.OK
        and e.features_committed_at is not None
        and e.features_committed_at <= state.cutoff_at
    ]
    if not usable:
        return None, "no_jev_evaluation_committed_before_cutoff"
    best = max(usable, key=lambda e: e.features_committed_at or state.cutoff_at)
    assert best.features_committed_at is not None
    age = state.cutoff_s - best.features_committed_at.timestamp()
    if age > JEV_MAX_AGE_S:
        return None, f"jev_evaluation_stale_{age:.0f}s"
    return best, None


def compute_jev(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    evaluation, reason = select_evaluation(state)
    if evaluation is None:
        assert reason is not None
        flag = stale if reason.startswith("jev_evaluation_stale") else missing
        return {n: flag(reason) for n in JEV_NAMES}
    assert evaluation.features_committed_at is not None
    out: dict[str, FeatureResult] = {
        "jev_available": ok(1.0),
        "jev_age_s": ok(state.cutoff_s - evaluation.features_committed_at.timestamp()),
    }
    kind = evaluation.answers.get("event_kind")
    if kind is None:
        out["jev_confidence"] = missing("event_kind_absent")
        for c in EVENT_CATEGORIES:
            out[f"jev_p_{c}"] = missing("event_kind_absent")
    else:
        out["jev_confidence"] = (
            ok(kind.confidence) if kind.confidence is not None else missing("confidence_absent")
        )
        if kind.probabilities:
            total = sum(kind.probabilities.values())
            if total <= 0 or abs(total - 1.0) > 0.05:
                for c in EVENT_CATEGORIES:
                    out[f"jev_p_{c}"] = invalid(f"event_kind_probabilities_sum_{total:.3f}")
            else:
                for c in EVENT_CATEGORIES:
                    out[f"jev_p_{c}"] = ok(kind.probabilities.get(c, 0.0) / total)
        elif kind.choice is not None:
            if kind.choice not in EVENT_CATEGORIES:
                for c in EVENT_CATEGORIES:
                    out[f"jev_p_{c}"] = invalid("unknown_event_kind_choice")
            else:
                for c in EVENT_CATEGORIES:
                    out[f"jev_p_{c}"] = ok(1.0 if c == kind.choice else 0.0)
        else:
            for c in EVENT_CATEGORIES:
                out[f"jev_p_{c}"] = missing("event_kind_unanswered")
    for feature, question in NOUL_QUESTIONS.items():
        ans = evaluation.answers.get(question)
        if ans is None or ans.probability is None:
            out[feature] = missing(f"{question}_absent")
        else:
            out[feature] = ok(ans.probability)
    for feature, question in SCORE_QUESTIONS.items():
        ans = evaluation.answers.get(question)
        val = expected_score(ans) if ans is not None else None
        out[feature] = ok(val) if val is not None else missing(f"{question}_absent")
    return out


def compute(state: PointInTimeMarketState) -> dict[str, FeatureResult]:
    out = compute_metadata(state)
    out.update(compute_jev(state))
    return out
