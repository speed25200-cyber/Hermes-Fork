"""Propriétés des validateurs JEV (Hypothesis) : nombres stricts, distributions, bornes, SSRF, similarité."""

import ipaddress
import json
import math
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from okxq.domain.errors import JevContractError
from okxq.jev.entity_mapping import jaccard, word_shingles
from okxq.jev.schemas import (
    PROBABILITY_SUM_TOLERANCE,
    load_question_set,
    parse_answer,
    strict_int,
    strict_number,
    truncate_text,
)
from okxq.jev.source_connectors import is_public_address

pytestmark = pytest.mark.property

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS = load_question_set(ROOT / "configs" / "jev_questions.v1.json")
CATEGORIES = QUESTIONS.event_categories
EVENT_KIND = QUESTIONS.questions["event_kind"]
SEVERITY = QUESTIONS.questions["reported_severity"]
RELEVANCE = QUESTIONS.questions["asset_relevance"]


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_finite_floats_pass_strict_number(x):
    assert strict_number(x) == x


@given(st.sampled_from([math.nan, math.inf, -math.inf, True, False, "1.0", None, [1.0]]))
def test_non_finite_or_non_numeric_values_are_rejected(x):
    with pytest.raises(ValueError):
        strict_number(x)


@given(st.one_of(st.booleans(), st.floats(), st.text()))
def test_strict_int_rejects_bool_float_and_text(x):
    with pytest.raises(ValueError):
        strict_int(x)


@given(
    st.lists(st.floats(min_value=0.001, max_value=1.0), min_size=len(CATEGORIES), max_size=len(CATEGORIES))
)
def test_normalized_choice_distributions_are_accepted(weights):
    total = math.fsum(weights)
    probabilities = {c: w / total for c, w in zip(CATEGORIES, weights, strict=True)}
    chosen = max(probabilities, key=probabilities.get)
    answer = parse_answer(
        "event_kind", EVENT_KIND, {"choice": chosen, "probabilities": probabilities}, confidence=None
    )
    assert answer.choice == chosen and list(answer.probabilities) == CATEGORIES
    assert math.fsum(answer.probabilities.values()) == pytest.approx(1.0, abs=PROBABILITY_SUM_TOLERANCE)


@given(
    st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=len(CATEGORIES), max_size=len(CATEGORIES)),
    st.floats(min_value=PROBABILITY_SUM_TOLERANCE * 2, max_value=0.5),
)
def test_choice_distributions_off_by_more_than_tolerance_are_rejected(weights, delta):
    total = math.fsum(weights)
    if total == 0:
        weights = [1.0] * len(CATEGORIES)
        total = float(len(CATEGORIES))
    probabilities = {c: w / total for c, w in zip(CATEGORIES, weights, strict=True)}
    probabilities[CATEGORIES[0]] = min(1.0, probabilities[CATEGORIES[0]] + delta)
    if abs(math.fsum(probabilities.values()) - 1.0) <= PROBABILITY_SUM_TOLERANCE:
        return
    with pytest.raises(JevContractError):
        parse_answer(
            "event_kind",
            EVENT_KIND,
            {"choice": CATEGORIES[0], "probabilities": probabilities},
            confidence=None,
        )


@given(st.text(min_size=1, max_size=12).filter(lambda s: s not in CATEGORIES))
def test_unknown_category_is_always_rejected(extra):
    probabilities = {c: 1.0 / len(CATEGORIES) for c in CATEGORIES}
    probabilities[extra] = 0.0
    with pytest.raises(JevContractError):
        parse_answer(
            "event_kind",
            EVENT_KIND,
            {"choice": CATEGORIES[0], "probabilities": probabilities},
            confidence=None,
        )


@given(st.integers(min_value=0, max_value=SEVERITY.level_count - 1), st.floats(min_value=0.5, max_value=1.0))
def test_score_with_consistent_distribution_is_accepted_and_without_is_rejected(level, mass):
    rest = (1.0 - mass) / (SEVERITY.level_count - 1)
    distribution = [mass if i == level else rest for i in range(SEVERITY.level_count)]
    answer = parse_answer(
        "reported_severity", SEVERITY, {"score": level, "probabilities": distribution}, confidence=0.5
    )
    assert answer.score == level and answer.score_distribution[str(level)] == pytest.approx(mass)
    with pytest.raises(JevContractError):
        parse_answer("reported_severity", SEVERITY, {"score": level}, confidence=0.5)


