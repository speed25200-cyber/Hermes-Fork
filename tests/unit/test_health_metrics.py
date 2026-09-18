"""Santé (liveness ≠ readiness) et métriques (§61 : noms exacts, labels bornés)."""

from __future__ import annotations

from datetime import timedelta

from okxq.runtime.health import GATEWAY, HealthRegistry, Status
from okxq.runtime.metrics import METRIC_NAMES, Metrics, bounded_label

from okxq.domain.clocks import SimulatedClock


def test_liveness_is_always_alive_but_readiness_requires_components(clock: SimulatedClock) -> None:
    h = HealthRegistry(clock=clock, mode="PAPER")
    assert h.liveness()["status"] == "alive"
    ready, detail = h.readiness()
    assert ready is True and detail["mode"] == "PAPER"
    h.set("database", Status.FAULT, "connexion refusée")
    ready, detail = h.readiness()
    assert ready is False
    assert any(r.startswith("database") for r in detail["reasons"])
    h.set("database", Status.OK)
    assert h.readiness()[0] is True


def test_T60_gateway_connected_but_not_reconciled_is_not_ready(clock: SimulatedClock) -> None:
    h = HealthRegistry(clock=clock, mode="DEMO")
    h.set_gateway(connected=True, reconciled=False)
    ready, detail = h.readiness()
    assert ready is False
    assert detail["components"][GATEWAY]["status"] == "WARN"
    assert "réconcili" in " ".join(detail["reasons"])
    h.set_gateway(connected=True, reconciled=True)
    assert h.readiness()[0] is True
    h.set_gateway(connected=False, reconciled=False)
    ready, detail = h.readiness()
    assert ready is False and detail["components"][GATEWAY]["status"] == "FAULT"


def test_stale_component_becomes_fault_and_blocks_readiness(clock: SimulatedClock) -> None:
    h = HealthRegistry(clock=clock, stale_after_seconds=30, mode="PAPER")
    h.set("market_data", Status.OK, "flux vivant")
    clock.advance(timedelta(seconds=31))
    ready, detail = h.readiness()
    assert ready is False
    assert detail["components"]["market_data"]["status"] == "FAULT"
    assert "muet" in detail["components"]["market_data"]["info"]
    h.touch("market_data")
    assert h.readiness()[0] is True


def test_health_tick_matches_hermes_interface_format(clock: SimulatedClock) -> None:
    h = HealthRegistry(clock=clock, mode="SHADOW")
    h.set("strategy", "ok", "boucle 60 s")
    h.set("jev", Status.WARN, "circuit ouvert", required=False)
    tick = h.health_tick()
    assert set(tick) >= {"modules", "ts", "mode"}
    assert tick["modules"]["strategy"] == {"status": "OK", "info": "boucle 60 s"}
    assert tick["modules"]["jev"]["status"] == "WARN"
    assert all(m["status"] in ("OK", "WARN", "FAULT") for m in tick["modules"].values())


def test_all_metrics_declared_with_exact_names() -> None:
    m = Metrics()
    names = m.collected_names()
    missing = [n for n in METRIC_NAMES if n not in names]
    assert not missing, missing
    rendered = m.render().decode()
    for n in METRIC_NAMES:
        assert n in rendered
    assert "# TYPE book_sequence_gaps_total counter" in rendered
    assert "# TYPE db_write_failures gauge" in rendered
    assert "# TYPE feature_compute_ms histogram" in rendered


def test_labels_are_bounded_and_halt_state_encoded() -> None:
    assert bounded_label("channel", "books") == "books"
    assert bounded_label("channel", "BTC-USDT-SWAP") == "other"
    assert bounded_label("reason", None) == "other"
    assert bounded_label("inconnu", "x") == "other"
    m = Metrics()
    m.book_invalid_total.labels(reason=bounded_label("reason", "crossed")).inc()
    m.book_invalid_total.labels(reason=bounded_label("reason", "ord_123456")).inc()
    out = m.render().decode()
    assert 'book_invalid_total{reason="other"} 1.0' in out
    assert "ord_123456" not in out
    m.set_halt_state("ENTRIES_HALTED")
    assert "halt_state 1.0" in m.render().decode()
    m.set_halt_state("n'importe quoi")
    assert "halt_state 3.0" in m.render().decode()
