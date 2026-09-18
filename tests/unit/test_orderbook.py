"""Carnet OKX : séquences, resets, checksum déprécié, validité (T05–T13)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from okxq.data.orderbook import (
    BookAction,
    BookState,
    BookValidator,
    OrderBook,
    channel_profile,
    parse_level,
)
from okxq.domain.errors import BookInvalidError, DataQualityError, SequenceGapError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "okx"
TS = 1_789_732_800_000


def fixture(name: str) -> dict:
    """Message unique d'une fixture (``message``) ; les fixtures multi-messages passent par ``fixtures``."""
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if "message" in raw:
        return dict(raw["message"])
    if "messages" in raw:
        return dict(raw["messages"][0])
    return dict(raw)


def fixtures(name: str) -> list[dict]:
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if "messages" in raw:
        return [dict(m) for m in raw["messages"]]
    return [fixture(name)]


def msg(action: str, *, seq: int | None, prev: int | None, bids=(), asks=(), checksum=0, ts=TS) -> dict:
    return {
        "action": action,
        "bids": [list(b) for b in bids],
        "asks": [list(a) for a in asks],
        "seq_id": seq,
        "prev_seq_id": prev,
        "checksum": checksum,
        "ts_ms": ts,
    }


def seeded() -> BookValidator:
    v = BookValidator("BTC-USDT-SWAP", "books")
    r = v.ingest(
        msg("snapshot", seq=100, prev=-1, bids=[["100.0", "5"], ["99.9", "7"]], asks=[["100.1", "4"]])
    )
    assert r.accepted
    return v


def test_T05_snapshot_then_valid_updates_match_reference():
    v = seeded()
    r = v.ingest(msg("update", seq=101, prev=100, bids=[["99.8", "2"]], asks=[["100.2", "3"]]))
    assert r.accepted and r.action is BookAction.UPDATE
    view = v.view()
    assert view.state is BookState.VALID
    assert view.bids == (
        (Decimal("100.0"), Decimal("5")),
        (Decimal("99.9"), Decimal("7")),
        (Decimal("99.8"), Decimal("2")),
    )
    assert view.asks == ((Decimal("100.1"), Decimal("4")), (Decimal("100.2"), Decimal("3")))
    assert view.mid == Decimal("100.05")
    assert view.usable


def test_T06_sequence_gap_invalidates_and_blocks():
    v = seeded()
    r = v.ingest(msg("update", seq=140, prev=120, bids=[["99.0", "1"]]))
    assert not r.accepted and r.resync_required
    assert v.state is BookState.RESYNC_REQUIRED
    assert not v.view().usable  # aucune décision sur ce carnet
    assert v.counters()["gaps"] == 1
    # tant qu'aucun snapshot n'est revenu, un update reste refusé
    assert not v.ingest(msg("update", seq=141, prev=140)).accepted
    assert v.ingest(msg("snapshot", seq=200, prev=-1, bids=[["100", "1"]], asks=[["101", "1"]])).accepted
    assert v.state is BookState.VALID


def test_T07_non_consecutive_sequences_are_not_false_rejections():
    v = seeded()
    for seq, prev in ((105, 100), (117, 105), (2000, 117)):
        r = v.ingest(msg("update", seq=seq, prev=prev, bids=[["99.9", "8"]]))
        assert r.accepted, (seq, prev, r.reason)
    assert v.counters()["gaps"] == 0
    assert v.view().seq_id == 2000


def test_T08_heartbeat_distinguishes_staleness_from_connection_health():
    v = seeded()
    now = datetime.fromtimestamp(TS / 1000, tz=UTC)
    r = v.ingest(msg("update", seq=100, prev=100, ts=TS + 5000))
    assert r.accepted and r.action is BookAction.HEARTBEAT
    assert v.counters()["heartbeats"] == 1 and v.counters()["updates"] == 0
    assert v.state is BookState.VALID  # connexion vivante
    # le dernier CHANGEMENT de prix reste ancien : les deux mesures sont distinctes
    age = v.book.age_of_last_change_seconds(now.replace(microsecond=0))
    assert age is not None and age >= 0
    assert v.book.exchange_ts is not None and v.book.exchange_ts > v.book.last_change_ts


def test_T09_sequence_reset_requires_new_snapshot():
    v = seeded()
    r = v.ingest(msg("update", seq=5, prev=4, bids=[["99", "1"]]))
    assert not r.accepted and r.resync_required
    assert v.counters()["resets"] == 1 and v.counters()["gaps"] == 0
    assert v.state is BookState.RESYNC_REQUIRED


def test_T10_deprecated_checksum_is_never_computed():
    profile = channel_profile("books")
    assert profile.checksum_deprecated and profile.sequenced
    v = seeded()
    before = v.counters()["checksum_ignored"]
    assert v.ingest(msg("update", seq=101, prev=100, bids=[["99.7", "1"]], checksum=0)).accepted
    assert v.counters()["checksum_ignored"] == before + 1
    # un checksum non nul n'est pas interprété comme un CRC valide/invalide sur ce canal
    assert v.ingest(msg("update", seq=102, prev=101, bids=[["99.6", "1"]], checksum=123456)).accepted
    assert not channel_profile("books5").sequenced


