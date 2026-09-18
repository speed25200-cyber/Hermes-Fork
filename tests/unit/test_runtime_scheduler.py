import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from okxq.domain.clocks import SimulatedClock, floor_to_interval, next_boundary
from okxq.runtime.scheduler import DecisionScheduler

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def test_boundaries_are_utc_minute_aligned():
    t = datetime(2026, 9, 18, 12, 0, 37, 500000, tzinfo=UTC)
    assert floor_to_interval(t, 60) == T0
    assert next_boundary(t, 60) == T0 + timedelta(minutes=1)


async def test_skip_policy_never_overlaps(clock: SimulatedClock):
    started: list[datetime] = []
    release = asyncio.Event()

    async def cb(boundary, deadline):
        started.append(boundary)
        await release.wait()
        return "ok"

    s = DecisionScheduler(
        clock=clock, interval_seconds=60, budget_ms=3000, overlap_policy="skip", on_boundary=cb
    )
    assert await s.fire(T0) is True
    await asyncio.sleep(0)
    assert await s.fire(T0 + timedelta(minutes=1)) is False
    assert s.stats.skipped_overlap == 1
    release.set()
    await s.wait_idle()
    assert started == [T0]
    assert s.stats.decisions_completed == 1


async def test_coalesce_policy_runs_only_last_pending(clock: SimulatedClock):
    started: list[datetime] = []
    release = asyncio.Event()

    async def cb(boundary, deadline):
        started.append(boundary)
        if len(started) == 1:
            await release.wait()
        return "ok"

    s = DecisionScheduler(
        clock=clock, interval_seconds=60, budget_ms=3000, overlap_policy="coalesce", on_boundary=cb
    )
    await s.fire(T0)
    await asyncio.sleep(0)
    await s.fire(T0 + timedelta(minutes=1))
    await s.fire(T0 + timedelta(minutes=2))
    release.set()
    await s.wait_idle()
    await s.wait_idle()
    # une seule décision de rattrapage, sur la dernière frontière demandée : pas de rafale
    assert started == [T0, T0 + timedelta(minutes=2)]
    assert s.stats.coalesced == 2


async def test_deadline_miss_is_counted_and_failure_survives(clock: SimulatedClock):
    async def cb(boundary, deadline):
        clock.advance(timedelta(seconds=5))
        raise RuntimeError("boom")

    s = DecisionScheduler(
        clock=clock, interval_seconds=60, budget_ms=3000, overlap_policy="skip", on_boundary=cb
    )
    await s.fire(T0)
    await s.wait_idle()
    assert s.stats.decisions_failed == 1
    assert s.stats.deadline_misses == 1


async def test_missed_boundaries_are_not_replayed(clock: SimulatedClock):
    calls: list[datetime] = []

    async def cb(boundary, deadline):
        calls.append(boundary)
        return None

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        # le processus « dort » cinq minutes d'un coup ; on rend la main à la boucle d'événements
        clock.advance(timedelta(minutes=5, seconds=1))
        await asyncio.sleep(0)
        if len(sleeps) >= 4:
            stop.set()

    stop = asyncio.Event()
    s = DecisionScheduler(
        clock=clock,
        interval_seconds=60,
        budget_ms=3000,
        overlap_policy="skip",
        on_boundary=cb,
        sleep=fake_sleep,
    )

    await asyncio.wait_for(s.run(stop), timeout=5)
    assert s.stats.missed_boundaries >= 4
    assert len(calls) <= 2  # aucune rafale de rattrapage


def test_invalid_parameters(clock: SimulatedClock):
    async def cb(b, d):
        return None

    with pytest.raises(ValueError):
        DecisionScheduler(clock=clock, interval_seconds=0, budget_ms=1, overlap_policy="skip", on_boundary=cb)
