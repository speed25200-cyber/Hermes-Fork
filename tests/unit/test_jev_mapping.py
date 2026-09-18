"""Mapping d'entités point-in-time (T54) et sélection des documents antérieurs comparables."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from okxq.domain.errors import JevError
from okxq.domain.events import AssetMapping, SourceDocument
from okxq.jev.entity_mapping import (
    EntityRecord,
    EntityRegistry,
    MappingQuality,
    jaccard,
    select_prior_documents,
    word_shingles,
)
from okxq.jev.quality import document_from_case, load_corpus
from okxq.jev.schemas import load_question_set

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "jev"
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
REGISTRY = EntityRegistry.load(FIX / "entity_registry.json")
QUESTIONS = load_question_set(ROOT / "configs" / "jev_questions.v1.json")
CORPUS = load_corpus(FIX / "corpus.jsonl", question_set=QUESTIONS)


def test_T54_ambiguous_ticker_yields_no_mapping_and_explicit_quality():
    result = REGISTRY.resolve(
        title="EXM to be listed on a new venue", text="EXM will be listed next week.", as_of=T0
    )
    assert result.mappings == [] and result.quality is MappingQuality.AMBIGUOUS
    assert result.candidates == ["EXM-USDT-SWAP", "EXMFI-USDT-SWAP"]
    assert any("non inventé" in n for n in result.notes)


def test_T54_ticker_alone_is_not_an_identity():
    result = REGISTRY.resolve(title="$SOL rallies", text="$SOL climbed 20% today.", as_of=T0)
    assert (
        result.mappings == []
        and result.quality is MappingQuality.TICKER_ONLY
        and result.candidates == ["SOL-USDT-SWAP"]
    )
    lowercase = REGISTRY.resolve(title="", text="the sol of the earth and btc-like words", as_of=T0)
    assert lowercase.quality is MappingQuality.NONE and lowercase.candidates == []


def test_canonical_name_alias_and_network_context_methods():
    by_name = REGISTRY.resolve(
        title="Scheduled service interruption", text="Example Network will pause EXM withdrawals.", as_of=T0
    )
    assert [m.method for m in by_name.mappings] == ["canonical_name"] and by_name.mappings[
        0
    ].confidence >= 0.95
    by_alias = REGISTRY.resolve(
        title="ExampleNet upgrade", text="ExampleNet developers scheduled v2.", as_of=T0
    )
    assert by_alias.mappings[0].method == "project_alias" and by_alias.mappings[0].inst_id == "EXM-USDT-SWAP"
    with_context = REGISTRY.resolve(
        title="EXM parameters", text="Validators on Example Chain changed EXM staking.", as_of=T0
    )
    assert (
        with_context.quality is MappingQuality.OK and with_context.mappings[0].method == "ticker_with_context"
    )
    assert (
        with_context.mappings[0].confidence < by_alias.mappings[0].confidence < by_name.mappings[0].confidence
    )


def test_multi_asset_document_maps_each_asset():
    result = REGISTRY.resolve(
        title="Maintenance", text="Bitcoin and Ethereum perpetual engines will pause.", as_of=T0
    )
    assert sorted(m.inst_id for m in result.mappings) == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


def test_rename_is_point_in_time():
    text = "Polygon completes the MATIC to POL migration."
    after = REGISTRY.resolve(title="", text=text, as_of=datetime(2024, 10, 1, tzinfo=UTC))
    before = REGISTRY.resolve(title="", text=text, as_of=datetime(2024, 3, 1, tzinfo=UTC))
    assert [m.inst_id for m in after.mappings] == ["POL-USDT-SWAP"]
    assert [m.inst_id for m in before.mappings] == ["MATIC-USDT-SWAP"]
    assert [
        m.inst_id
        for m in REGISTRY.resolve(title="", text=text, as_of=datetime(2020, 1, 1, tzinfo=UTC)).mappings
    ] == []


@pytest.mark.parametrize("case", CORPUS, ids=lambda c: c.case_id)
def test_corpus_expected_mappings_hold(case):
    as_of = case.document.published_at or T0
    result = REGISTRY.resolve(title=case.document.title, text=case.document.text, as_of=as_of)
    assert result.quality is case.expected_mapping.quality
    assert sorted(m.inst_id for m in result.mappings) == sorted(case.expected_mapping.inst_ids)


def test_registry_rejects_inconsistent_records():
    with pytest.raises(JevError):
        EntityRegistry([], version="v")
    rec = EntityRecord(inst_id="X-USDT-SWAP", canonical_name="Xample", symbol="X", valid_from=T0, valid_to=T0)
    with pytest.raises(JevError):
        EntityRegistry([rec], version="v")


# --- documents antérieurs comparables --------------------------------------------------------------------


def _doc(
    case_id: str,
    *,
    received_at: datetime,
    text: str,
    title: str = "Scheduled service interruption",
    inst_id="EXM-USDT-SWAP",
    version=1,
    published_at=None,
) -> SourceDocument:
    return SourceDocument(
        document_id=f"doc_{case_id}",
        source="s",
        source_url="https://news.example.test/x",
        published_at=published_at,
        first_seen_at=received_at,
        received_at=received_at,
        version=version,
        date_method="absent",
        raw_text_hash=f"h_{case_id}_{version}",
        deduplication_id=case_id,
        asset_mapping=[
            AssetMapping(
                inst_id=inst_id,
                canonical_name="Example Network",
                symbol="EXM",
                mapping_version="v",
                confidence=0.95,
                method="canonical_name",
            )
        ],
        title=title,
        text=text,
    )


BASE = "Example Network will undergo a scheduled service interruption on 2026-09-20 from 02:00 to 04:00 UTC. Deposits and withdrawals paused. Trading remains available."


def test_prior_selection_is_point_in_time_and_asset_scoped():
    current = _doc("now", received_at=T0, text=BASE)
    earlier = _doc(
        "earlier",
        received_at=T0 - timedelta(hours=2),
        text="Example Network announces validator upgrade next week.",
    )
    published_before_received_after = _doc(
        "late",
        received_at=T0 + timedelta(minutes=1),
        published_at=T0 - timedelta(days=1),
        text="Old news received late.",
    )
    other_asset = _doc(
        "btc", received_at=T0 - timedelta(hours=1), text="Bitcoin maintenance", inst_id="BTC-USDT-SWAP"
    )
    too_old = _doc("old", received_at=T0 - timedelta(days=30), text="Ancient notice about Example Network.")
    sel = select_prior_documents(
        current,
        [earlier, published_before_received_after, other_asset, too_old, current],
        inst_id="EXM-USDT-SWAP",
    )
    assert [d.document_id for d in sel.priors] == ["doc_earlier"] and sel.considered == 1


def test_prior_selection_merges_near_duplicates_but_never_distinct_events_with_same_title():
    current = _doc("now", received_at=T0, text=BASE + " Reminder.")
    syndicated_a = _doc("a", received_at=T0 - timedelta(hours=3), text=BASE)
    syndicated_b = _doc("b", received_at=T0 - timedelta(hours=2), text=BASE + " (syndicated copy)")
    distinct = _doc(
        "c",
        received_at=T0 - timedelta(hours=1),
        text="Example Network will undergo a scheduled service interruption on 2026-10-05 from 22:00 to 23:00 UTC to rotate validator keys; withdrawals paused for one hour only.",
    )
    sel = select_prior_documents(current, [syndicated_a, syndicated_b, distinct], inst_id="EXM-USDT-SWAP")
    assert [d.document_id for d in sel.priors] == ["doc_a", "doc_c"] and sel.dropped_near_duplicates == 1
    previous_version = _doc(
        "now", received_at=T0 - timedelta(hours=4), text="Example Network initial notice.", version=1
    )
    current_v2 = _doc("now", received_at=T0, text=BASE, version=2)
    sel2 = select_prior_documents(current_v2, [previous_version, current_v2], inst_id="EXM-USDT-SWAP")
    assert [d.version for d in sel2.priors] == [1]
    capped = select_prior_documents(
        current,
        [
            _doc(
                f"d{i}",
                received_at=T0 - timedelta(minutes=i + 1),
                text=f"Distinct event number {i} about Example Network with unique words {i * 7}.",
            )
            for i in range(10)
        ],
        inst_id="EXM-USDT-SWAP",
        max_prior=3,
    )
    assert len(capped.priors) == 3 and capped.considered == 10


def test_shingles_and_jaccard_basics():
    a = word_shingles("Example Network will undergo a scheduled service interruption")
    assert (
        jaccard(a, a) == 1.0 and jaccard(a, frozenset()) == 0.0 and jaccard(frozenset(), frozenset()) == 1.0
    )
    assert word_shingles("ab") == frozenset({"ab"}) and word_shingles("") == frozenset()


def test_document_from_corpus_case_keeps_declared_date_or_none():
    undated = next(c for c in CORPUS if "undated" in c.tags)
    doc = document_from_case(undated, received_at=T0)
    assert doc.published_at is None and doc.date_method == "absent" and doc.received_at == T0
    dated = next(c for c in CORPUS if c.document.published_at is not None)
    assert document_from_case(dated, received_at=T0).published_at == dated.document.published_at
