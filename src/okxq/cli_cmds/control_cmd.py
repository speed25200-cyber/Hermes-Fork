"""``okxq control pause|resume|cancel-entry-orders|request-flatten --config --reason --actor``.

Chaque commande crée d'abord un ``OperatorAction`` audité (request_id, acteur, rôle, raison), PUIS
applique ou demande le changement de ``RiskState`` :

- ``pause`` : SOFT_HALT (aucune augmentation d'exposition) ;
- ``request-flatten`` : EMERGENCY_FLATTEN (demande de sortie contrôlée, exécutée par le runtime) ;
- ``cancel-entry-orders`` : demande d'annulation des ordres d'entrée (consommée par le gateway) ;
- ``resume`` : lève le halt SEULEMENT si les préconditions vérifiables hors ligne tiennent (délai de
  stabilité écoulé depuis ``halt_since``, rôle autorisé) ; le runtime ré-escalade immédiatement si un
  déclencheur persiste (escalade seule, jamais de descente automatique).

Aucune de ces commandes ne peut activer LIVE, augmenter un levier ni relâcher une limite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from okxq.cli_cmds._common import emit, load_or_exit, session_factory_from_env
from okxq.config.schema import AppConfig
from okxq.domain.clocks import SystemClock
from okxq.domain.errors import HaltError
from okxq.domain.ids import new_id
from okxq.risk.budgets import LimitSet
from okxq.risk.kill_switch import (
    HaltLevel,
    HealthSignals,
    KillSwitch,
    OperatorRequest,
    SqlRiskStateStore,
    record_operator_action,
)

app = typer.Typer(help="Actions opérateur auditées sur le Risk Engine (jamais d'activation LIVE).")

ACTION_PAUSE = "PAUSE"
ACTION_RESUME = "RESUME"
ACTION_CANCEL_ENTRIES = "CANCEL_ENTRY_ORDERS"
ACTION_REQUEST_FLATTEN = "REQUEST_FLATTEN"

STATUS_APPLIED = "APPLIED"
STATUS_REQUESTED = "REQUESTED"
STATUS_REJECTED = "REJECTED"


def _request(actor: str, role: str, reason: str) -> OperatorRequest:
    try:
        return OperatorRequest(
            request_id=new_id("opr"), actor=actor, role=role, reason=reason, requested_at=datetime.now(tz=UTC)
        )
    except HaltError as exc:
        typer.echo(f"action refusée : {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _kill_switch(cfg: AppConfig, store: SqlRiskStateStore) -> KillSwitch:
    return KillSwitch(
        account_scope=cfg.account.scope,
        store=store,
        cfg=cfg.risk,
        clock=SystemClock(),
        limits_version=LimitSet.from_config(cfg).limits_version,
    )


def _run(cfg: AppConfig, action: str, request: OperatorRequest, apply: Any) -> None:
    factory = session_factory_from_env()
    store = SqlRiskStateStore(factory)
    with factory() as session:
        row = record_operator_action(session, action=action, scope=cfg.account.scope, request=request, status=STATUS_REQUESTED)
        session.commit()
    ks = _kill_switch(cfg, store)
    status, result = apply(ks)
    with factory() as session:
        from okxq.persistence.models import OperatorAction

        row = session.get(OperatorAction, request.request_id)
        assert row is not None
        row.status = status
        row.observed_result = result
        row.completed_at = datetime.now(tz=UTC)
        session.commit()
    emit(
        {
            "request_id": request.request_id,
            "action": action,
            "status": status,
            "halt_level": ks.level.value,
            "halt_reason": ks.state.halt_reason,
            "result": result,
        }
    )
    if status == STATUS_REJECTED:
        raise typer.Exit(code=1)


_CONFIG = typer.Option(..., "--config", exists=True, dir_okay=False)
_REASON = typer.Option(..., "--reason", min=3, help="Motif audité (obligatoire).")
_ACTOR = typer.Option(..., "--actor", min=1, help="Identité de l'opérateur (auditée).")
_ROLE = typer.Option("operator", "--role", help="Rôle déclaré (operator|owner|viewer).")


@app.command("pause")
def pause(config: Path = _CONFIG, reason: str = _REASON, actor: str = _ACTOR, role: str = _ROLE) -> None:
    """SOFT_HALT : aucune augmentation d'exposition ; réductions et protections autorisées."""
    cfg = load_or_exit(config)
    req = _request(actor, role, reason)

    def apply(ks: KillSwitch) -> tuple[str, dict[str, Any]]:
        before = ks.level
        ks.request(HaltLevel.SOFT_HALT, reason, operator=req)
        return STATUS_APPLIED, {"before": before.value, "after": ks.level.value}

    _run(cfg, ACTION_PAUSE, req, apply)


@app.command("request-flatten")
def request_flatten(config: Path = _CONFIG, reason: str = _REASON, actor: str = _ACTOR, role: str = _ROLE) -> None:
    """EMERGENCY_FLATTEN : demande de sortie contrôlée ; les résidus restent exposés et rapportés."""
    cfg = load_or_exit(config)
    req = _request(actor, role, reason)

    def apply(ks: KillSwitch) -> tuple[str, dict[str, Any]]:
        before = ks.level
        ks.request(HaltLevel.EMERGENCY_FLATTEN, reason, operator=req)
        return STATUS_REQUESTED, {"before": before.value, "after": ks.level.value, "executed_by": "runtime"}

    _run(cfg, ACTION_REQUEST_FLATTEN, req, apply)


@app.command("cancel-entry-orders")
def cancel_entry_orders(config: Path = _CONFIG, reason: str = _REASON, actor: str = _ACTOR, role: str = _ROLE) -> None:
    """Demande l'annulation des ordres d'entrée ouverts (consommée par le gateway) et passe en HARD_HALT."""
    cfg = load_or_exit(config)
    req = _request(actor, role, reason)

    def apply(ks: KillSwitch) -> tuple[str, dict[str, Any]]:
        before = ks.level
        ks.request(HaltLevel.HARD_HALT, reason, operator=req)
        return STATUS_REQUESTED, {"before": before.value, "after": ks.level.value, "executed_by": "gateway"}

    _run(cfg, ACTION_CANCEL_ENTRIES, req, apply)


@app.command("resume")
def resume(config: Path = _CONFIG, reason: str = _REASON, actor: str = _ACTOR, role: str = _ROLE) -> None:
    """Lève un halt sous préconditions (délai de stabilité, rôle autorisé). Jamais d'activation LIVE."""
    cfg = load_or_exit(config)
    req = _request(actor, role, reason)

    def apply(ks: KillSwitch) -> tuple[str, dict[str, Any]]:
        before = ks.level
        if before is HaltLevel.NONE:
            return STATUS_APPLIED, {"before": before.value, "after": before.value, "note": "aucun halt actif"}
        # Signaux hors ligne : la CLI ne voit pas le marché ; seules les préconditions vérifiables ici
        # sont appliquées. Le runtime ré-escalade si un déclencheur persiste.
        signals = HealthSignals(now=datetime.now(tz=UTC), reconciliation_ok=True)
        try:
            ks.operator_resume(req, signals)
        except HaltError as exc:
            return STATUS_REJECTED, {"before": before.value, "after": ks.level.value, "error": str(exc), **exc.context}
        return STATUS_APPLIED, {"before": before.value, "after": ks.level.value}

    _run(cfg, ACTION_RESUME, req, apply)
