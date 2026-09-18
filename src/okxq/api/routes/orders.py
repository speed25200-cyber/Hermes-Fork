"""Ordres, événements d'ordres et fills (lecture)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from okxq.api.auth import Principal, Role, require_role
from okxq.api.context import get_ctx
from okxq.api.readmodel import clamp
from okxq.api.serialize import as_iso, to_jsonable
from okxq.persistence.models import FillRow, OrderEventRow, OrderRow

router = APIRouter(tags=["ordres"])


def order_to_dict(row: OrderRow) -> dict[str, Any]:
    return {
        "order_id": row.order_id,
        "client_order_id": row.client_order_id,
        "exchange_order_id": row.exchange_order_id,
        "intent_id": row.intent_id,
        "approval_id": row.approval_id,
        "inst_id": row.inst_id,
        "side": row.side,
        "order_type": row.order_type,
        "contracts": to_jsonable(row.contracts),
        "price_limit": to_jsonable(row.price_limit),
        "reduce_only": row.reduce_only,
        "observed_state": row.observed_state,
        "pending_operation": row.pending_operation,
        "cumulative_filled": to_jsonable(row.cumulative_filled),
        "average_fill_price": to_jsonable(row.average_fill_price),
        "payload_hash": row.payload_hash,
        "attempt_count": row.attempt_count,
        "created_at": as_iso(row.created_at),
        "sent_at": as_iso(row.sent_at),
        "ack_at": as_iso(row.ack_at),
        "terminal_at": as_iso(row.terminal_at),
        "updated_at": as_iso(row.updated_at),
        "version": row.version,
    }


def event_to_dict(row: OrderEventRow) -> dict[str, Any]:
    return {
        "event_id": row.event_id,
        "order_id": row.order_id,
        "client_order_id": row.client_order_id,
        "exchange_order_id": row.exchange_order_id,
        "event_kind": row.event_kind,
        "observed_state": row.observed_state,
        "cumulative_filled": to_jsonable(row.cumulative_filled),
        "event_ts": as_iso(row.event_ts),
        "receive_ts": as_iso(row.receive_ts),
        "raw_hash": row.raw_hash,
    }


def fill_to_dict(row: FillRow) -> dict[str, Any]:
    return {
        "execution_key": row.execution_key,
        "order_id": row.order_id,
        "client_order_id": row.client_order_id,
        "exchange_order_id": row.exchange_order_id,
        "trade_id": row.trade_id,
        "inst_id": row.inst_id,
        "side": row.side,
        "contracts": to_jsonable(row.contracts),
        "fill_price": to_jsonable(row.fill_price),
        "fee_cashflow": to_jsonable(row.fee_cashflow),
        "fee_ccy": row.fee_ccy,
        "liquidity": row.liquidity,
        "fill_at": as_iso(row.fill_at),
        "receive_ts": as_iso(row.receive_ts),
    }


@router.get("/api/v1/orders", summary="Ordres du compte (les plus récents d'abord)")
def orders(
    request: Request,
    _p: Principal = Depends(require_role(Role.READER)),
    limit: int = Query(100, ge=1, le=500),
    state: str | None = Query(None, description="État observé exact, ex. ACKNOWLEDGED"),
    inst_id: str | None = Query(None),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    rows = ctx.readmodel.orders(limit=clamp(limit), state=state, inst_id=inst_id)
    return {"ok": True, "mode": ctx.cfg.mode.value, "count": len(rows), "items": [order_to_dict(r) for r in rows]}


@router.get("/api/v1/orders/{order_id}", summary="Un ordre et ses événements")
def order_detail(
    request: Request, order_id: str, _p: Principal = Depends(require_role(Role.READER))
) -> dict[str, Any]:
    ctx = get_ctx(request)
    rows = [r for r in ctx.readmodel.orders(limit=500) if r.order_id == order_id]
    if not rows:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "ORDRE_INCONNU", "message": order_id})
    return {
        "ok": True,
        "order": order_to_dict(rows[0]),
        "events": [event_to_dict(e) for e in ctx.readmodel.order_events(order_id)],
    }


@router.get("/api/v1/fills", summary="Fills du compte (les plus récents d'abord)")
def fills(
    request: Request,
    _p: Principal = Depends(require_role(Role.READER)),
    limit: int = Query(100, ge=1, le=500),
    since: datetime | None = Query(None, description="ISO 8601, timezone-aware"),
    inst_id: str | None = Query(None),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    rows = ctx.readmodel.fills(limit=clamp(limit), since=since, inst_id=inst_id)
    return {"ok": True, "mode": ctx.cfg.mode.value, "count": len(rows), "items": [fill_to_dict(r) for r in rows]}
