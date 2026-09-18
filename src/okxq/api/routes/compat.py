"""Compatibilité de l'interface conservée : canaux ``POST /api/<canal>`` et flux SSE ``/api/flux``.

L'interface Hermes existante parle à son moteur par des canaux nommés (``window.api.invoke``) et par un
flux d'événements. Cette couche reproduit EXACTEMENT cette surface au-dessus des lectures de la nouvelle
plateforme, pour que la page reste la même page.

Deux refus explicites, qui sont le cœur de la séparation des pouvoirs :

- ``toggle-ai`` ne démarre ni n'arrête rien directement : il crée une ACTION OPÉRATEUR auditée
  (pause / demande de reprise) que le superviseur exécute sous préconditions. Un bouton ne contourne
  pas le Risk Engine (§58).
- ``poser-cles`` refuse : les clés d'exchange vivent dans l'environnement du SEUL gateway, jamais
  posées depuis un navigateur (§60). Le canal répond par un message qui dit où les poser.

Aucune route ici n'appelle le réseau : les bougies viennent des archives, jamais d'un appel sortant.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from okxq.api.auth import Principal, Role, current_principal, require_role
from okxq.api.candles import load_candles
from okxq.api.context import ApiContext, get_ctx
from okxq.api.routes.account import account_summary
from okxq.api.routes.control import submit_operator_action
from okxq.api.serialize import as_iso, to_jsonable
from okxq.api.sse import format_sse
from okxq.runtime.logging import get_logger

router = APIRouter(tags=["compatibilité interface"])
_log = get_logger("okxq.api.compat")

#: Canaux exposés à la page. Tout autre nom rend ``CANAL_INCONNU`` — comme l'ancien serveur.
CHANNELS: tuple[str, ...] = (
    "fetch-portfolio",
    "get-ai-state",
    "ai:live-status",
    "ai:training-status",
    "ui-mode",
    "toggle-ai",
    "poser-cles",
    "chandelles",
    "laboratoire",
    "decisions",
    "jev",
    "risque",
)

HEARTBEAT_SECONDS = 4.0


def _ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value.timestamp() * 1000)
    except AttributeError:
        return None


def _f(value: Any) -> float | None:
    """Nombre pour l'affichage. ``None`` reste ``None`` : l'interface écrit « Non disponible », pas zéro."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _performance(ctx: ApiContext) -> dict[str, Any]:
    """Taux de gain et volumes DÉRIVÉS des positions clôturées et des fills.

    Deux questions distinctes (§59) : « comment s'est passée la journée » et « que vaut l'historique ».
    Une absence de trade rend ``None``, jamais 0 % — un zéro se lirait comme une performance nulle.
    """
    rm = ctx.readmodel
    now = ctx.clock.now_utc()
    closed = rm.closed_positions(limit=500)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    scored = [c for c in closed if c.pnl is not None]
    today = [c for c in scored if c.close_time >= day_start]
    wins = sum(1 for c in scored if c.pnl is not None and c.pnl > 0)
    wins24 = sum(1 for c in today if c.pnl is not None and c.pnl > 0)
    fills_today = rm.fills_today()
    volume = Decimal(0)
    for f in fills_today:
        units = rm.base_units_per_contract(f.inst_id)
        if units is not None:
            volume += abs(f.contracts) * units * f.fill_price
    return {
        "winrate": _f(Decimal(wins) / Decimal(len(scored)) * 100) if scored else None,
        "winrate24": _f(Decimal(wins24) / Decimal(len(today)) * 100) if today else None,
        "wins": wins if scored else None,
        "wins24": wins24 if today else None,
        "daily_pnl": _f(sum((c.pnl for c in today if c.pnl is not None), Decimal(0))) if today else None,
        "total_trades": len(scored),
        "daily_trades": len(today),
        "daily_volume": _f(volume) if fills_today else None,
    }


