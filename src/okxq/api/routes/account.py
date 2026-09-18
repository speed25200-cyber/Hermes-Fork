"""Compte et positions (lecture)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from okxq.api.auth import Principal, Role, require_role
from okxq.api.context import ApiContext, get_ctx
from okxq.api.serialize import as_iso, to_jsonable
from okxq.persistence.models import PositionSnapshot

router = APIRouter(tags=["compte"])


def account_summary(ctx: ApiContext) -> dict[str, Any]:
    snap = ctx.readmodel.latest_account()
    base: dict[str, Any] = {
        "ok": True,
        "mode": ctx.cfg.mode.value,
        "account_scope": ctx.cfg.account.scope,
        "currency": ctx.cfg.account.settlement_currency,
        "synthetic_data": ctx.synthetic_data,
    }
    if snap is None:
        return {
            **base,
            "available": False,
            "as_of": None,
            "equity_version": None,
            "source": None,
            "equity": None,
            "cash_collateral": None,
            "unrealized_pnl": None,
            "available_margin": None,
            "used_margin": None,
            "external_cashflow_cum": None,
            "unit_value": None,
        }
    return {
        **base,
        "available": True,
        "as_of": as_iso(snap.as_of),
        "equity_version": snap.equity_version,
        "source": snap.source,
        "equity": to_jsonable(snap.equity),
        "cash_collateral": to_jsonable(snap.cash_collateral),
        "unrealized_pnl": to_jsonable(snap.unrealized_pnl),
        "available_margin": to_jsonable(snap.available_margin),
        "used_margin": to_jsonable(snap.used_margin),
        "external_cashflow_cum": to_jsonable(snap.external_cashflow_cum),
        "unit_value": to_jsonable(snap.unit_value),
    }


def position_to_dict(ctx: ApiContext, row: PositionSnapshot, open_since: Any) -> dict[str, Any]:
    qty = row.signed_base_qty
    mark = row.mark_price
    notional = None if mark is None else abs(qty) * mark
    pnl = None if mark is None else qty * (mark - row.average_entry_price)
    return {
        "inst_id": row.inst_id,
        "mode": ctx.cfg.mode.value,
        "account_scope": row.account_scope,
        "side": "LONG" if qty > 0 else "SHORT" if qty < 0 else "FLAT",
        "signed_base_qty": to_jsonable(qty),
        "signed_contracts": to_jsonable(row.signed_contracts),
        "average_entry_price": to_jsonable(row.average_entry_price),
        "mark_price": to_jsonable(mark),
        "liquidation_price": to_jsonable(row.liquidation_price),
        "margin": to_jsonable(row.margin),
        "leverage": to_jsonable(row.leverage),
        "notional": to_jsonable(notional),
        "unrealized_pnl": to_jsonable(pnl),
        "protection": to_jsonable(row.protection),
        "as_of": as_iso(row.as_of),
        "open_since": as_iso(open_since),
        "source": row.source,
        "version": row.version,
    }


@router.get("/api/v1/account/summary", summary="Dernier relevé de compte (null si aucun)")
def summary(request: Request, _p: Principal = Depends(require_role(Role.READER))) -> dict[str, Any]:
    return account_summary(get_ctx(request))


@router.get("/api/v1/portfolio/positions", summary="Positions ouvertes (dernier instantané par instrument)")
def positions(request: Request, _p: Principal = Depends(require_role(Role.READER))) -> dict[str, Any]:
    ctx = get_ctx(request)
    items = [position_to_dict(ctx, row, since) for row, since in ctx.readmodel.open_positions()]
    return {"ok": True, "mode": ctx.cfg.mode.value, "count": len(items), "items": items}
