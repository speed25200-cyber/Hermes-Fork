"""État du système : mode (toujours présent), versions, leadership, halts, composants ; métriques."""

from __future__ import annotations

import platform
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from okxq.api.auth import Principal, Role, require_role
from okxq.api.context import get_ctx
from okxq.api.serialize import as_iso, to_jsonable

router = APIRouter(tags=["système"])


@router.get("/api/v1/system/status", summary="Mode, versions, leadership, halts et composants")
def system_status(
    request: Request, principal: Principal = Depends(require_role(Role.READER))
) -> dict[str, Any]:
    ctx = get_ctx(request)
    rm = ctx.readmodel
    rs = rm.risk_state()
    leases = [
        {
            "lease_name": lease.lease_name,
            "holder": lease.holder,
            "fencing_token": lease.fencing_token,
            "acquired_at": as_iso(lease.acquired_at),
            "heartbeat_at": as_iso(lease.heartbeat_at),
            "expires_at": as_iso(lease.expires_at),
            "active": bool(lease.expires_at and lease.expires_at > ctx.clock.now_utc()),
        }
        for lease in rm.leases()
    ]
    writer = next((lease for lease in leases if lease["lease_name"] == "execution_writer"), None)
    ready, health = ctx.health.readiness()
    return {
        "ok": True,
        "mode": ctx.cfg.mode.value,
        "live_enabled": ctx.cfg.project.live_enabled,
        "is_live": ctx.cfg.is_live,
        "synthetic_data": ctx.synthetic_data,
        "account_scope": ctx.cfg.account.scope,
        "role": principal.role.value,
        "versions": {
            "software": ctx.software_version,
            "python": platform.python_version(),
            "config_hash": ctx.cfg.config_hash,
            "config_schema": ctx.cfg.project.schema_version,
            "code_commit": ctx.code_commit,
            "execution_policy": ctx.cfg.execution.execution_policy_version,
        },
        "leadership": {
            "single_writer_required": ctx.cfg.runtime.require_single_execution_writer,
            "execution_writer": writer,
            "leases": leases,
        },
        "halts": None
        if rs is None
        else {
            "halt_level": rs.halt_level,
            "halt_reason": rs.halt_reason,
            "halt_since": as_iso(rs.halt_since),
            "day_realized_loss": to_jsonable(rs.day_realized_loss),
            "day_start_equity": to_jsonable(rs.day_start_equity),
            "high_water_mark_equity": to_jsonable(rs.high_water_mark_equity),
            "limits_version": rs.limits_version,
            "updated_at": as_iso(rs.updated_at),
        },
        "components": health["components"],
        "ready": ready,
        "readiness_reasons": health["reasons"],
        "alerts_active": [a.to_dict() for a in ctx.alerts.active()] if ctx.alerts else None,
        "started_at": ctx.started_at_iso,
        "ts": as_iso(ctx.clock.now_utc()),
    }


@router.get("/metrics", summary="Exposition Prometheus (rôle reader)", include_in_schema=True)
def metrics(request: Request, _principal: Principal = Depends(require_role(Role.READER))) -> Response:
    ctx = get_ctx(request)
    if not ctx.cfg.api.metrics_enabled:
        return Response("métriques désactivées\n", status_code=404, media_type="text/plain; charset=utf-8")
    return Response(ctx.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")
