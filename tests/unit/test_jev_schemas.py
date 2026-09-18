"""Schémas JEV : requête stricte, réponse tolérante aux inconnus mais stricte sur l'obligatoire (T52, T53)."""

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from okxq.domain.errors import JevContractError
from okxq.domain.events import JevAnswerType
from okxq.jev.schemas import (
    PROBABILITY_SUM_TOLERANCE,
    JevAssetState,
    JevDocumentState,
    JevRequest,
    JevState,
    build_request,
    load_question_set,
    parse_json_strict,
    parse_response,
    truncate_text,
)

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "jev"
QUESTIONS = load_question_set(ROOT / "configs" / "jev_questions.v1.json")
REFERENCE_RESPONSE = json.loads((FIX / "response_reference.json").read_text(encoding="utf-8"))
INVALID = json.loads((FIX / "invalid_responses.json").read_text(encoding="utf-8"))


def _reference():
    return json.loads(json.dumps(REFERENCE_RESPONSE))


def _parse(raw):
    return parse_response(raw, questions=QUESTIONS.questions, expected_model=QUESTIONS.model)


# --- requête -------------------------------------------------------------------------------------------


def test_state_accepts_only_asset_document_prior_documents():
    asset = JevAssetState(canonical_name="Example Network", symbol="EXM")
    doc = JevDocumentState(title="t", text="x")
    JevState(asset=asset, document=doc)
    for forbidden in (
        {"okx_api_key": "k"},
        {"positions": [{"inst_id": "EXM-USDT-SWAP", "contracts": "10"}]},
        {"equity_usdt": "100000"},
        {"account_id": "acc"},
        {"tools": [{"name": "place_order"}]},
    ):
        with pytest.raises(ValidationError):
            JevState(asset=asset, document=doc, **forbidden)
    with pytest.raises(ValidationError):
        JevAssetState(canonical_name="Example Network", symbol="EXM", position="long")
    with pytest.raises(ValidationError):
        JevDocumentState(title="t", text="x", api_secret="s")
    with pytest.raises(ValidationError):
        JevRequest(model="jev-1.13.0", state=JevState(asset=asset, document=doc), questions=QUESTIONS.questions, account="x")


def test_reference_request_matches_question_set_and_is_strict():
    raw = json.loads((FIX / "request_reference.json").read_text(encoding="utf-8"))
    request = JevRequest.model_validate(raw)
    assert request.model == QUESTIONS.model
    assert request.questions == QUESTIONS.questions
    assert set(request.payload()) == {"model", "state", "questions"}
    assert set(request.payload()["state"]) == {"asset", "document", "prior_documents"}
    raw["state"]["credentials"] = {"api_key": "k"}
    with pytest.raises(ValidationError):
        JevRequest.model_validate(raw)


def test_question_set_hash_is_stable_and_content_sensitive():
    again = load_question_set(ROOT / "configs" / "jev_questions.v1.json")
    assert again.question_set_hash == QUESTIONS.question_set_hash
    mutated = QUESTIONS.model_copy(update={"notes": "autre note"})
    assert mutated.question_set_hash == QUESTIONS.question_set_hash  # les notes ne font pas partie du contrat
    changed = {**QUESTIONS.questions}
    changed["asset_relevance"] = changed["asset_relevance"].model_copy(update={"instructions": "Different."})
    assert QUESTIONS.model_copy(update={"questions": changed}).question_set_hash != QUESTIONS.question_set_hash


def test_truncate_and_build_request_bounds_document():
    text, truncated = truncate_text("a" * 100, 20)
    assert truncated and len(text) <= 20
    assert truncate_text("abc", 20) == ("abc", False)
    build = build_request(
        model="jev-1.13.0",
        question_set=QUESTIONS,
        canonical_name="Example Network",
        symbol="EXM",
        title="T",
        text="x" * 50,
        prior_documents=[("p", "y" * 50)],
        max_document_characters=10,
    )
    assert build.truncated and build.document_characters <= 10
    assert len(build.request.state.prior_documents[0].text) <= 10