def portfolio_payload(ctx: ApiContext) -> dict[str, Any]:
    """Forme attendue par la page conservée (canal ``fetch-portfolio``)."""
    rm = ctx.readmodel
    summary = account_summary(ctx)
    positions: list[dict[str, Any]] = []
    total_notional = Decimal(0)
    total_margin = Decimal(0)
    unrealized = Decimal(0)
    for row, since in rm.open_positions():
        qty = row.signed_base_qty
        mark = row.mark_price
        notional = None if mark is None else abs(qty) * mark
        pnl = None if mark is None else qty * (mark - row.average_entry_price)
        margin = row.margin
        if notional is not None:
            total_notional += notional
        if margin is not None:
            total_margin += margin
        if pnl is not None:
            unrealized += pnl
        protection = row.protection if isinstance(row.protection, dict) else {}
        positions.append(
            {
                "symbol": row.inst_id,
                "side": "LONG" if qty > 0 else "SHORT" if qty < 0 else "FLAT",
                "leverage": _f(row.leverage),
                "entryPrice": _f(row.average_entry_price),
                "markPrice": _f(mark),
                "size": _f(abs(qty)),
                "notional": _f(notional),
                "margin": _f(margin),
                "entryTime": _ms(since),
                "liqPrice": _f(row.liquidation_price),
                "unrealizedPnl": _f(pnl),
                "pnlPctOfMargin": _f(pnl / margin * 100) if (pnl is not None and margin) else None,
                "takeProfit": _f(protection.get("take_profit")),
                "stopLoss": _f(protection.get("stop_loss")),
                "stopActuel": _f(protection.get("stop_current") or protection.get("stop_loss")),
                "stopMode": protection.get("stop_mode"),
                "trailArme": bool(protection.get("trail_armed")),
                # Une protection non CONFIRMÉE côté exchange n'est pas une protection (§54, T59).
                "protectionConfirmee": bool(protection.get("confirmed")),
                "protection": to_jsonable(row.protection),
                "mode": ctx.cfg.mode.value,
            }
        )
    closed = [
        {
            "symbol": c.inst_id,
            "side": c.side,
            "leverage": _f(getattr(c, "leverage", None)),
            "entryPrice": _f(c.entry_price),
            "closePrice": _f(c.close_price),
            "openTime": _ms(c.open_time),
            "closeTime": _ms(c.close_time),
            "pnl": _f(c.pnl),
            "pnlRatio": _f(c.pnl_ratio),
        }
        for c in rm.closed_positions(limit=30)
    ]
    series = [
        {"t": _ms(s.as_of), "v": _f(s.equity)} for s in rm.account_series(limit=600) if s.equity is not None
    ]
    perf = _performance(ctx)
    return {
        "ok": True,
        "data": {
            "spot": {"total": 0},
            "futures": {
                "total": summary.get("equity"),
                "available": summary.get("available_margin"),
                "unrealizedPnL": _f(unrealized) if positions else summary.get("unrealized_pnl"),
            },
            "positions": {
                "count": len(positions),
                "totalValue": _f(total_notional),
                "totalMargin": _f(total_margin),
            },
            "performance": {
                "winrate": perf.get("winrate"),
                "winrate24": perf.get("winrate24"),
                "gagnees": perf.get("wins"),
                "gagnees24": perf.get("wins24"),
                "dailyPnL": perf.get("daily_pnl"),
                "totalTrades": perf.get("total_trades"),
                "dailyTrades": perf.get("daily_trades"),
                "dailyVolume": perf.get("daily_volume"),
            },
            "openPositionsDetails": positions,
            "positionsFermees": closed,
            "history": {"spot": series},
            # Le mode et la nature des données sont VISIBLES en permanence (§59).
            "mode": ctx.cfg.mode.value,
            "accountScope": ctx.cfg.account.scope,
            "syntheticData": ctx.synthetic_data,
            "clesOkx": ctx.cfg.mode.uses_private_streams,
            "lastUpdate": summary.get("as_of"),
        },
        "ts": as_iso(ctx.clock.now_utc()),
    }


def ai_state_payload(ctx: ApiContext) -> dict[str, Any]:
    logs = ctx.bus.recent("ai-log", limit=260)
    state = ctx.readmodel.risk_state()
    return {
        "ok": True,
        "active": bool(state is not None and state.halt_level == "NONE"),
        "halt_level": None if state is None else state.halt_level,
        "halt_reason": None if state is None else state.halt_reason,
        "logs": logs,
        "mode": ctx.cfg.mode.value,
        "ts": as_iso(ctx.clock.now_utc()),
    }


