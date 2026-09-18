"""Contrat TypeSafe sur fixtures (aucun appel réel : marqué ``contract``, jamais ``connected``)."""

import json
from pathlib import Path

import pytest

from okxq.domain.errors import JevContractError
from okxq.jev.schemas import JevRequest, load_question_set, parse_response

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "jev"
DOC = ROOT / "docs" / "api_contracts" / "typesafe_jev.md"
QUESTIONS = load_question_set(ROOT / "configs" / "jev_questions.v1.json")


def test_reference_request_is_the_documented_example_and_round_trips():
    raw = json.loads((FIX / "request_reference.json").read_text(encoding="utf-8"))
    request = JevRequest.model_validate(raw)
    assert request.state.document.title == "Scheduled service interruption"
    assert request.state.asset.canonical_name == "Example Network" and request.model == "jev-1.13.0"
    assert json.loads(json.dumps(request.payload())) == raw
    assert set(request.questions) == {
        "event_kind",
        "asset_relevance",
        "reported_severity",
        "novelty_vs_prior",
        "reported_operational_impact",
        "source_confirms",
    }
    assert {q.type for q in request.questions.values()} == {"choice", "noul", "score"}


def test_reference_response_parses_with_confidence_and_usage():
    parsed = parse_response(
        (FIX / "response_reference.json").read_bytes(),
        questions=QUESTIONS.questions,
        expected_model=QUESTIONS.model,
    )
    assert parsed.model_effective == QUESTIONS.model and len(parsed.answers) == 6
    assert parsed.confidence["asset_relevance"] == pytest.approx(0.93)
    assert (
        parsed.usage.input_tokens == 812
        and parsed.usage.output_tokens == 96
        and parsed.usage.total_tokens == 908
    )
    for answer in parsed.answers.values():
        if answer.probabilities is not None:
            assert sum(answer.probabilities.values()) == pytest.approx(1.0, abs=1e-3)
        if answer.score_distribution is not None:
            assert sum(answer.score_distribution.values()) == pytest.approx(1.0, abs=1e-3)


@pytest.mark.parametrize(
    "case", json.loads((FIX / "invalid_responses.json").read_text(encoding="utf-8")), ids=lambda c: c["name"]
)
def test_every_documented_invalid_response_is_refused(case):
    with pytest.raises(JevContractError):
        parse_response(case["response"], questions=QUESTIONS.questions, expected_model=QUESTIONS.model)


def test_nan_literal_is_refused():
    with pytest.raises(JevContractError):
        parse_response(
            (FIX / "response_invalid_nan.txt").read_bytes(),
            questions=QUESTIONS.questions,
            expected_model=QUESTIONS.model,
        )


def test_contract_document_records_url_verification_date_and_no_real_call():
    text = DOC.read_text(encoding="utf-8")
    assert "https://api.typesafe.ai/v1/systemone" in text and "2026-09-18" in text
    assert "docs.typesafe.ai" in text and "aucun appel réel" in text.lower()
    assert "Retry-After" in text and "max_tokens_exceeded" in text
