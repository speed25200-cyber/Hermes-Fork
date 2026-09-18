"""Recherche (lecture) : expériences, rapports d'évaluation, versions de modèles.

Un rapport n'est « validé » que s'il porte sur une période INDÉPENDANTE (``independent=true``) ; tout
le reste est marqué ``validated: false`` et l'interface l'affiche comme non validé.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from okxq.api.auth import Principal, Role, require_role
from okxq.api.context import ApiContext, get_ctx
from okxq.api.readmodel import clamp
from okxq.api.serialize import as_iso, to_jsonable
from okxq.persistence.models import EvaluationReport, ExperimentRun, ModelVersion

router = APIRouter(tags=["recherche"])


def report_to_dict(r: EvaluationReport) -> dict[str, Any]:
    return {
        "report_id": r.report_id,
        "run_id": r.run_id,
        "model_id": r.model_id,
        "kind": r.kind,
        "period_start": as_iso(r.period_start),
        "period_end": as_iso(r.period_end),
        "independent": r.independent,
        "validated": bool(r.independent),
        "metrics": to_jsonable(r.metrics),
        "created_at": as_iso(r.created_at),
    }


def experiment_to_dict(e: ExperimentRun, reports: list[EvaluationReport]) -> dict[str, Any]:
    plan = e.plan or {}
    return {
        "run_id": e.run_id,
        "plan_id": e.plan_id,
        "plan_hash": e.plan_hash,
        "hypothesis": plan.get("hypothesis"),
        "variants": plan.get("variants") or plan.get("arms"),
        "plan": to_jsonable(plan),
        "status": e.status,
        "started_at": as_iso(e.started_at),
        "finished_at": as_iso(e.finished_at),
        "trials": len(e.trials),
        "final_test_consulted_at": as_iso(e.final_test_consulted_at),
        "code_commit": e.code_commit,
        "seed": e.seed,
        "reports": [report_to_dict(r) for r in reports],
        "validated": any(r.independent for r in reports),
    }


def model_to_dict(m: ModelVersion) -> dict[str, Any]:
    return {
        "model_id": m.model_id,
        "family": m.family,
        "status": m.status,
        "code_commit": m.code_commit,
        "dataset_hash": m.dataset_hash,
        "feature_schema_hash": m.feature_schema_hash,
        "period_start": as_iso(m.period_start),
        "period_end": as_iso(m.period_end),
        "universe_version": m.universe_version,
        "manifest": to_jsonable(m.manifest),
        "artifact_sha256": m.artifact_sha256,
        "created_at": as_iso(m.created_at),
        "promoted_at": as_iso(m.promoted_at),
        "promoted_by": m.promoted_by,
        "validated": m.status in ("validated", "promoted", "active"),
    }


def research_payload(ctx: ApiContext, limit: int = 50) -> dict[str, Any]:
    rm = ctx.readmodel
    runs = rm.experiments(limit=limit)
    reports = rm.evaluation_reports(limit=500)
    by_run: dict[str, list[EvaluationReport]] = {}
    for r in reports:
        by_run.setdefault(r.run_id, []).append(r)
    return {
        "ok": True,
        "mode": ctx.cfg.mode.value,
        "synthetic_data": ctx.synthetic_data,
        "research": to_jsonable(ctx.cfg.research.model_dump()),
        "experiments": [experiment_to_dict(e, by_run.get(e.run_id, [])) for e in runs],
        "models": [model_to_dict(m) for m in rm.models()],
    }


@router.get("/api/v1/experiments", summary="Expériences avec leurs rapports (validés = période indépendante)")
def experiments(
    request: Request,
    _p: Principal = Depends(require_role(Role.READER)),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    payload = research_payload(get_ctx(request), limit=clamp(limit, 50))
    return {k: v for k, v in payload.items() if k != "models"}


@router.get("/api/v1/experiments/{run_id}", summary="Une expérience et ses rapports")
def experiment_detail(
    request: Request, run_id: str, _p: Principal = Depends(require_role(Role.READER))
) -> dict[str, Any]:
    ctx = get_ctx(request)
    e = ctx.readmodel.experiment(run_id)
    if e is None:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "EXPERIENCE_INCONNUE", "message": run_id})
    return {"ok": True, "experiment": experiment_to_dict(e, ctx.readmodel.evaluation_reports(run_id))}


@router.get("/api/v1/models", summary="Versions de modèles enregistrées")
def models(request: Request, _p: Principal = Depends(require_role(Role.READER))) -> dict[str, Any]:
    ctx = get_ctx(request)
    return {"ok": True, "items": [model_to_dict(m) for m in ctx.readmodel.models()]}