async def dispatch(ctx: ApiContext, canal: str, arg: Any, request: Request) -> dict[str, Any]:
    principal = current_principal(request)
    role = principal.role if principal is not None else Role.READER
    if canal == "fetch-portfolio":
        return portfolio_payload(ctx)
    if canal == "get-ai-state":
        return ai_state_payload(ctx)
    if canal == "ai:live-status":
        state = ctx.readmodel.risk_state()
        return {
            "ok": True,
            # « Le moteur tourne » ne veut PAS dire « LIVE » : le mode est une propriété distincte.
            "liveEnabled": bool(state is not None and state.halt_level == "NONE"),
            "mode": ctx.cfg.mode.value,
            "isLive": ctx.cfg.is_live,
            "ts": as_iso(ctx.clock.now_utc()),
        }
    if canal == "ai:training-status":
        runs = ctx.readmodel.experiments(limit=1)
        return {
            "ok": True,
            "training": bool(runs and runs[0].status == "RUNNING"),
            "lastRun": None if not runs else runs[0].run_id,
            "ts": as_iso(ctx.clock.now_utc()),
        }
    if canal == "ui-mode":
        return {
            "ok": True,
            "mode": "full" if role in (Role.OPERATOR, Role.ADMIN) else "viewer",
            "role": role.value,
            "runMode": ctx.cfg.mode.value,
            "allowLiveActivation": False,
        }
    if canal == "toggle-ai":
        desired = bool(arg)
        action = "request_resume" if desired else "pause"
        result: dict[str, Any] = submit_operator_action(
            ctx,
            request,
            action=action,
            scope=ctx.cfg.account.scope,
            reason="demande depuis l'interface (bouton moteur)",
        )
        return result
    if canal == "poser-cles":
        # Refus explicite et documenté : aucune clé d'exchange ne se pose depuis un navigateur.
        _log.warning("tentative de pose de clés par l'interface", role=role.value)
        return {
            "ok": False,
            "error": "NON_SUPPORTE",
            "message": (
                "Les clés OKX ne se posent pas depuis l'interface. Elles vivent dans l'environnement du "
                "seul service gateway (infra/env/gateway.env sur la machine), posées par le workflow "
                "« VPS status », et ne sont jamais transmises à l'API ni à JEV."
            ),
        }
    if canal == "chandelles":
        params = arg if isinstance(arg, dict) else {}
        inst_id = str(params.get("instId", ""))
        bar = str(params.get("bar", "1m"))
        if not inst_id:
            return {"ok": False, "error": "INSTRUMENT_INVALIDE"}
        data_root = ctx.extra.get("data_root", ctx.cfg.storage.data_root)
        from pathlib import Path

        return load_candles(ctx.session_factory, data_root=Path(data_root), inst_id=inst_id, bar=bar)
    if canal in ("laboratoire", "decisions", "jev", "risque"):
        # Ces onglets lisent les routes v1 ; le canal rend un pointeur plutôt que de dupliquer la logique.
        return {
            "ok": True,
            "redirect": {
                "laboratoire": "/api/v1/experiments",
                "decisions": "/api/v1/decisions",
                "jev": "/api/v1/jev/status",
                "risque": "/api/v1/risk/events",
            }[canal],
            "mode": ctx.cfg.mode.value,
        }
    return {"ok": False, "error": "CANAL_INCONNU", "canal": canal}


@router.get("/api", summary="Liste des canaux de compatibilité")
def channels(request: Request, _p: Principal = Depends(require_role(Role.READER))) -> dict[str, Any]:
    return {"ok": True, "canaux": sorted(CHANNELS), "mode": get_ctx(request).cfg.mode.value}


@router.post("/api/{canal:path}", summary="Canal de compatibilité de l'interface conservée")
async def channel(canal: str, request: Request, _p: Principal = Depends(require_role(Role.READER))) -> Any:
    ctx = get_ctx(request)
    if canal.startswith("v1/") or canal in ("flux", ""):
        return {"ok": False, "error": "CANAL_INCONNU", "canal": canal}
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        body = {}
    arg = body.get("arg") if isinstance(body, dict) else None
    return await dispatch(ctx, canal, arg, request)


@router.get("/api/flux", summary="Flux d'événements (santé toutes les 4 s, journal en continu)")
async def flux(request: Request, _p: Principal = Depends(require_role(Role.READER))) -> StreamingResponse:
    ctx = get_ctx(request)
    sid, queue = ctx.bus.subscribe()

    async def generate() -> Any:
        yield b": ouvert\n\n"
        yield format_sse("health-tick", ctx.health.health_tick())
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                    yield format_sse(str(message.get("canal", "ai-log")), message.get("charge"))
                except TimeoutError:
                    ctx.poller.poll()
                    yield format_sse("health-tick", ctx.health.health_tick())
        finally:
            ctx.bus.unsubscribe(sid)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
