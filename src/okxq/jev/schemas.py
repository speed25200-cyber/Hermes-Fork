"""Schémas de la requête et de la réponse TypeSafe (endpoint ``systemone``, modèle ``jev-1.13.0``).

Contrat consulté dans le cahier des charges (URL de référence : https://docs.typesafe.ai/api, à
revalider — voir ``docs/api_contracts/typesafe_jev.md``, date de référence 2026-09-18).

Règles (§49, T52, T53) :
- la requête est STRICTE : ``state`` n'accepte que ``asset``, ``document`` et ``prior_documents`` ; tout
  autre champ (clé, position, patrimoine, identité) est refusé à la construction ;
- la réponse tolère un champ INCONNU ajouté mais jamais un champ obligatoire absent ;
- un nombre est un ``int``/``float`` fini : NaN, infini et booléen sont refusés ;
- une catégorie inconnue, une version de modèle inattendue et une somme de probabilités incohérente
  (tolérance explicite ``PROBABILITY_SUM_TOLERANCE``) sont refusées ;
- une distribution absente n'est JAMAIS reconstruite à partir d'un score seul.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from okxq.domain.errors import JevContractError, JevError
from okxq.domain.events import JevAnswer, JevAnswerType
from okxq.domain.ids import payload_hash

PROBABILITY_SUM_TOLERANCE = 1e-3
"""Écart absolu toléré entre la somme d'une distribution et 1 (arrondis du fournisseur)."""

DEFAULT_MODEL = "jev-1.13.0"
MAX_QUESTION_ID_LENGTH = 64
MAX_PRIOR_DOCUMENTS = 20
MAX_TITLE_CHARACTERS = 512

QuestionType = Literal["choice", "noul", "score"]


# --- validateurs de nombres --------------------------------------------------------------------------


def strict_number(value: object) -> float:
    """Nombre fini : refuse booléen, NaN, infini, chaîne et tout autre type."""
    if isinstance(value, bool):
        raise ValueError("booléen reçu pour un nombre")
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nombre non fini (NaN/infini)")
        return value
    raise ValueError(f"nombre attendu, reçu {type(value).__name__}")


def strict_int(value: object) -> int:
    """Entier strict : refuse booléen et flottant (même intégral)."""
    if isinstance(value, bool):
        raise ValueError("booléen reçu pour un entier")
    if isinstance(value, int):
        return value
    raise ValueError(f"entier attendu, reçu {type(value).__name__}")


UnitInterval = Annotated[float, BeforeValidator(strict_number), Field(ge=0.0, le=1.0)]
StrictInt = Annotated[int, BeforeValidator(strict_int)]
NonNegativeNumber = Annotated[float, BeforeValidator(strict_number), Field(ge=0.0)]
NonNegativeInt = Annotated[int, BeforeValidator(strict_int), Field(ge=0)]


def _reject_json_constant(name: str) -> Any:
    raise JevContractError(f"constante JSON non finie refusée : {name}", constant=name)


def parse_json_strict(payload: bytes | str) -> Any:
    """``json.loads`` qui refuse ``NaN``/``Infinity`` (acceptés par défaut par Python)."""
    try:
        return json.loads(payload, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise JevContractError(f"réponse JEV non JSON : {exc.msg}", position=exc.pos) from exc


# --- requête (stricte) --------------------------------------------------------------------------------


class StrictJevModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)


class JevAssetState(StrictJevModel):
    canonical_name: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)


class JevDocumentState(StrictJevModel):
    title: str = Field(max_length=MAX_TITLE_CHARACTERS)
    text: str = Field(min_length=1)


class JevPriorDocument(StrictJevModel):
    title: str = Field(max_length=MAX_TITLE_CHARACTERS)
    text: str = Field(min_length=1)


class JevState(StrictJevModel):
    """Seuls trois champs existent. Aucun credential, aucune position, aucune identité ne peut y entrer."""

    asset: JevAssetState
    document: JevDocumentState
    prior_documents: list[JevPriorDocument] = Field(default_factory=list, max_length=MAX_PRIOR_DOCUMENTS)


