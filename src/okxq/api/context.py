"""Contexte partagé par les routes (posé sur ``app.state.ctx`` par ``create_app``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from okxq import __version__
from okxq.api.auth import AuditTrail
from okxq.api.readmodel import ReadModel
from okxq.api.sse import EventBus, JournalPoller
from okxq.config.schema import AppConfig
from okxq.domain.clocks import Clock
from okxq.runtime.alerts import AlertManager
from okxq.runtime.health import HealthRegistry
from okxq.runtime.metrics import Metrics


@dataclass(slots=True)
class ApiContext:
    cfg: AppConfig
    session_factory: sessionmaker[Session]
    readmodel: ReadModel
    health: HealthRegistry
    metrics: Metrics
    bus: EventBus
    audit: AuditTrail
    clock: Clock
    alerts: AlertManager | None
    poller: JournalPoller
    frontend_dir: Path | None
    synthetic_data: bool
    code_commit: str | None = None
    software_version: str = __version__
    started_at_iso: str = ""
    role_keys_present: bool = False
    extra: dict[str, str] = field(default_factory=dict)


def get_ctx(request: Request) -> ApiContext:
    ctx = request.app.state.ctx
    assert isinstance(ctx, ApiContext)
    return ctx
