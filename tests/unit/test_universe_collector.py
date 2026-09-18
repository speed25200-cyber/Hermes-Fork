"""Univers point-in-time (unité commune, hystérésis, biais qualifié) et collecteur public borné."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest


from okxq.data.collector import CollectorStats, PublicCollector, backoff_delay
from okxq.data.point_in_time import PointInTimeStore
from okxq.data.replay import load_dataset
from okxq.data.universe import HYSTERESIS_KEEP, UniverseBuilder, build_universe, refresh_due, rotation_report
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import ConfigError
from okxq.exchange.okx.websocket_public import SubscriptionArg

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def golden_store() -> tuple[PointInTimeStore, datetime]:
    _, events = load_dataset(GOLDEN)
    return PointInTimeStore.from_events(events), events[-1].available_at


def test_universe_ranks_by_common_unit_not_coin_count(golden_store, smoke_config):
    store, cutoff = golden_store
    version, decisions = build_universe(smoke_config.universe, store, cutoff)
    assert set(version.eligible) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    assert version.bias_note is None
    by_id = {d.inst_id: d for d in decisions}
    # Le critère est un NOTIONNEL : volCcy24h (base) × prix, jamais un nombre de pièces.
    assert "volCcy24h" in version.criteria["volume_unit"] or "base" in version.criteria["volume_unit"]
    btc = by_id["BTC-USDT-SWAP"].metrics.notional_volume_24h_usdt
    eth = by_id["ETH-USDT-SWAP"].metrics.notional_volume_24h_usdt
    assert btc is not None and eth is not None and btc > eth  # BTC échange plus d'argent
    assert version.eligible[0] == "BTC-USDT-SWAP"
    # Chaque candidat a un verdict, admis compris (un critère qui n'explique que ses refus est aveugle).
    assert all(d.reasons == () for d in decisions if d.admitted)


def test_universe_declares_bias_when_too_few_admissible(golden_store, paper_config):
    store, cutoff = golden_store
    # Le profil PAPER exige 10 instruments minimum : deux ne suffisent pas, et cela doit être DIT.
    version, _ = build_universe(paper_config.universe, store, cutoff)
    assert version.bias_note is not None and "minimum" in version.bias_note


def test_universe_rejects_and_names_reasons(golden_store, smoke_config):
    store, cutoff = golden_store
    demanding = smoke_config.universe.model_copy(
        update={"min_notional_volume_24h_usdt": "1000000000000", "minimum_history_days": 30}
    )
    version, decisions = build_universe(demanding, store, cutoff)
    assert version.eligible == () and version.bias_note is not None
    reasons = " ".join(r for d in decisions for r in d.reasons)
    assert "volume notionnel" in reasons and "historique" in reasons


def test_hysteresis_keeps_an_incumbent_a_reject_would_lose(golden_store, smoke_config):
    store, cutoff = golden_store
    builder = UniverseBuilder(smoke_config.universe)
    metrics = builder.measure(store, "BTC-USDT-SWAP", store.instruments_at(cutoff)["BTC-USDT-SWAP"], cutoff)
    assert metrics.notional_volume_24h_usdt is not None
    # Seuil placé juste au-dessus du volume mesuré : un nouveau candidat est refusé…
    threshold = metrics.notional_volume_24h_usdt * Decimal("1.05")
    cfg = smoke_config.universe.model_copy(update={"min_notional_volume_24h_usdt": format(threshold, "f")})
    tight = UniverseBuilder(cfg)
    spec = store.instruments_at(cutoff)["BTC-USDT-SWAP"]
    assert not tight.judge(metrics, spec, incumbent=False).admitted
    # …alors qu'un sortant est conservé grâce à l'hystérésis (0,8 × seuil)
    assert threshold * HYSTERESIS_KEEP < metrics.notional_volume_24h_usdt
    assert tight.judge(metrics, spec, incumbent=True).admitted


def test_rotation_and_refresh_are_explicit(golden_store, smoke_config):
    store, cutoff = golden_store
    v1, _ = build_universe(smoke_config.universe, store, cutoff)
    v2, _ = build_universe(smoke_config.universe, store, cutoff, held=["OLD-USDT-SWAP"])
    report = rotation_report(v1, v2)
    assert report["kept"] == sorted(v1.eligible) and report["entered"] == [] and report["left"] == []
    assert report["held_only"] == ["OLD-USDT-SWAP"]  # détenu hors univers : suivi, reduce-only
    assert refresh_due(None, cutoff, smoke_config.universe) is True
    assert refresh_due(v1, cutoff, smoke_config.universe) is False
    assert (
        refresh_due(
            v1,
            cutoff + timedelta(seconds=smoke_config.universe.refresh_interval_seconds),
            smoke_config.universe,
        )
        is True
    )


# --- collecteur -----------------------------------------------------------------------------------------


class FakeConnection:
    def __init__(self, messages: list[str], *, fail_after: int | None = None) -> None:
        self.messages = messages
        self.sent: list[str] = []
        self.closed = False
        self._fail_after = fail_after

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    def __aiter__(self) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            for index, message in enumerate(self.messages):
                if self._fail_after is not None and index == self._fail_after:
                    raise ConnectionResetError("flux coupé")
                yield message

        return gen()

    async def close(self) -> None:
        self.closed = True


def snapshot_msg(seq: int = 100) -> str:
    return (
        '{"arg":{"channel":"books","instId":"BTC-USDT-SWAP"},"action":"snapshot","data":[{'
        f'"bids":[["100.0","5","0","1"]],"asks":[["100.1","4","0","1"]],"ts":"1789732800000","checksum":0,"prevSeqId":-1,"seqId":{seq}'
        "}]}"
    )


def update_msg(seq: int, prev: int) -> str:
    return (
        '{"arg":{"channel":"books","instId":"BTC-USDT-SWAP"},"action":"update","data":[{'
        f'"bids":[["99.9","2","0","1"]],"asks":[],"ts":"1789732800500","checksum":0,"prevSeqId":{prev},"seqId":{seq}'
        "}]}"
    )


def make_collector(
    messages: list[str], *, fail_after: int | None = None, **kwargs
) -> tuple[PublicCollector, list[FakeConnection]]:
    clock = SimulatedClock(T0)
    connections: list[FakeConnection] = []

    async def connect(url: str) -> FakeConnection:
        # Seule la PREMIÈRE session échoue : la reconnexion doit ensuite réussir.
        conn = FakeConnection(messages, fail_after=fail_after if not connections else None)
        connections.append(conn)
        return conn

    collector = PublicCollector(
        clock=clock,
        subscriptions=[SubscriptionArg("books", "BTC-USDT-SWAP")],
        connect=connect,
        url="wss://test/public",
        **kwargs,
    )
    return collector, connections


async def test_collector_publishes_valid_books_and_requests_resync():
    collector, connections = make_collector([snapshot_msg(), update_msg(101, 100), update_msg(200, 150)])
    await collector.run_once()
    assert connections[0].closed
    assert "subscribe" in connections[0].sent[0]
    published = [collector.queue.get_nowait() for _ in range(collector.queue.qsize())]
    assert [e.event_type for e in published] == ["book.snapshot", "book.update"]  # le troisième est refusé
    assert collector.stats.resyncs_requested == 1
    assert ("BTC-USDT-SWAP", "books") in collector.resync_needed
    health = collector.health()
    assert health["quality"]["gaps"] == 1
    assert health["books"]["BTC-USDT-SWAP|books"] is False


async def test_collector_queue_is_bounded_and_drops_are_counted():
    collector, _ = make_collector([snapshot_msg(), update_msg(101, 100)], queue_maxsize=1)
    await collector.run_once()
    assert collector.queue.qsize() == 1
    assert collector.stats.dropped == 1  # jamais un blocage silencieux
    assert collector.quality.counters.drops == 1


async def test_collector_refuses_private_channels_structurally():
    async def connect(url: str) -> FakeConnection:
        return FakeConnection([])

    with pytest.raises(ConfigError):
        PublicCollector(
            clock=SimulatedClock(T0),
            subscriptions=[SubscriptionArg("orders", "BTC-USDT-SWAP")],
            connect=connect,
            url="wss://test/public",
        )


async def test_collector_reconnects_with_bounded_backoff():
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    collector, connections = make_collector(
        [snapshot_msg(), update_msg(101, 100)], fail_after=1, max_reconnects=2, sleep=sleep
    )
    stop = asyncio.Event()
    await collector.run(stop)
    assert collector.stats.reconnects == 2
    assert delays and all(0 < d <= 30.0 for d in delays)
    assert backoff_delay(0) <= backoff_delay(5) <= 30.0
    assert len(connections) >= 2
    assert "subscribe" in connections[-1].sent[0]


async def test_collector_counts_pong_events_and_errors():
    collector, _ = make_collector(
        [
            "pong",
            '{"event":"subscribe","arg":{"channel":"books","instId":"BTC-USDT-SWAP"}}',
            '{"event":"error","code":"60012","msg":"invalid request"}',
            snapshot_msg(),
        ]
    )
    await collector.run_once()
    stats: CollectorStats = collector.stats
    assert stats.pongs == 1 and stats.subscriptions_confirmed == 1 and stats.errors == 1
    assert stats.events == 1 and stats.by_event_type == {"book.snapshot": 1}