class JevQuestionSpec(StrictJevModel):
    type: QuestionType
    instructions: str = Field(min_length=1, max_length=4000)
    criteria: dict[str, str] | list[str] | None = None

    @model_validator(mode="after")
    def _criteria_shape(self) -> JevQuestionSpec:
        if self.type == "choice":
            if not isinstance(self.criteria, dict) or len(self.criteria) < 2:
                raise ValueError("une question choice exige un dict de critères (≥ 2 catégories)")
            for key, text in self.criteria.items():
                if not key or not text:
                    raise ValueError("catégorie ou critère vide")
        elif self.type == "score":
            if not isinstance(self.criteria, list) or len(self.criteria) < 2:
                raise ValueError("une question score exige une liste de niveaux (≥ 2)")
            if any(not level for level in self.criteria):
                raise ValueError("niveau de score vide")
        elif self.criteria is not None:
            raise ValueError("une question noul ne porte pas de critères")
        return self

    @property
    def categories(self) -> list[str]:
        return list(self.criteria.keys()) if isinstance(self.criteria, dict) else []

    @property
    def level_count(self) -> int:
        return len(self.criteria) if isinstance(self.criteria, list) else 0


class JevRequest(StrictJevModel):
    model: str = Field(min_length=1, max_length=64)
    state: JevState
    questions: dict[str, JevQuestionSpec] = Field(min_length=1)

    @field_validator("questions")
    @classmethod
    def _question_ids(cls, v: dict[str, JevQuestionSpec]) -> dict[str, JevQuestionSpec]:
        for qid in v:
            if not qid or len(qid) > MAX_QUESTION_ID_LENGTH or not qid.replace("_", "").isalnum():
                raise ValueError(f"identifiant de question invalide : {qid!r}")
        return v

    def payload(self) -> dict[str, Any]:
        """Corps JSON exact envoyé au fournisseur."""
        return self.model_dump(mode="json", exclude_none=True)

    def payload_hash(self) -> str:
        return payload_hash(self.payload())


# --- jeu de questions ----------------------------------------------------------------------------------