# --- réponse : T52 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", [c for c in INVALID if c["name"].startswith("T52")], ids=lambda c: c["name"])
def test_T52_out_of_range_boolean_or_missing_probability_rejected(case):
    with pytest.raises(JevContractError):
        _parse(case["response"])


def test_T52_nan_and_infinity_rejected_in_json_and_python_objects():
    with pytest.raises(JevContractError):
        _parse((FIX / "response_invalid_nan.txt").read_bytes())
    with pytest.raises(JevContractError):
        parse_json_strict('{"a": Infinity}')
    for bad in (math.nan, math.inf, -math.inf):
        raw = _reference()
        raw["answers"]["asset_relevance"]["probability"] = bad
        with pytest.raises(JevContractError):
            _parse(raw)


# --- réponse : T53 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", [c for c in INVALID if c["name"].startswith("T53")], ids=lambda c: c["name"])
def test_T53_inconsistent_sum_unknown_category_version_or_missing_distribution_rejected(case):
    with pytest.raises(JevContractError):
        _parse(case["response"])


def test_T53_sum_tolerance_is_explicit():
    raw = _reference()
    probs = raw["answers"]["event_kind"]["probabilities"]
    probs["maintenance"] += PROBABILITY_SUM_TOLERANCE * 0.9
    _parse(raw)
    probs["maintenance"] += PROBABILITY_SUM_TOLERANCE * 0.5
    with pytest.raises(JevContractError, match="somme"):
        _parse(raw)


def test_T53_score_distribution_never_rebuilt_from_score_alone():
    raw = _reference()
    raw["answers"]["novelty_vs_prior"] = {"score": 2}
    with pytest.raises(JevContractError):
        _parse(raw)


# --- tolérance aux champs inconnus -------------------------------------------------------------------


def test_unknown_fields_tolerated_but_required_fields_not():
    raw = _reference()
    raw["debug"] = {"trace": "x"}
    raw["answers"]["asset_relevance"]["explanation"] = "because"
    raw["answers"]["extra_question"] = {"probability": 0.2}
    raw["usage"]["billingTier"] = "standard"
    raw["providerMetadata"]["typesafe"]["latency"] = 12
    parsed = _parse(raw)
    assert parsed.unknown_answer_ids == ("extra_question",)
    assert parsed.answers["asset_relevance"].confidence == pytest.approx(0.93)
    assert parsed.raw_usage["billingTier"] == "standard"
    assert parsed.usage.input_tokens == 812
    del raw["answers"]
    with pytest.raises(JevContractError):
        _parse(raw)


def test_reference_response_converts_to_domain_answers():
    parsed = _parse(REFERENCE_RESPONSE)
    kind = parsed.answers["event_kind"]
    assert kind.type is JevAnswerType.CHOICE and kind.choice == "maintenance"
    assert list(kind.probabilities) == QUESTIONS.event_categories
    sev = parsed.answers["reported_severity"]
    assert sev.type is JevAnswerType.SCORE and sev.score == 1
    assert sev.score_distribution == {"0": 0.15, "1": 0.70, "2": 0.10, "3": 0.04, "4": 0.01}
    assert parsed.answers["source_confirms"].probability == pytest.approx(0.9)
    assert parsed.model_effective == "jev-1.13.0"


def test_score_distribution_accepts_dict_form_and_distribution_alias():
    raw = _reference()
    raw["answers"]["novelty_vs_prior"] = {"score": 0, "distribution": {"0": 0.8, "1": 0.15, "2": 0.05}}
    parsed = _parse(raw)
    assert parsed.answers["novelty_vs_prior"].score_distribution == {"0": 0.8, "1": 0.15, "2": 0.05}
    raw["answers"]["novelty_vs_prior"] = {"score": 0, "distribution": {"0": 0.8, "1": 0.15, "5": 0.05}}
    with pytest.raises(JevContractError):
        _parse(raw)


def test_providermetadata_optional_but_validated_when_present():
    raw = _reference()
    del raw["providerMetadata"]
    parsed = _parse(raw)
    assert parsed.answers["event_kind"].confidence is None
    raw = _reference()
    raw["providerMetadata"]["typesafe"]["confidence"]["event_kind"] = 1.5
    with pytest.raises(JevContractError):
        _parse(raw)
