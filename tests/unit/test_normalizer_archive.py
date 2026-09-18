"""Normalisation (T16, convention taker), archive immuable et replay reproductible (T69, T70)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from okxq.data.archive import (
    dedup_key,
    event_inst_id,
    read_parquet_events,
    verify_dataset_checksum,
    write_jsonl_dataset,
    write_parquet_partition,
)
from okxq.data.normalizer import Normalizer, ms_to_datetime
from okxq.data.replay import ReplaySource, load_dataset, replay_order
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import DataQualityError
from okxq.domain.events import EventEnvelope

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
OKX = ROOT / "tests" / "fixtures" / "okx"
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def norm(clock: SimulatedClock | None = None) -> tuple[Normalizer, SimulatedClock]:
    c = clock or SimulatedClock(T0)
    return Normalizer(c, source="test", id_factory=lambda n: f"evt_test_{n:04d}"), c


def test_T16_closed_candle_and_intrabar_candle_are_not_confused():
    n, c = norm()
    raw = {
        "arg": {"channel": "candle1m", "instId": "BTC-USDT-SWAP"},
        "data": [["1789732800000", "1", "2", "0.5", "1.5", "10", "0.1", "1500", "0"]],
    }
    partial = n.normalize_ws(raw)[0]
    assert partial.event_type == "candle.1m" and partial.payload["confirm"] == "0"
    c.advance(timedelta(minutes=1))
    raw["data"][0][8] = "1"
    closed = n.normalize_ws(raw)[0]
    assert closed.payload["confirm"] == "1"
    # même bougie économique, deux disponibilités différentes : la clôture n'est pas connue avant
    assert closed.exchange_ts == partial.exchange_ts
    assert closed.available_at > partial.available_at
    bad = dict(raw)
    bad["data"] = [["1789732800000", "1", "2", "0.5", "1.5", "10", "0.1", "1500", "2"]]
    assert n.normalize_ws(bad) == []
    assert any(r["reason"] == "BOUGIE_INVALIDE" for r in n.rejected)


def test_trade_side_is_the_taker_side_from_official_fixture():
    raw = json.loads((OKX / "ws_trades.json").read_text())
    message = raw.get("message") or raw["messages"][0]
    n, _ = norm()
    events = n.normalize_ws(message)
    assert events
    for event, row in zip(events, message["data"], strict=True):
        assert event.payload["side"] == row["side"]  # côté TAKER, jamais inversé
        assert event.payload["qty_contracts"] == row["sz"]
        assert isinstance(event.payload["price"], str)  # décimal en chaîne


def test_missing_timestamp_stays_none_and_never_becomes_collection_time():
    assert ms_to_datetime(None) is None
    assert ms_to_datetime("") is None
    with pytest.raises(DataQualityError):
        ms_to_datetime("pas-un-nombre")
    n, _ = norm()
    events = n.normalize_ws(
        {"arg": {"channel": "mark-price", "instId": "X-USDT-SWAP"}, "data": [{"markPx": "1", "ts": ""}]}
    )
    assert events[0].exchange_ts is None
    assert events[0].receive_ts == T0  # l'heure de réception est réelle, distincte de l'absence


def test_private_channel_on_public_normalizer_is_a_wiring_error():
    n, _ = norm()
    with pytest.raises(DataQualityError):
        n.normalize_ws({"arg": {"channel": "orders", "instId": "X"}, "data": [{}]})


def test_ingest_seq_is_monotonic_and_breaks_ties():
    n, _clock = norm()
    for _ in range(3):
        n.normalize_ws(
            {
                "arg": {"channel": "mark-price", "instId": "X-USDT-SWAP"},
                "data": [{"markPx": "1", "ts": "1789732800000"}],
            }
        )
    ordered = replay_order([])
    assert ordered == []
    events = []
    for i in range(3):
        events.extend(
            n.normalize_ws(
                {
                    "arg": {"channel": "mark-price", "instId": f"Y{i}-USDT-SWAP"},
                    "data": [{"markPx": "1", "ts": "1789732800000"}],
                }
            )
        )
    seqs = [e.ingest_seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    # même available_at : le départage est ingest_seq (ordre de réception enregistré)
    assert [e.ingest_seq for e in replay_order(list(reversed(events)))] == seqs


def test_archive_jsonl_roundtrip_dedup_and_checksum(tmp_path: Path):
    n, c = norm()
    events = n.normalize_ws(
        {
            "arg": {"channel": "mark-price", "instId": "X-USDT-SWAP"},
            "data": [{"markPx": "1", "ts": "1789732800000"}],
        }
    )
    duplicated = list(events) + list(events)  # livraison « au moins une fois »
    manifest = write_jsonl_dataset(duplicated, tmp_path, dataset="t", clock=c, quality_level="synthetic")
    assert manifest["rows"] == 1  # dédupliqué
    assert dedup_key(events[0]) == dedup_key(events[0])
    read_manifest, read_events = load_dataset(tmp_path)
    assert read_manifest["sha256"] == manifest["sha256"]
    assert [e.event_id for e in read_events] == [events[0].event_id]
    # un fichier modifié après écriture est refusé
    (tmp_path / "events.jsonl").write_text("{}\n")
    with pytest.raises(DataQualityError):
        verify_dataset_checksum(tmp_path, manifest)


def test_archive_parquet_partitions_are_immutable(tmp_path: Path):
    n, c = norm()
    events = n.normalize_ws(
        {
            "arg": {"channel": "trades", "instId": "X-USDT-SWAP"},
            "data": [
                {
                    "instId": "X-USDT-SWAP",
                    "tradeId": "1",
                    "px": "10.5",
                    "sz": "3",
                    "side": "buy",
                    "ts": "1789732800000",
                }
            ],
        }
    )
    records = write_parquet_partition(events, tmp_path, dataset="raw", clock=c)
    assert len(records) == 1 and records[0].rows == 1 and records[0].checksum_sha256
    with pytest.raises(DataQualityError):
        write_parquet_partition(events, tmp_path, dataset="raw", clock=c)  # pas d'écrasement
    back = read_parquet_events([tmp_path / "raw" / f"{records[0].partition_key}.parquet"])
    assert back[0].payload["price"] == "10.5"  # décimal préservé comme chaîne
    assert back[0].event_id == events[0].event_id


def test_T69_golden_dataset_replays_offline_with_verified_checksum():
    manifest, events = load_dataset(GOLDEN, verify=True)
    assert manifest["quality_level"] == "synthetic"  # jamais présenté comme de la donnée réelle
    assert manifest["rows"] == len(events) > 500
    source = ReplaySource.from_directory(GOLDEN)
    seen = 0
    previous = None
    for envelope in source.iterate():
        assert envelope.available_at <= source.clock.now_utc()
        if previous is not None:
            assert (envelope.available_at, envelope.ingest_seq) >= previous
        previous = (envelope.available_at, envelope.ingest_seq)
        seen += 1
    assert seen == len(events)
    assert event_inst_id(events[0]) != ""


def test_admissible_orders_expose_tie_sensitivity():
    source = ReplaySource.from_directory(GOLDEN)
    orders = source.admissible_orders(limit=2)
    assert len(orders) == 2
    assert [e.event_id for e in orders[0]] != [e.event_id for e in orders[1]]
    assert sorted(e.event_id for e in orders[0]) == sorted(e.event_id for e in orders[1])


def test_dataset_without_manifest_or_with_unknown_format_is_refused(tmp_path: Path):
    with pytest.raises(DataQualityError):
        load_dataset(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({"format": "csv", "sha256": "x", "files": []}))
    with pytest.raises(DataQualityError):
        load_dataset(tmp_path, verify=False)


def test_rest_book_snapshot_is_tagged_as_rest_channel():
    n, _ = norm()
    raw = json.loads((OKX / "rest_books.json").read_text())
    data = raw["response"]["data"] if "response" in raw else raw["data"]
    events = n.normalize_rest("market/books", data, inst_id="BTC-USDT-SWAP")
    assert events and events[0].payload["channel"] == "rest:books"
    assert isinstance(events[0], EventEnvelope)