class QuestionSet(StrictJevModel):
    """``configs/jev_questions.v1.json`` validé ; ``question_set_hash`` = hash canonique des questions."""

    question_set_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    language: str = Field(min_length=2, max_length=8)
    notes: str = ""
    event_categories: list[str] = Field(min_length=1)
    questions: dict[str, JevQuestionSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _categories_consistent(self) -> QuestionSet:
        kind = self.questions.get("event_kind")
        if kind is not None and kind.type == "choice" and kind.categories != self.event_categories:
            raise ValueError("event_kind.criteria doit énumérer exactement event_categories, dans l'ordre")
        return self

    @property
    def question_set_hash(self) -> str:
        return payload_hash(
            {qid: q.model_dump(mode="json", exclude_none=True) for qid, q in self.questions.items()}
        )


def load_question_set(path: str | Path) -> QuestionSet:
    p = Path(path)
    try:
        raw = parse_json_strict(p.read_bytes())
        return QuestionSet.model_validate(raw)
    except FileNotFoundError as exc:
        raise JevError(f"jeu de questions introuvable : {p}", path=str(p)) from exc
    except ValidationError as exc:
        raise JevError(f"jeu de questions invalide ({p}) : {exc}", path=str(p)) from exc


# --- construction de requête ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestBuild:
    request: JevRequest
    truncated: bool
    document_characters: int


def truncate_text(text: str, max_characters: int) -> tuple[str, bool]:
    """Coupe un texte trop long (état trop gros = 400 non rejouable, §49). Marque la coupure."""
    if max_characters <= 0:
        raise JevError("max_characters doit être positif", max_characters=max_characters)
    if len(text) <= max_characters:
        return text, False
    marker = " […]"
    if max_characters <= len(marker):
        # Budget trop petit pour porter la marque : on coupe NET, sans elle. L'ancienne écriture
        # gardait au moins un caractère puis ajoutait la marque quand même, si bien qu'un budget de
        # 1 rendait 5 caractères : une fonction nommée « tronquer à N » pouvait dépasser N. Le
        # dépassement était de quatre caractères, donc inoffensif aux budgets d'exploitation — mais
        # la borne est justement ce que cette fonction promet, et ce que l'appelant utilise pour ne
        # pas se faire refuser une requête trop grosse (§49).
        # La coupure reste SIGNALÉE par le drapeau, qui est ce que l'appelant enregistre ; la marque
        # n'est qu'un indice de lecture.
        return text[:max_characters], True
    return text[: max_characters - len(marker)] + marker, True


def build_request(
    *,
    model: str,
    question_set: QuestionSet,
    canonical_name: str,
    symbol: str,
    title: str,
    text: str,
    prior_documents: list[tuple[str, str]] | None = None,
    max_document_characters: int,
) -> RequestBuild:
    """Assemble une requête stricte ; les documents antérieurs sont bornés au même budget de caractères."""
    body, truncated = truncate_text(text, max_document_characters)
    priors: list[JevPriorDocument] = []
    for prior_title, prior_text in prior_documents or []:
        prior_body, _ = truncate_text(prior_text, max_document_characters)
        priors.append(JevPriorDocument(title=prior_title[:MAX_TITLE_CHARACTERS], text=prior_body))
    if len(priors) > MAX_PRIOR_DOCUMENTS:
        priors = priors[:MAX_PRIOR_DOCUMENTS]
    request = JevRequest(
        model=model,
        state=JevState(
            asset=JevAssetState(canonical_name=canonical_name, symbol=symbol),
            document=JevDocumentState(title=title[:MAX_TITLE_CHARACTERS], text=body),
            prior_documents=priors,
        ),
        questions=dict(question_set.questions),
    )
    return RequestBuild(request=request, truncated=truncated, document_characters=len(body))


# --- réponse (tolérante aux champs inconnus, stricte sur les obligatoires) ---------------------------


class TolerantJevModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow", populate_by_name=True)


class JevChoiceAnswer(TolerantJevModel):
    choice: str = Field(min_length=1)
    probabilities: dict[str, UnitInterval]


class JevNoulAnswer(TolerantJevModel):
    probability: UnitInterval


class JevScoreAnswer(TolerantJevModel):
    score: StrictInt
    # Le nom exact du champ de distribution peut différer selon le fournisseur ; on accepte les deux noms
    # documentés. Un score sans distribution est REFUSÉ (jamais reconstruit, §49).
    probabilities: list[UnitInterval] | dict[str, UnitInterval] = Field(
        validation_alias=AliasChoices("probabilities", "distribution")
    )


class JevTypesafeMetadata(TolerantJevModel):
    confidence: dict[str, UnitInterval] = Field(default_factory=dict)


class JevProviderMetadata(TolerantJevModel):
    typesafe: JevTypesafeMetadata | None = None


class JevUsage(TolerantJevModel):
    """Usage facturé. Les noms exacts peuvent différer : plusieurs alias documentés sont acceptés."""

    input_tokens: NonNegativeInt | None = Field(
        default=None, validation_alias=AliasChoices("inputTokens", "input_tokens", "prompt_tokens")
    )
    output_tokens: NonNegativeInt | None = Field(
        default=None, validation_alias=AliasChoices("outputTokens", "output_tokens", "completion_tokens")
    )
    total_tokens: NonNegativeInt | None = Field(
        default=None, validation_alias=AliasChoices("totalTokens", "total_tokens")
    )
    cost_usd: NonNegativeNumber | None = Field(
        default=None, validation_alias=AliasChoices("costUsd", "cost_usd", "cost")
    )

    def raw(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=False, exclude_none=True)


class JevRawResponse(TolerantJevModel):
    model: str = Field(min_length=1)
    answers: dict[str, dict[str, Any]]
    provider_metadata: JevProviderMetadata | None = Field(default=None, alias="providerMetadata")
    usage: JevUsage


@dataclass(frozen=True, slots=True)
class ParsedResponse:
    model_effective: str
    answers: dict[str, JevAnswer]
    usage: JevUsage
    raw_usage: dict[str, Any]
    confidence: dict[str, float]
    unknown_answer_ids: tuple[str, ...]


def _check_sum(values: list[float], *, where: str, tolerance: float) -> None:
    total = math.fsum(values)
    if abs(total - 1.0) > tolerance:
        raise JevContractError(
            f"{where} : somme des probabilités {total:.6f} hors tolérance ±{tolerance}",
            where=where,
            total=total,
        )


def _contract(where: str, exc: ValidationError) -> JevContractError:
    errors = exc.errors()
    message = errors[0]["msg"] if errors else "réponse invalide"
    location = ".".join(str(x) for x in errors[0]["loc"]) if errors else ""
    return JevContractError(f"{where} : {message} (champ {location})", where=where)


def parse_answer(
    qid: str,
    spec: JevQuestionSpec,
    raw: Mapping[str, Any],
    *,
    confidence: float | None,
    tolerance: float = PROBABILITY_SUM_TOLERANCE,
) -> JevAnswer:
    """Valide une réponse selon le type de la question et la convertit en ``JevAnswer`` du domaine."""
    if not isinstance(raw, Mapping):
        raise JevContractError(f"réponse {qid} : objet attendu", where=qid)
    if spec.type == "choice":
        try:
            choice = JevChoiceAnswer.model_validate(dict(raw))
        except ValidationError as exc:
            raise _contract(f"réponse {qid} (choice)", exc) from exc
        expected = set(spec.categories)
        got = set(choice.probabilities)
        if got - expected:
            raise JevContractError(
                f"réponse {qid} : catégorie inconnue {sorted(got - expected)}",
                where=qid,
                unknown=sorted(got - expected),
            )
        if expected - got:
            raise JevContractError(
                f"réponse {qid} : catégorie manquante {sorted(expected - got)}",
                where=qid,
                missing=sorted(expected - got),
            )
        if choice.choice not in expected:
            raise JevContractError(f"réponse {qid} : choix {choice.choice!r} hors catégories", where=qid)
        _check_sum(list(choice.probabilities.values()), where=f"réponse {qid}", tolerance=tolerance)
        return JevAnswer(
            type=JevAnswerType.CHOICE,
            choice=choice.choice,
            probabilities={k: choice.probabilities[k] for k in spec.categories},
            confidence=confidence,
        )
    if spec.type == "noul":
        try:
            noul = JevNoulAnswer.model_validate(dict(raw))
        except ValidationError as exc:
            raise _contract(f"réponse {qid} (noul)", exc) from exc
        return JevAnswer(type=JevAnswerType.NOUL, probability=noul.probability, confidence=confidence)
    try:
        score = JevScoreAnswer.model_validate(dict(raw))
    except ValidationError as exc:
        raise _contract(f"réponse {qid} (score)", exc) from exc
    levels = spec.level_count
    if not 0 <= score.score < levels:
        raise JevContractError(f"réponse {qid} : score {score.score} hors des {levels} niveaux", where=qid)
    if isinstance(score.probabilities, list):
        if len(score.probabilities) != levels:
            raise JevContractError(
                f"réponse {qid} : distribution de {len(score.probabilities)} niveaux, {levels} attendus",
                where=qid,
            )
        distribution = {str(i): p for i, p in enumerate(score.probabilities)}
    else:
        keys = set(score.probabilities)
        expected_keys = {str(i) for i in range(levels)}
        if keys != expected_keys:
            raise JevContractError(
                f"réponse {qid} : niveaux de distribution {sorted(keys)} ≠ {sorted(expected_keys)}", where=qid
            )
        distribution = {str(i): score.probabilities[str(i)] for i in range(levels)}
    _check_sum(list(distribution.values()), where=f"réponse {qid}", tolerance=tolerance)
    return JevAnswer(
        type=JevAnswerType.SCORE, score=score.score, score_distribution=distribution, confidence=confidence
    )


def parse_response(
    payload: bytes | str | Mapping[str, Any],
    *,
    questions: Mapping[str, JevQuestionSpec],
    expected_model: str,
    tolerance: float = PROBABILITY_SUM_TOLERANCE,
) -> ParsedResponse:
    """Valide la réponse complète du fournisseur et la convertit en réponses du domaine (T52, T53)."""
    data = payload if isinstance(payload, Mapping) else parse_json_strict(payload)
    if not isinstance(data, Mapping):
        raise JevContractError("réponse JEV : objet JSON attendu à la racine")
    try:
        raw = JevRawResponse.model_validate(dict(data))
    except ValidationError as exc:
        raise _contract("réponse JEV", exc) from exc
    if raw.model != expected_model:
        raise JevContractError(
            f"version de modèle inattendue : {raw.model!r} (demandé {expected_model!r})",
            model_effective=raw.model,
            model_requested=expected_model,
        )
    confidences: dict[str, float] = {}
    if raw.provider_metadata is not None and raw.provider_metadata.typesafe is not None:
        confidences = dict(raw.provider_metadata.typesafe.confidence)
    answers: dict[str, JevAnswer] = {}
    missing = [qid for qid in questions if qid not in raw.answers]
    if missing:
        raise JevContractError(f"réponses manquantes : {missing}", missing=missing)
    for qid, spec in questions.items():
        answers[qid] = parse_answer(
            qid, spec, raw.answers[qid], confidence=confidences.get(qid), tolerance=tolerance
        )
    unknown = tuple(sorted(set(raw.answers) - set(questions)))
    return ParsedResponse(
        model_effective=raw.model,
        answers=answers,
        usage=raw.usage,
        raw_usage=dict(raw.usage.model_dump(mode="json", exclude_none=True)),
        confidence=confidences,
        unknown_answer_ids=unknown,
    )


def answers_from_json(raw: Mapping[str, Any]) -> dict[str, JevAnswer]:
    """Relit des réponses persistées (``jev_results.answers``) en objets du domaine."""
    return {qid: JevAnswer.model_validate(value) for qid, value in raw.items()}


def answers_to_json(answers: Mapping[str, JevAnswer]) -> dict[str, Any]:
    return {qid: answer.model_dump(mode="json", exclude_none=True) for qid, answer in answers.items()}
