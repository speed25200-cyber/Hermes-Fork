"""Alertes : hystérésis, déduplication, destinataires, puits local et webhook non présumé."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from okxq.runtime.alerts import (
    DEFAULT_RULES,
    AlertManager,
    AlertRule,
    LocalJsonlSink,
    Priority,
    WebhookSink,
    build_default_manager,
)

from okxq.domain.clocks import SimulatedClock


class MemorySink:
    name = "memory"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, alert: Any, now: Any) -> Any:
        from okxq.runtime.alerts import Delivery

        self.sent.append(alert.to_dict())
        return Delivery(self.name, alert.alert_id, True, "ok", now)


def _rule() -> AlertRule:
    return AlertRule(
        name="stale",
        metric="market_data_age_seconds",
        threshold=2.0,
        clear_threshold=1.5,
        direction="above",
        priority=Priority.P2,
        title="Données obsolètes",
    )


def test_hysteresis_fires_once_and_resolves_only_below_clear_threshold(clock: SimulatedClock) -> None:
    sink = MemorySink()
    m = AlertManager(rules=[_rule()], sinks=[sink], clock=clock)
    assert m.observe("market_data_age_seconds", 1.0) == []
    fired = m.observe("market_data_age_seconds", 2.5)
    assert len(fired) == 1 and fired[0].state == "FIRING" and fired[0].priority is Priority.P2
    assert m.observe("market_data_age_seconds", 3.0) == []  # déjà en cours
    assert m.observe("market_data_age_seconds", 1.8) == []  # entre les deux seuils : rien ne bouge
    resolved = m.observe("market_data_age_seconds", 1.4)
    assert len(resolved) == 1 and resolved[0].state == "RESOLVED"
    assert m.active() == []
    assert [s["state"] for s in sink.sent] == ["FIRING", "RESOLVED"]


def test_dedup_window_and_escalation(clock: SimulatedClock) -> None:
    sink = MemorySink()
    m = AlertManager(sinks=[sink], dedup_seconds=300, clock=clock)
    assert m.raise_alert("k", Priority.P3, "t", "m") is not None
    assert m.raise_alert("k", Priority.P3, "t", "m") is None
    clock.advance(timedelta(seconds=100))
    assert m.raise_alert("k", Priority.P3, "t", "m") is None
    assert m.raise_alert("k", Priority.P1, "t", "aggravation") is not None  # une aggravation passe
    clock.advance(timedelta(seconds=301))
    assert m.raise_alert("k", Priority.P3, "t", "m") is not None
    assert len(sink.sent) == 3


def test_recipients_follow_priority(clock: SimulatedClock) -> None:
    sink = MemorySink()
    m = AlertManager(
        sinks=[sink],
        clock=clock,
        recipients={Priority.P1: ["astreinte@example", "sms:+41"], Priority.P4: ["journal"]},
    )
    a = m.raise_alert("halt", Priority.P1, "Halt", "critique")
    assert a is not None and a.recipients == ["astreinte@example", "sms:+41"]
    b = m.raise_alert("info", Priority.P4, "Info", "rien")
    assert b is not None and b.recipients == ["journal"]
    c = m.raise_alert("moyen", Priority.P3, "Moyen", "x")
    assert c is not None and c.recipients == []


def test_local_jsonl_sink_writes_one_line_per_alert(tmp_path: Path, clock: SimulatedClock) -> None:
    path = tmp_path / "alerts" / "alerts.jsonl"
    m = AlertManager(sinks=[LocalJsonlSink(path)], clock=clock)
    m.raise_alert("a", Priority.P2, "A", "un")
    m.raise_alert("b", Priority.P2, "B", "deux")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["key"] for line in lines] == ["a", "b"]
    assert all(d.delivered for d in m.deliveries)


def test_webhook_is_not_sent_without_url_and_only_confirmed_on_2xx(
    clock: SimulatedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    calls: list[tuple[str, dict[str, Any]]] = []

    def transport(url: str, payload: dict[str, Any]) -> int:
        calls.append((url, payload))
        return 500 if payload["key"] == "ko" else 204

    absent = WebhookSink(transport=transport)
    m = AlertManager(sinks=[absent], clock=clock)
    m.raise_alert("x", Priority.P1, "X", "x")
    assert calls == []
    assert m.deliveries[0].delivered is False and "absent" in m.deliveries[0].detail

    configured = WebhookSink(url="https://hooks.example/okxq", transport=transport)
    m2 = AlertManager(sinks=[configured], clock=clock)
    m2.raise_alert("ok", Priority.P1, "OK", "x")
    m2.raise_alert("ko", Priority.P1, "KO", "x")
    assert [d.delivered for d in m2.deliveries] == [True, False]
    assert calls[0][0] == "https://hooks.example/okxq"
    assert "non confirmé" in m2.deliveries[1].detail


def test_default_manager_has_p1_rules_and_local_sink(tmp_path: Path, clock: SimulatedClock) -> None:
    m = build_default_manager(sink_path=tmp_path / "alerts.jsonl", alert_sink="local", clock=clock)
    assert any(r.priority is Priority.P1 for r in DEFAULT_RULES)
    fired = m.observe("unknown_orders_total", 1)
    assert fired and fired[0].priority is Priority.P1
    assert (tmp_path / "alerts.jsonl").exists()
