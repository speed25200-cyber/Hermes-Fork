"""Corpus sémantique et évaluateur FACTICE (§49, §68).

``tests/fixtures/jev/corpus.jsonl`` contient des cas étiquetés (ambigus, multilingues FR/EN/ZH, contradictoires,
répétés, adversariaux/injection, sans date, collision de ticker) avec les réponses attendues.

``FixtureJevEvaluator`` est un ÉVALUATEUR FACTICE : il rend les réponses du corpus, sans aucun appel au
fournisseur. Il est étiqueté ``model_effective="fixture:corpus"`` et ``usage={"fixture": True,
"real_call": "NOT_RUN"}`` ; il ne doit jamais être présenté comme un appel réel.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from okxq.domain.clocks import Clock, SystemClock
from okxq.domain.errors import JevError
from okxq.domain.events import AssetMapping, JevAnswer, JevAnswerType, JevEvaluation, SourceDocument
from okxq.domain.events import JevStatus as EvaluationStatus
from okxq.domain.ids import new_id
from okxq.jev.entity_mapping import MappingQuality
from okxq.jev.schemas import QuestionSet, parse_json_strict
from okxq.jev.source_connectors import DATE_METHOD_ABSENT, sha256_text

CORPUS_MIN_CASES = 25
FIXTURE_MODEL_LABEL = "fixture:corpus"
FIXTURE_CONFIDENCE = 0.90
FIXTURE_MAPPING_VERSION = "corpus-fixture"
NOUL_DECISION_THRESHOLD = 0.5
CORPUS_TAGS = frozenset(
    {
        "baseline",
        "ambiguous",
        "multilingual",
        "contradictory",
        "repeated",
        "adversarial",
        "injection",
        "undated",
        "ticker_collision",
        "multi_asset",
        "rename",
        "irrelevant",
    }
)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CorpusDocument(_Strict):
    title: str = Field(max_length=512)
    text: str = Field(min_length=1)
    published_at: AwareDatetime | None = None


class CorpusAsset(_Strict):
    canonical_name: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    inst_id: str = Field(min_length=1)


class CorpusExpected(_Strict):
    event_kind: str
    asset_relevance: bool
    reported_severity: int = Field(ge=0)
    novelty_vs_prior: int = Field(ge=0)
    reported_operational_impact: int = Field(ge=0)
    source_confirms: bool


class CorpusExpectedMapping(_Strict):
    quality: MappingQuality
    inst_ids: list[str] = Field(default_factory=list)


class CorpusCase(_Strict):
    case_id: str = Field(pattern=r"^c[0-9]{2,3}_[a-z0-9_]+$")
    language: str = Field(min_length=2, max_length=8)
    tags: list[str] = Field(min_length=1)
    asset: CorpusAsset
    document: CorpusDocument
    prior_documents: list[CorpusDocument] = Field(default_factory=list)
    expected: CorpusExpected
    expected_mapping: CorpusExpectedMapping
    notes: str = ""

    @property
    def text_hash(self) -> str:
        return sha256_text(self.document.title + "\n" + self.document.text)


def load_corpus(
    path: str | Path, *, question_set: QuestionSet, min_cases: int = CORPUS_MIN_CASES
) -> list[CorpusCase]:
    """Charge et valide le corpus contre le jeu de questions (catégories, niveaux, étiquettes)."""
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise JevError(f"corpus introuvable : {p}", code="CORPUS_MISSING") from exc
    cases: list[CorpusCase] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            cases.append(CorpusCase.model_validate(parse_json_strict(line)))
        except (ValidationError, JevError) as exc:
            raise JevError(
                f"corpus ligne {number} invalide : {exc}", code="CORPUS_INVALID", line=number
            ) from exc
    ids = [c.case_id for c in cases]
    if len(set(ids)) != len(ids):
        raise JevError("identifiants de cas dupliqués", code="CORPUS_INVALID")
    if len(cases) < min_cases:
        raise JevError(f"corpus trop petit : {len(cases)} < {min_cases}", code="CORPUS_INVALID")
    categories = set(question_set.event_categories)
    for case in cases:
        unknown_tags = set(case.tags) - CORPUS_TAGS
        if unknown_tags:
            raise JevError(
                f"{case.case_id} : étiquettes inconnues {sorted(unknown_tags)}", code="CORPUS_INVALID"
            )
        if case.expected.event_kind not in categories:
            raise JevError(
                f"{case.case_id} : event_kind inconnu {case.expected.event_kind}", code="CORPUS_INVALID"
            )
        for qid in ("reported_severity", "novelty_vs_prior", "reported_operational_impact"):
            levels = question_set.questions[qid].level_count
            value = getattr(case.expected, qid)
            if not 0 <= value < levels:
                raise JevError(
                    f"{case.case_id} : {qid}={value} hors des {levels} niveaux", code="CORPUS_INVALID"
                )
        if case.expected_mapping.quality is MappingQuality.OK and not case.expected_mapping.inst_ids:
            raise JevError(f"{case.case_id} : mapping ok sans inst_ids", code="CORPUS_INVALID")
    return cases


def expected_answers(case: CorpusCase, question_set: QuestionSet) -> dict[str, JevAnswer]:
    """Réponses attendues sous forme de ``JevAnswer`` déterministes (distributions concentrées, FACTICES)."""
    answers: dict[str, JevAnswer] = {}
    for qid, spec in question_set.questions.items():
        if spec.type == "choice":
            expected = str(getattr(case.expected, qid))
            n_other = len(spec.categories) - 1
            answers[qid] = JevAnswer(
                type=JevAnswerType.CHOICE,
                choice=expected,
                probabilities={c: (0.91 if c == expected else 0.09 / n_other) for c in spec.categories},
                confidence=FIXTURE_CONFIDENCE,
            )
        elif spec.type == "noul":
            flag = bool(getattr(case.expected, qid))
            answers[qid] = JevAnswer(
                type=JevAnswerType.NOUL, probability=0.9 if flag else 0.1, confidence=FIXTURE_CONFIDENCE
            )
        else:
            level = int(getattr(case.expected, qid))
            n_other = spec.level_count - 1
            answers[qid] = JevAnswer(
                type=JevAnswerType.SCORE,
                score=level,
                score_distribution={
                    str(i): (0.91 if i == level else 0.09 / n_other) for i in range(spec.level_count)
                },
                confidence=FIXTURE_CONFIDENCE,
            )
    return answers


def document_from_case(
    case: CorpusCase,
    *,
    received_at: datetime,
    version: int = 1,
    with_mapping: bool = True,
) -> SourceDocument:
    """``SourceDocument`` d'un cas de corpus. La date vient du cas (déclarée) ou est absente : jamais l'heure de collecte."""
    mapping = (
        [
            AssetMapping(
                inst_id=case.asset.inst_id,
                canonical_name=case.asset.canonical_name,
                symbol=case.asset.symbol,
                mapping_version=FIXTURE_MAPPING_VERSION,
                confidence=1.0,
                method="corpus_fixture",
            )
        ]
        if with_mapping and case.expected_mapping.quality is MappingQuality.OK
        else []
    )
    return SourceDocument(
        document_id=f"doc_{case.case_id}",
        source="corpus_fixture",
        source_url=f"https://fixture.invalid/corpus/{case.case_id}",
        published_at=case.document.published_at,
        first_seen_at=received_at,
        received_at=received_at,
        parsed_at=received_at,
        language=case.language,
        version=version,
        date_method="declared_field" if case.document.published_at is not None else DATE_METHOD_ABSENT,
        raw_text_hash=case.text_hash,
        deduplication_id=case.case_id,
        asset_mapping=mapping,
        mapping_quality=case.expected_mapping.quality.value,
        title=case.document.title,
        text=case.document.text,
    )


class FixtureJevEvaluator:
    """ÉVALUATEUR FACTICE (implémente ``JevEventEvaluator``) : rend les réponses du corpus, jamais un appel réel."""

    is_fixture = True

    def __init__(
        self, corpus: list[CorpusCase], *, question_set: QuestionSet, clock: Clock | None = None
    ) -> None:
        self._by_hash = {case.text_hash: case for case in corpus}
        self._questions = question_set
        self._clock: Clock = clock or SystemClock()
        self.calls = 0

    async def evaluate(self, document: SourceDocument) -> JevEvaluation:
        self.calls += 1
        now = self._clock.now_utc()
        case = self._by_hash.get(sha256_text(document.title + "\n" + document.text))
        inst_id = document.asset_mapping[0].inst_id if document.asset_mapping else None
        common = {
            "document_id": document.document_id,
            "document_version": document.version,
            "question_set_hash": self._questions.question_set_hash,
            "asset_mapping_version": f"{FIXTURE_MAPPING_VERSION}#{inst_id}" if inst_id else "none",
            "model_requested": self._questions.model,
            "requested_at": now,
            "inst_id": inst_id,
        }
        if case is None:
            return JevEvaluation(
                evaluation_id=new_id("jevfx"),
                model_effective=FIXTURE_MODEL_LABEL,
                completed_at=now,
                status=EvaluationStatus.REJECTED,
                error="FIXTURE_CASE_UNKNOWN: document absent du corpus (évaluateur factice)",
                usage={"fixture": True, "real_call": "NOT_RUN"},
                **common,
            )
        return JevEvaluation(
            evaluation_id=new_id("jevfx"),
            model_effective=FIXTURE_MODEL_LABEL,
            completed_at=now,
            inference_completed_at=now,
            features_committed_at=now,
            answers=expected_answers(case, self._questions),
            usage={"fixture": True, "real_call": "NOT_RUN", "case_id": case.case_id},
            status=EvaluationStatus.OK,
            **common,
        )


@dataclass(frozen=True, slots=True)
class QuestionAgreement:
    question_id: str
    n: int
    agreed: int
    within_one: int | None = None

    @property
    def agreement(self) -> float | None:
        return self.agreed / self.n if self.n else None

    @property
    def within_one_rate(self) -> float | None:
        return None if self.within_one is None or not self.n else self.within_one / self.n


def agreement_metrics(
    predictions: Mapping[str, Mapping[str, JevAnswer]],
    corpus: list[CorpusCase],
    question_set: QuestionSet,
) -> dict[str, QuestionAgreement]:
    """Accord par question entre des réponses (réelles ou factices) et les attentes du corpus."""
    by_id = {c.case_id: c for c in corpus}
    out: dict[str, QuestionAgreement] = {}
    for qid, spec in question_set.questions.items():
        n = agreed = within = 0
        for case_id, answers in predictions.items():
            case = by_id.get(case_id)
            answer = answers.get(qid)
            if case is None or answer is None:
                continue
            n += 1
            expected = getattr(case.expected, qid)
            if spec.type == "choice":
                agreed += int(answer.choice == expected)
            elif spec.type == "noul":
                predicted = (answer.probability or 0.0) >= NOUL_DECISION_THRESHOLD
                agreed += int(predicted == bool(expected))
            else:
                agreed += int(answer.score == expected)
                within += int(answer.score is not None and abs(answer.score - int(expected)) <= 1)
        out[qid] = QuestionAgreement(qid, n, agreed, within if spec.type == "score" else None)
    return out
