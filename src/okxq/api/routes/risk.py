"""Risque et qualité des données (lecture) : incidents, état de halt, événements de qualité."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from okxq.api.auth import Principal, Role, require_role
from okxq.api.context import get_ctx
from okxq.api.readmodel import clamp
from okxq.api.serialize import as_iso, to_jsonable
from okxq.persistence.models import DataQualityEvent, RiskEventRow, RiskState

router = APIRouter(tags=["risque"])


def risk_event_to_dict(r: RiskEventRow) -> dict[str, Any]:
    return {
        "event_id": r.event_id,
        "created_at": as_iso(r.created_at),
        "severity": r.severity,
        "reason_code": r.reason_code,
        "affected_scope": r.affected_scope,
        "evidence": to_jsonable(r.evidence),
        "requested_action": r.requested_action,
        "observed_result": r.observed_result,
    }


def risk_state_to_dict(rs: RiskState | None) -> dict[str, Any] | None:
    if rs is None:
        return None
    return {
        "account_scope": rs.account_scope,
        "halt_level": rs.halt_level,
        "halt_reason": rs.halt_reason,
        "halt_since": as_iso(rs.halt_since),
        "utc_day": as_iso(rs.utc_day),
        "day_start_equity": to_jsonable(rs.day_start_equity),
        "day_realized_loss": to_jsonable(rs.day_realized_loss),
        "high_water_mark_unit": to_jsonable(rs.high_water_mark_unit),
        "high_water_mark_equity": to_jsonable(rs.high_water_mark_equity),
        "limits_version": rs.limits_version,
        "updated_at": as_iso(rs.updated_at),
        "version": rs.version,
    }


def dq_event_to_dict(e: DataQualityEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "occurred_at": as_iso(e.occurred_at),
        "inst_id": e.inst_id,
        "channel": e.channel,
        "kind": e.kind,
        "severity": e.severity,
        "details": to_jsonable(e.details),
    }


@router.get("/api/v1/risk/events", summary="Événements de risque (les plus récents d'abord)")
def risk_events(
    request: Request,
    _p: Principal = Depends(require_role(Role.READER)),
    limit: int = Query(100, ge=1, le=500),
    severity: str | None = Query(None, description="INFO / WARN / CRITICAL"),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    rows = ctx.readmodel.risk_events(limit=clamp(limit), severity=severity)
    return {
        "ok": True,
        "mode": ctx.cfg.mode.value,
        "state": risk_state_to_dict(ctx.readmodel.risk_state()),
        "limits": to_jsonable(ctx.cfg.risk.model_dump()),
        "count": len(rows),
        "items": [risk_event_to_dict(r) for r in rows],
    }


@router.get("/api/v1/risk/state", summary="État de risque persistant (halts, pertes, high-water mark)")
def risk_state(request: Request, _p: Principal = Depends(require_role(Role.READER))) -> dict[str, Any]:
    ctx = get_ctx(request)
    return {"ok": True, "mode": ctx.cfg.mode.value, "state": risk_state_to_dict(ctx.readmodel.risk_state())}


@router.get("/api/v1/data/quality", summary="Événements de qualité des données")
def data_quality(
    request: Request,
    _p: Principal = Depends(require_role(Role.READER)),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    rows = ctx.readmodel.data_quality_events(limit=clamp(limit))
    return {"ok": True, "count": len(rows), "items": [dq_event_to_dict(e) for e in rows]}
