"""Commandes opérateur : la SEULE écriture de l'API, et c'est une demande auditée.

``POST /api/v1/control/{pause|cancel-entry-orders|request-flatten|request-resume}`` crée un
``OperatorAction`` au statut ``REQUESTED`` ; le runtime (risk/gateway) l'exécute et renseigne
``status``/``observed_result``. L'API ne touche ni ordre ni position, et répond 202 : la demande est
enregistrée, pas exécutée. Un ``request_id`` déjà connu est renvoyé tel quel (idempotence).

Aucune commande n'active LIVE, ne change un mode, ni ne relâche une limite.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from okxq.api.auth import Principal, Role, require_role
from okxq.api.context import ApiContext, get_ctx
from okxq.api.readmodel import clamp
from okxq.api.serialize import as_iso, to_jsonable
from okxq.persistence.models import OperatorAction

router = APIRouter(tags=["contrôle"])

ControlCommand = Literal["pause", "cancel-entry-orders", "request-flatten", "request-resume"]
COMMAND_ACTIONS: dict[str, str] = {
    "pause": "PAUSE",
    "cancel-entry-orders": "CANCEL_ENTRY_ORDERS",
    "request-flatten": "REQUEST_FLATTEN",
    "request-resume": "REQUEST_RESUME",
}
STATUS_REQUESTED = "REQUESTED"


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=3, max_length=512, description="Motif humain, obligatoire")
    scope: str = Field(min_length=1, max_length=128, description="Compte, instrument ou 'all'")
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    actor: str = Field(min_length=2, max_length=128, description="Qui demande (nom d'opérateur)")


def action_to_dict(a: OperatorAction) -> dict[str, Any]:
    return {
        "request_id": a.request_id,
        "action": a.action,
        "scope": a.scope,
        "reason": a.reason,
        "actor": a.actor,
        "role": a.role,
        "requested_at": as_iso(a.requested_at),
        "status": a.status,
        "observed_result": to_jsonable(a.observed_result),
        "completed_at": as_iso(a.completed_at),
    }


def create_operator_action(
    ctx: ApiContext, *, action: str, body: ControlRequest, principal: Principal
) -> tuple[OperatorAction, bool]:
    """Insère la demande ; ``(ligne, créée)``. Une demande existante n'est jamais modifiée ici."""
    with ctx.session_factory() as session:
        existing = session.get(OperatorAction, body.request_id)
        if existing is not None:
            return existing, False
        row = OperatorAction(
            request_id=body.request_id,
            action=action,
            scope=body.scope,
            reason=body.reason,
            actor=f"{body.actor} ({principal.actor})",
            role=principal.role.value,
            requested_at=ctx.clock.now_utc(),
            status=STATUS_REQUESTED,
            observed_result={"mode": ctx.cfg.mode.value, "account_scope": ctx.cfg.account.scope},
            completed_at=None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    ctx.bus.publish(
        "ai-log",
        {
            "ts": as_iso(row.requested_at),
            "event": f"OPERATOR_{action}",
            "id": row.request_id,
            "status": STATUS_REQUESTED,
            "actor": row.actor,
            "scope": row.scope,
            "mode": ctx.cfg.mode.value,
        },
    )
    return row, True


@router.post(
    "/api/v1/control/{command}",
    status_code=202,
    summary="Demande opérateur auditée (pause, annulation des entrées, flatten, reprise) — rôle operator",
)
def control(
    request: Request,
    command: ControlCommand,
    body: ControlRequest,
    principal: Principal = Depends(require_role(Role.OPERATOR)),
) -> JSONResponse:
    ctx = get_ctx(request)
    action = COMMAND_ACTIONS[command]
    row, created = create_operator_action(ctx, action=action, body=body, principal=principal)
    payload = {
        "ok": True,
        "created": created,
        "request_id": row.request_id,
        "action": row.action,
        "status": row.status,
        "mode": ctx.cfg.mode.value,
        "account_scope": ctx.cfg.account.scope,
        "message": "demande enregistrée ; le runtime l'exécute et renseigne son résultat",
        "track": f"/api/v1/control/requests/{row.request_id}",
    }
    return JSONResponse(payload, status_code=202)


@router.get("/api/v1/control/requests/{request_id}", summary="Suivi d'une demande opérateur (avec résidus)")
def track(
    request: Request, request_id: str, _p: Principal = Depends(require_role(Role.READER))
) -> dict[str, Any]:
    ctx = get_ctx(request)
    row = ctx.readmodel.operator_action(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "DEMANDE_INCONNUE", "message": request_id})
    residuals = [
        {
            "inst_id": p.inst_id,
            "signed_base_qty": to_jsonable(p.signed_base_qty),
            "signed_contracts": to_jsonable(p.signed_contracts),
            "as_of": as_iso(p.as_of),
        }
        for p, _ in ctx.readmodel.open_positions()
    ]
    open_orders = [
        {"order_id": o.order_id, "inst_id": o.inst_id, "observed_state": o.observed_state}
        for o in ctx.readmodel.orders(limit=200)
        if o.observed_state in ("SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "UNKNOWN")
    ]
    return {
        "ok": True,
        "request": action_to_dict(row),
        "mode": ctx.cfg.mode.value,
        "account_scope": ctx.cfg.account.scope,
        "residual_positions": residuals,
        "open_orders": open_orders,
        "settled": row.status in ("COMPLETED", "DONE") and not residuals and not open_orders,
        "ts": as_iso(ctx.clock.now_utc()),
    }


@router.get("/api/v1/audit/operator-actions", summary="Journal d'audit des actions opérateur et des refus")
def audit_actions(
    request: Request,
    _p: Principal = Depends(require_role(Role.READER)),
    limit: int = Query(100, ge=1, le=500),
    status: str | None = Query(None, description="REQUESTED / DENIED / COMPLETED / FAILED"),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    rows = ctx.readmodel.operator_actions(limit=clamp(limit), status=status)
    return {"ok": True, "count": len(rows), "items": [action_to_dict(a) for a in rows]}
