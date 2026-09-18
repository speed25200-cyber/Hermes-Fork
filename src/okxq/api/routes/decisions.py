"""Décisions par minute : candidats, avantage brut/net, coûts, incertitude, raisons de rejet stables et
liens de provenance (instantané, modèle, univers, équité, cibles, intentions)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from okxq.api.auth import Principal, Role, require_role
from okxq.api.context import get_ctx
from okxq.api.readmodel import clamp
from okxq.api.serialize import as_iso, to_jsonable
from okxq.domain.reasons import ReasonCode
from okxq.persistence.models import Decision

router = APIRouter(tags=["décisions"])

STABLE_REASONS: frozenset[str] = frozenset(r.value for r in ReasonCode)


def _reason(code: Any) -> dict[str, Any]:
    c = str(code)
    return {"code": c, "stable": c in STABLE_REASONS}


def _candidate(edge: dict[str, Any]) -> dict[str, Any]:
    components = edge.get("components") or {}
    return {
        "edge_id": edge.get("edge_id"),
        "forecast_id": edge.get("forecast_id"),
        "instrument": edge.get("instrument"),
        "side": edge.get("side"),
        "horizon_s": edge.get("horizon_s"),
        "cost_basis": edge.get("cost_basis"),
        "gross_return": edge.get("expected_gross_return"),
        "net_edge": edge.get("net_edge"),
        "costs": to_jsonable(components),
        "included_in_price": list(edge.get("included_in_price") or []),
        "uncertainty": edge.get("uncertainty"),
        "uncertainty_penalty": edge.get("uncertainty_penalty"),
        "edge_score": edge.get("edge_score"),
        "expires_at": edge.get("expires_at"),
    }


def _rejected(alt: dict[str, Any]) -> dict[str, Any]:
    codes = alt.get("reason_codes") or ([alt["reason_code"]] if alt.get("reason_code") else [])
    return {**to_jsonable(alt), "reasons": [_reason(c) for c in codes]}


def decision_to_dict(d: Decision, links: dict[str, list[str]] | None = None) -> dict[str, Any]:
    links = links or {"target_ids": [], "intent_ids": []}
    return {
        "decision_id": d.decision_id,
        "mode": d.mode,
        "cutoff_at": as_iso(d.cutoff_at),
        "started_at": as_iso(d.started_at),
        "finished_at": as_iso(d.finished_at),
        "outcome": d.outcome,
        "reasons": [_reason(c) for c in d.reason_codes],
        "reason_codes": list(d.reason_codes),
        "candidates": [_candidate(e) for e in d.edges],
        "forecasts": to_jsonable(d.forecasts),
        "rejected_alternatives": [_rejected(a) for a in d.rejected_alternatives],
        "timings_ms": to_jsonable(d.timings_ms),
        "provenance": {
            "snapshot_id": d.snapshot_id,
            "model_id": d.model_id,
            "universe_version": d.universe_version,
            "equity_version": d.equity_version,
            "inputs_hash": d.inputs_hash,
            "software_version": d.software_version,
            "trace_id": d.trace_id,
            "target_ids": links["target_ids"],
            "intent_ids": links["intent_ids"],
        },
    }


@router.get("/api/v1/decisions", summary="Décisions (les plus récentes d'abord)")
def decisions(
    request: Request,
    _p: Principal = Depends(require_role(Role.READER)),
    limit: int = Query(60, ge=1, le=500),
    since: datetime | None = Query(None),
    outcome: str | None = Query(None, description="TRADE / NO_TRADE / SKIPPED / FAILED"),
    instrument: str | None = Query(None),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    rows = ctx.readmodel.decisions(
        limit=clamp(limit, 60), since=since, outcome=outcome, instrument=instrument
    )
    links = ctx.readmodel.decision_links([d.decision_id for d in rows])
    return {
        "ok": True,
        "mode": ctx.cfg.mode.value,
        "count": len(rows),
        "reason_catalog": sorted(STABLE_REASONS),
        "items": [decision_to_dict(d, links.get(d.decision_id)) for d in rows],
    }


@router.get("/api/v1/decisions/{decision_id}", summary="Une décision avec ses cibles de portefeuille")
def decision_detail(
    request: Request, decision_id: str, _p: Principal = Depends(require_role(Role.READER))
) -> dict[str, Any]:
    ctx = get_ctx(request)
    d = ctx.readmodel.decision(decision_id)
    if d is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "DECISION_INCONNUE", "message": decision_id}
        )
    links = ctx.readmodel.decision_links([decision_id])[decision_id]
    targets = [
        {
            "target_id": t.target_id,
            "snapshot_id": t.snapshot_id,
            "equity_version": t.equity_version,
            "signed_weights": to_jsonable(t.signed_weights),
            "constraints_version": t.constraints_version,
            "solver_status": t.solver_status,
            "solver_report": to_jsonable(t.solver_report),
            "created_at": as_iso(t.created_at),
            "expires_at": as_iso(t.expires_at),
        }
        for t in ctx.readmodel.portfolio_targets(decision_id)
    ]
    return {"ok": True, "decision": decision_to_dict(d, links), "targets": targets}
