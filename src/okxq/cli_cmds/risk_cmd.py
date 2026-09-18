"""``okxq risk status --config`` : lit ``risk_state`` (halts, pertes, HWM) et affiche les limites."""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer

from okxq.cli_cmds._common import emit, load_or_exit, session_factory_from_env
from okxq.risk.budgets import LimitSet
from okxq.risk.kill_switch import HaltLevel, RiskStateRecord, SqlRiskStateStore, latest_operator_actions

app = typer.Typer(help="Risk Engine : état persistant des halts, pertes et limites.")


def _limits_dict(limits: LimitSet) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(limits):
        v = getattr(limits, f.name)
        out[f.name] = format(v, "f") if isinstance(v, Decimal) else v
    return out


def _state_dict(state: RiskStateRecord | None, scope: str) -> dict[str, Any]:
    if state is None:
        return {"account_scope": scope, "halt_level": HaltLevel.NONE.value, "persisted": False}
    return {
        "account_scope": state.account_scope,
        "persisted": True,
        "halt_level": state.halt_level.value,
        "halt_reason": state.halt_reason,
        "halt_since": state.halt_since.isoformat() if state.halt_since else None,
        "utc_day": state.utc_day.isoformat() if state.utc_day else None,
        "day_start_equity": None if state.day_start_equity is None else format(state.day_start_equity, "f"),
        "day_realized_loss": format(state.day_realized_loss, "f"),
        "day_loss_fraction": (
            None
            if not state.day_start_equity
            else format(state.day_realized_loss / state.day_start_equity, "f")
        ),
        "high_water_mark_unit": None
        if state.high_water_mark_unit is None
        else format(state.high_water_mark_unit, "f"),
        "high_water_mark_equity": (
            None if state.high_water_mark_equity is None else format(state.high_water_mark_equity, "f")
        ),
        "limits_version": state.limits_version,
        "updated_at": state.updated_at.isoformat(),
        "version": state.version,
    }


@app.command("status")
def status(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    actions: int = typer.Option(
        5, "--actions", min=0, max=100, help="Dernières actions opérateur à afficher."
    ),
) -> None:
    """Affiche halts, pertes journalières, high-water mark et limites (versionnées) du compte configuré."""
    cfg = load_or_exit(config)
    limits = LimitSet.from_config(cfg)
    factory = session_factory_from_env()
    store = SqlRiskStateStore(factory)
    state = store.load(cfg.account.scope)
    with factory() as session:
        recent = latest_operator_actions(session, limit=actions) if actions else []
        recent_rows = [
            {
                "request_id": a.request_id,
                "action": a.action,
                "status": a.status,
                "actor": a.actor,
                "reason": a.reason,
                "requested_at": a.requested_at.isoformat(),
            }
            for a in recent
        ]
    emit(
        {
            "mode": cfg.mode.value,
            "live_enabled": cfg.project.live_enabled,
            "state": _state_dict(state, cfg.account.scope),
            "limits": _limits_dict(limits),
            "limits_match_state": state is None or state.limits_version == limits.limits_version,
            "recent_operator_actions": recent_rows,
        }
    )