def test_T11_zero_quantity_removes_level_and_rebuilds_top():
    v = seeded()
    assert v.view().best_bid == (Decimal("100.0"), Decimal("5"))
    assert v.ingest(msg("update", seq=101, prev=100, bids=[["100.0", "0"]])).accepted
    assert v.view().best_bid == (Decimal("99.9"), Decimal("7"))
    assert Decimal("100.0") not in dict(v.view().bids)


def test_T12_crossed_negative_and_nan_are_invalid():
    v = seeded()
    r = v.ingest(msg("update", seq=101, prev=100, bids=[["100.5", "1"]]))  # croise l'ask 100.1
    assert not r.accepted and "croisé" in (r.reason or "")
    assert v.state is BookState.RESYNC_REQUIRED
    v2 = seeded()
    assert not v2.ingest(msg("update", seq=101, prev=100, bids=[["99.5", "-3"]])).accepted
    v3 = seeded()
    assert not v3.ingest(msg("update", seq=101, prev=100, asks=[["nan", "1"]])).accepted
    v4 = seeded()
    assert not v4.ingest(msg("update", seq=101, prev=100, asks=[["0", "1"]])).accepted
    assert v4.counters()["invalid"] == 1
    with pytest.raises(BookInvalidError):
        parse_level("1", "inf", inst_id="X")


def test_T13_rest_snapshot_cannot_be_spliced_with_ws_increments():
    v = seeded()  # source ws par défaut
    rest = v.ingest(
        msg("snapshot", seq=999, prev=-1, bids=[["100", "1"]], asks=[["101", "1"]]), source="rest"
    )
    assert not rest.accepted and "non raccordables" in (rest.reason or "")
    increment = v.ingest(msg("update", seq=101, prev=100, bids=[["99.5", "1"]]), source="rest")
    assert not increment.accepted and "non raccordables" in (increment.reason or "")
    assert v.view().seq_id == 100  # le carnet WebSocket est intact


def test_books5_and_bbo_are_snapshot_per_message_without_sequence():
    for channel, depth in (("books5", 5), ("bbo-tbt", 1)):
        v = BookValidator("BTC-USDT-SWAP", channel)
        profile = channel_profile(channel)
        assert profile.snapshot_per_message and not profile.sequenced and profile.depth == depth
        r = v.ingest({"bids": [["100", "1"]], "asks": [["101", "2"]], "ts_ms": TS})
        assert r.accepted and r.action is BookAction.SNAPSHOT
        r2 = v.ingest({"bids": [["100.5", "3"]], "asks": [["101", "2"]], "ts_ms": TS + 100})
        assert r2.accepted and v.view().best_bid == (Decimal("100.5"), Decimal("3"))


def test_memory_bound_and_unknown_channel_are_refused():
    with pytest.raises(DataQualityError):
        channel_profile("books99")
    book = OrderBook(inst_id="X", channel="books5")
    with pytest.raises(BookInvalidError):
        book.apply_snapshot(
            [[str(100 - i * 0.01), "1"] for i in range(20)], [["200", "1"]], seq_id=1, exchange_ts=None
        )


def test_update_without_snapshot_is_refused():
    v = BookValidator("BTC-USDT-SWAP", "books")
    r = v.ingest(msg("update", seq=2, prev=1, bids=[["1", "1"]]))
    assert not r.accepted
    with pytest.raises(SequenceGapError):
        OrderBook(inst_id="X").apply_update([], [], seq_id=2, prev_seq_id=1, exchange_ts=None)


def test_official_fixtures_are_accepted_and_gap_fixture_is_detected():
    snap = fixture("ws_books_snapshot.json")
    v = BookValidator(str(snap["arg"]["instId"]), str(snap["arg"]["channel"]))
    row = snap["data"][0]
    assert v.ingest(
        {
            "action": "snapshot",
            "bids": row["bids"],
            "asks": row["asks"],
            "seq_id": row["seqId"],
            "prev_seq_id": row.get("prevSeqId"),
            "checksum": row.get("checksum"),
            "ts_ms": row["ts"],
        }
    ).accepted
    for message in fixtures("ws_books_updates.json"):
        for row in message["data"]:
            res = v.ingest(
                {
                    "action": "update",
                    "bids": row["bids"],
                    "asks": row["asks"],
                    "seq_id": row["seqId"],
                    "prev_seq_id": row["prevSeqId"],
                    "checksum": row.get("checksum"),
                    "ts_ms": row["ts"],
                }
            )
            assert res.accepted, res.reason
    gap = fixture("ws_books_gap.json")
    row = gap["data"][0]
    res = v.ingest(
        {
            "action": "update",
            "bids": row["bids"],
            "asks": row["asks"],
            "seq_id": row["seqId"],
            "prev_seq_id": row["prevSeqId"],
            "checksum": row.get("checksum"),
            "ts_ms": row["ts"],
        }
    )
    assert not res.accepted and res.resync_required


def test_depth_bands_and_imbalance_are_defined_or_none():
    v = seeded()
    view = v.view()
    band = view.depth_within_bps(20)
    assert band is not None and band[0] > 0 and band[1] > 0
    assert view.imbalance_k(1) is not None
    assert view.microprice is not None
    assert view.relative_spread is not None
    with pytest.raises(ValueError):
        view.imbalance_k(0)
