"""Point-in-time : une donnée future ne réécrit pas le passé, un événement tardif n'est pas antidaté
(T14, T15), un actif délisté quitte l'univers sans quitter la comptabilité (T18)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from okxq.data.point_in_time import PointInTimeStore, UniverseVersion
from okxq.data.quality import DataQualityTracker
from okxq.domain.errors import CausalityError
from okxq.domain.events import EventEnvelope, QualityFlag
from okxq.domain.instruments import InstrumentSpec, InstrumentState

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def env(
    event_type: str,
    inst: str | None,
    *,
    exchange_ts: datetime | None,
    available_at: datetime,
    seq: int,
    value: str = "1",
) -> EventEnvelope:
    payload = {"inst_id": inst, "value": value}
    return EventEnvelope(
        event_id=f"evt_{event_type}_{seq}",
        event_type=event_type,
        schema_version=1,
        source="test",
        exchange_ts=exchange_ts,
        receive_ts=available_at,
        available_at=available_at,
        ingest_seq=seq,
        payload_hash="h" * 64,
        payload=payload,
    )


def spec(inst: str, *, observed_at: datetime, state: InstrumentState, version: int = 1) -> InstrumentSpec:
    return InstrumentSpec(
        inst_id=inst,
        valid_from=T0 - timedelta(days=400),
        observed_at=observed_at,
        settle_ccy="USDT",
        base_ccy=inst.split("-")[0],
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=Decimal("0.01"),
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.1"),
        min_size=Decimal("0.1"),
        state=state,
        provenance="test",
        version=version,
    )


def test_T14_future_data_does_not_change_earlier_answers():
    store = PointInTimeStore()
    cutoff = T0 + timedelta(minutes=5)
    store.add_events([env("trade", "X", exchange_ts=T0, available_at=T0, seq=1, value="a")])
    before = store.latest_event("trade", "X", cutoff)
    count_before = len(store.events_until("trade", "X", cutoff))
    # on ajoute ensuite des données POSTÉRIEURES au cutoff
    store.add_events(
        [
            env(
                "trade",
                "X",
                exchange_ts=cutoff + timedelta(minutes=1),
                available_at=cutoff + timedelta(minutes=1),
                seq=2,
                value="b",
            ),
            env(
                "trade",
                "X",
                exchange_ts=cutoff + timedelta(minutes=2),
                available_at=cutoff + timedelta(minutes=2),
                seq=3,
                value="c",
            ),
        ]
    )
    assert store.latest_event("trade", "X", cutoff) is before
    assert len(store.events_until("trade", "X", cutoff)) == count_before
    assert len(store.events_until("trade", "X", cutoff + timedelta(minutes=10))) == 3


def test_T15_old_event_received_late_is_unavailable_before_its_reception():
    store = PointInTimeStore()
    economic = T0  # heure économique ancienne
    received = T0 + timedelta(minutes=30)  # découvert bien plus tard
    late = env("funding", "X", exchange_ts=economic, available_at=received, seq=1)
    store.add_event(late)
    # à un cutoff postérieur à l'heure économique mais antérieur à la réception : indisponible
    assert store.latest_event("funding", "X", T0 + timedelta(minutes=10)) is None
    assert store.latest_event("funding", "X", received) is late
    with pytest.raises(CausalityError):
        store.assert_available(late, T0 + timedelta(minutes=10))


def test_events_arriving_out_of_order_are_read_chronologically():
    store = PointInTimeStore()
    store.add_event(env("trade", "X", exchange_ts=T0, available_at=T0 + timedelta(seconds=30), seq=2))
    store.add_event(env("trade", "X", exchange_ts=T0, available_at=T0 + timedelta(seconds=10), seq=1))
    series = store.events_until("trade", "X", T0 + timedelta(minutes=1))
    assert [e.ingest_seq for e in series] == [1, 2]
    assert store.latest_event("trade", "X", T0 + timedelta(seconds=20)).ingest_seq == 1


def test_T04_instrument_versions_are_read_at_their_observation_date():
    store = PointInTimeStore()
    store.add_instrument_version(spec("X-USDT-SWAP", observed_at=T0, state=InstrumentState.LIVE, version=1))
    store.add_instrument_version(
        spec("X-USDT-SWAP", observed_at=T0 + timedelta(hours=2), state=InstrumentState.SUSPEND, version=2)
    )
    early = store.instrument_at("X-USDT-SWAP", T0 + timedelta(hours=1))
    assert early is not None and early.version == 1 and early.state is InstrumentState.LIVE
    later = store.instrument_at("X-USDT-SWAP", T0 + timedelta(hours=3))
    assert later is not None and later.version == 2
    assert store.instrument_at("X-USDT-SWAP", T0 - timedelta(hours=1)) is None


def test_T18_delisted_asset_leaves_universe_but_stays_accountable():
    store = PointInTimeStore()
    store.add_instrument_version(spec("A-USDT-SWAP", observed_at=T0, state=InstrumentState.LIVE))
    store.add_instrument_version(spec("B-USDT-SWAP", observed_at=T0, state=InstrumentState.LIVE))
    store.add_instrument_version(
        spec("B-USDT-SWAP", observed_at=T0 + timedelta(hours=1), state=InstrumentState.EXPIRED, version=2)
    )
    tradable = store.tradable_instruments_at(T0 + timedelta(hours=2))
    assert set(tradable) == {"A-USDT-SWAP"}
    # B reste connu (comptabilité) et gérable en reduce-only via held_only
    assert "B-USDT-SWAP" in store.instruments_at(T0 + timedelta(hours=2))
    version = UniverseVersion(
        universe_version="uni_1",
        valid_from=T0 + timedelta(hours=2),
        eligible=("A-USDT-SWAP",),
        held_only=("B-USDT-SWAP",),
    )
    store.add_universe_version(version)
    current = store.universe_at(T0 + timedelta(hours=3))
    assert current is not None
    assert current.manageable() == ("A-USDT-SWAP", "B-USDT-SWAP")


def test_quality_distinguishes_missing_stale_invalid_and_observed_zero():
    tracker = DataQualityTracker(max_age_ms=1_000, forward_fill_ms={"book": 2_000})
    assert tracker.read("book", "X", T0).flag is QualityFlag.MISSING
    tracker.observe("book", "X", 0, T0)  # zéro RÉELLEMENT observé
    fresh = tracker.read("book", "X", T0)
    assert fresh.flag is QualityFlag.OK and fresh.value == 0
    stale = tracker.read("book", "X", T0 + timedelta(milliseconds=1_500))
    assert stale.flag is QualityFlag.STALE and stale.value == 0
    gone = tracker.read("book", "X", T0 + timedelta(milliseconds=2_500))
    assert gone.flag is QualityFlag.MISSING and gone.value is None  # forward-fill épuisé, rien d'imputé
    tracker.mark_invalid("book", "X", "carnet croisé", T0)
    assert tracker.read("book", "X", T0).flag is QualityFlag.INVALID
    counters = tracker.counters.as_dict()
    assert counters["invalid"] == 1 and counters["missing_reads"] >= 2


def test_quality_age_and_counters_are_reported():
    tracker = DataQualityTracker(max_age_ms=1_000)
    tracker.observe("trade", "X", 1, T0)
    tracker.observe("trade", "Y", 1, T0 - timedelta(seconds=5))
    assert tracker.max_age_seconds("trade", T0) == pytest.approx(5.0)
    tracker.note_gap()
    tracker.note_drop()
    report = tracker.report(T0)
    assert report["counters"]["gaps"] == 1 and report["counters"]["drops"] == 1
    assert len(report["readings"]) == 2