@given(st.integers().filter(lambda i: not 0 <= i < SEVERITY.level_count))
def test_score_outside_levels_is_rejected(level):
    uniform = [1.0 / SEVERITY.level_count] * SEVERITY.level_count
    with pytest.raises(JevContractError):
        parse_answer(
            "reported_severity", SEVERITY, {"score": level, "probabilities": uniform}, confidence=None
        )


@given(st.floats(allow_nan=False, allow_infinity=False).filter(lambda p: not 0.0 <= p <= 1.0))
def test_noul_probability_outside_unit_interval_is_rejected(p):
    with pytest.raises(JevContractError):
        parse_answer("asset_relevance", RELEVANCE, {"probability": p}, confidence=None)


@given(st.text(), st.integers(min_value=1, max_value=200))
def test_truncate_text_is_bounded_and_idempotent(text, limit):
    out, truncated = truncate_text(text, limit)
    assert len(out) <= limit
    assert truncated == (len(text) > limit)
    assert truncate_text(out, limit) == (out, False)


@given(
    st.text(alphabet=st.characters(whitelist_categories=("L", "N", "Z")), max_size=200),
    st.text(alphabet=st.characters(whitelist_categories=("L", "N", "Z")), max_size=200),
)
def test_jaccard_is_symmetric_bounded_and_reflexive(a, b):
    sa, sb = word_shingles(a), word_shingles(b)
    value = jaccard(sa, sb)
    assert 0.0 <= value <= 1.0 and value == jaccard(sb, sa) and jaccard(sa, sa) == 1.0


#: Plages IPv4 qu'une requête sortante ne doit jamais atteindre : réseaux privés, boucle locale,
#: lien-local (qui porte les métadonnées cloud) et espace partagé des opérateurs.
PLAGES_IPV4_INTERDITES = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "100.64.0.0/10",
)

adresses_privees = st.one_of(
    *[st.ip_addresses(v=4, network=plage) for plage in PLAGES_IPV4_INTERDITES],
    st.ip_addresses(v=6, network="fc00::/7"),
    st.ip_addresses(v=6, network="fe80::/10"),
)


@settings(max_examples=200)
@given(adresses_privees)
def test_private_ranges_are_never_public(address):
    assert is_public_address(str(address)) is False


@settings(max_examples=200)
@given(st.one_of(*[st.ip_addresses(v=4, network=plage) for plage in PLAGES_IPV4_INTERDITES]))
def test_a_private_address_wrapped_as_ipv6_is_still_refused(address):
    """Le contournement à connaître : écrire une adresse privée sous forme IPv6 mappée.

    ``::ffff:127.0.0.1`` désigne la boucle locale, mais ne ressemble pas à une adresse IPv4 : un
    contrôle qui ne déballerait pas le mappage laisserait passer une requête vers l'hôte lui-même,
    ou vers le service de métadonnées de l'hébergeur. Le contrôle déballe ``ipv4_mapped`` avant tout
    jugement, et c'est cette propriété-là qui est vérifiée ici.

    À ne pas confondre avec une propriété fausse que ce test portait auparavant : le bloc
    ``::ffff:0:0/96`` N'EST PAS une plage privée. Il est l'image de l'ESPACE IPv4 TOUT ENTIER, donc
    majoritairement public — ``::ffff:1.0.0.0`` est une adresse publique parfaitement légitime.
    Exiger que tout ce bloc soit refusé demandait de bloquer la moitié de l'Internet.
    """
    assert is_public_address(f"::ffff:{address}") is False
    assert is_public_address(str(ipaddress.IPv6Address(f"::ffff:{address}"))) is False


@given(st.ip_addresses(v=4).filter(lambda a: a.is_global and not a.is_multicast))
def test_global_ipv4_is_public_unless_metadata(address):
    expected = address != ipaddress.ip_address("169.254.169.254")
    assert is_public_address(str(address)) is expected or str(address) in {"100.100.100.200", "192.0.0.192"}


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8), st.floats(allow_nan=False, allow_infinity=False), max_size=5
    )
)
def test_json_round_trip_of_random_usage_never_breaks_parser_when_answers_valid(extra):
    from okxq.jev.schemas import parse_response

    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "jev" / "response_reference.json").read_text(encoding="utf-8")
    )
    fixture["usage"].update(
        {f"x_{k}": v for k, v in extra.items() if k not in ("inputTokens", "outputTokens")}
    )
    parsed = parse_response(fixture, questions=QUESTIONS.questions, expected_model=QUESTIONS.model)
    assert parsed.usage.input_tokens == 812
