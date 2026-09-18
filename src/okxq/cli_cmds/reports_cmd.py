"""``okxq reports daily|decisions|risk|equity`` : rapports machine-readable depuis la base (§61, §75).

Tous les rapports respectent une règle unique : une mesure absente vaut ``null``, jamais zéro. Un
rapport qui écrit 0 là où il n'a pas mesuré transforme une ignorance en affirmation, et c'est sur ce
genre d'affirmation qu'on prend ensuite de mauvaises décisions.

Aucun rapport n'appelle l'échange : ils relisent ce qui a été persisté. Un chiffre qui ne figure pas
en base n'apparaît pas dans un rapport.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from okxq.cli_cmds._common import emit, load_or_exit, session_factory_from_env
from okxq.config.schema import AppConfig
from okxq.domain.errors import OkxqError
from okxq.persistence.models import (
    AccountSnapshot,
    DataQualityEvent,
    Decision,
    FillRow,
    OperatorAction,
    PositionSnapshot,
    RiskEventRow,
    RiskState,
)

app = typer.Typer(help="Rapports machine-readable (lecture seule ; aucune donnée inventée).")


def _write(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _num(value: Decimal | float | None) -> str | None:
    """Formate un nombre, ``None`` si absent. Ne substitue JAMAIS zéro à une absence."""
    if value is None:
        return None
    return format(Decimal(str(value)), "f")


def _window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(tz=UTC)
    return end - timedelta(days=days), end


_CONFIG = typer.Option(..., "--config", exists=True, dir_okay=False)
_OUT = typer.Option(None, "--out", help="Écrit le rapport JSON à ce chemin.")
_DAYS = typer.Option(1, "--jours", min=1, max=365, help="Fenêtre d'observation en jours.")


def decisions_payload(cfg: AppConfig, factory: Any, days: int) -> dict[str, Any]:
    """Répartition des issues et des motifs de décision sur la fenêtre.

    Les motifs comptent autant que les issues : savoir qu'il y a eu 1440 NO_TRADE n'apprend rien,
    savoir qu'ils étaient tous ``DATA_STALE`` désigne une panne de collecte.
    """
    start, end = _window(days)
    with factory() as session:
        rows = list(
            session.execute(
                select(Decision).where(Decision.cutoff_at >= start, Decision.cutoff_at <= end)
            ).scalars()
        )
    outcomes = Counter(r.outcome for r in rows)
    reasons: Counter[str] = Counter()
    for row in rows:
        reasons.update(row.reason_codes or [])
    durations = [sum(int(v) for v in (r.timings_ms or {}).values()) for r in rows if r.timings_ms]
    payload = {
        "ok": True,
        "mode": cfg.mode.value,
        "account_scope": cfg.account.scope,
        "fenetre": {"debut": start.isoformat(), "fin": end.isoformat(), "jours": days},
        "decisions": len(rows),
        # Sans aucune décision il n'y a pas de durée médiane : c'est une absence, pas un zéro.
        "duree_mediane_ms": None if not durations else sorted(durations)[len(durations) // 2],
        "duree_max_ms": None if not durations else max(durations),
        "issues": dict(sorted(outcomes.items())),
        "motifs": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        "alternatives_rejetees": sum(len(r.rejected_alternatives or []) for r in rows),
        "ts": end.isoformat(),
    }
    return payload


def risk_payload(cfg: AppConfig, factory: Any, days: int) -> dict[str, Any]:
    """État de risque courant, incidents et qualité des données sur la fenêtre."""
    start, end = _window(days)
    with factory() as session:
        state = session.get(RiskState, cfg.account.scope)
        events = list(
            session.execute(
                select(RiskEventRow)
                .where(RiskEventRow.created_at >= start)
                .order_by(RiskEventRow.created_at.desc())
            ).scalars()
        )
        quality = list(
            session.execute(select(DataQualityEvent).where(DataQualityEvent.occurred_at >= start)).scalars()
        )
        actions = list(
            session.execute(select(OperatorAction).where(OperatorAction.requested_at >= start)).scalars()
        )
    payload = {
        "ok": True,
        "mode": cfg.mode.value,
        "account_scope": cfg.account.scope,
        "fenetre": {"debut": start.isoformat(), "fin": end.isoformat(), "jours": days},
        # `null` et non "NONE" : une absence d'état persisté n'est pas un état sain.
        "etat": None
        if state is None
        else {
            "halt_level": state.halt_level,
            "halt_reason": state.halt_reason,
            "halt_since": None if state.halt_since is None else state.halt_since.isoformat(),
            "perte_du_jour": _num(state.day_realized_loss),
            "sommet_equite": _num(state.high_water_mark_equity),
            "limits_version": state.limits_version,
        },
        "incidents": {
            "total": len(events),
            "par_severite": dict(Counter(e.severity for e in events)),
            "par_motif": dict(Counter(e.reason_code for e in events)),
        },
        "qualite_donnees": {
            "total": len(quality),
            "par_type": dict(Counter(e.kind for e in quality)),
            "par_severite": dict(Counter(e.severity for e in quality)),
        },
        "actions_operateur": {
            "total": len(actions),
            "par_statut": dict(Counter(a.status for a in actions)),
            # Une demande enregistrée n'est pas une action effectuée : le distinguer est le point.
            "non_terminees": sum(1 for a in actions if a.completed_at is None),
        },
        "ts": end.isoformat(),
    }
    return payload


def equity_payload(cfg: AppConfig, factory: Any, days: int) -> dict[str, Any]:
    """Derniers instantanés d'équité et positions ouvertes.

    L'équité rapportée est celle PERSISTÉE, pas une valeur recalculée à la volée : un rapport doit
    pouvoir être confronté à ce que le système croyait au moment où il le croyait.
    """
    start, end = _window(days)
    with factory() as session:
        latest = session.execute(
            select(AccountSnapshot)
            .where(AccountSnapshot.account_scope == cfg.account.scope)
            .order_by(AccountSnapshot.as_of.desc())
            .limit(1)
        ).scalar_one_or_none()
        count = session.execute(
            select(func.count(AccountSnapshot.equity_version)).where(
                AccountSnapshot.account_scope == cfg.account.scope,
                AccountSnapshot.as_of >= start,
            )
        ).scalar_one()
        positions = list(
            session.execute(
                select(PositionSnapshot)
                .where(PositionSnapshot.account_scope == cfg.account.scope)
                .order_by(PositionSnapshot.as_of.desc())
                .limit(200)
            ).scalars()
        )
        fills = list(
            session.execute(
                select(FillRow).where(FillRow.account_scope == cfg.account.scope, FillRow.fill_at >= start)
            ).scalars()
        )
    seen: set[str] = set()
    open_positions = []
    for row in positions:
        if row.inst_id in seen:
            continue
        seen.add(row.inst_id)
        if row.signed_base_qty:
            open_positions.append(
                {
                    "inst_id": row.inst_id,
                    "signed_base_qty": _num(row.signed_base_qty),
                    "signed_contracts": _num(row.signed_contracts),
                    "mark_price": _num(row.mark_price),
                    "liquidation_price": _num(row.liquidation_price),
                    "as_of": row.as_of.isoformat(),
                }
            )
    payload = {
        "ok": True,
        "mode": cfg.mode.value,
        "account_scope": cfg.account.scope,
        "fenetre": {"debut": start.isoformat(), "fin": end.isoformat(), "jours": days},
        "instantanes_dans_la_fenetre": int(count),
        "equite": None
        if latest is None
        else {
            "equity": _num(latest.equity),
            "cash_collateral": _num(latest.cash_collateral),
            "unrealized_pnl": _num(latest.unrealized_pnl),
            "external_cashflow_cum": _num(latest.external_cashflow_cum),
            # La valeur d'unité neutralise les apports et retraits : c'est la seule série comparable
            # dans le temps quand le capital bouge.
            "unit_value": _num(latest.unit_value),
            "equity_version": latest.equity_version,
            "as_of": latest.as_of.isoformat(),
        },
        "positions_ouvertes": open_positions,
        "executions": {
            "total": len(fills),
            # Les frais sont la SOMME des flux réellement facturés ; un rebate est négatif et
            # reste négatif. Sans exécution, il n'y a pas de frais mesurés : `null`, pas 0.
            "frais": _num(sum((f.fee_cashflow for f in fills), Decimal(0))) if fills else None,
        },
        "ts": end.isoformat(),
    }
    return payload


# --- commandes ------------------------------------------------------------------------------------


BUILDERS: dict[str, Any] = {
    "decisions": decisions_payload,
    "risque": risk_payload,
    "equite": equity_payload,
}


def _emit_report(name: str, config: Path, days: int, out: Path | None) -> None:
    cfg = load_or_exit(config)
    payload = BUILDERS[name](cfg, session_factory_from_env(), days)
    _write(out, payload)
    emit(payload)


@app.command("decisions")
def decisions(config: Path = _CONFIG, days: int = _DAYS, out: Path | None = _OUT) -> None:
    """Répartition des issues et des motifs de décision."""
    _emit_report("decisions", config, days, out)


@app.command("risk")
def risk(config: Path = _CONFIG, days: int = _DAYS, out: Path | None = _OUT) -> None:
    """État de risque, incidents, qualité des données, actions opérateur."""
    _emit_report("risque", config, days, out)


@app.command("equity")
def equity(config: Path = _CONFIG, days: int = _DAYS, out: Path | None = _OUT) -> None:
    """Équité persistée, valeur d'unité et positions ouvertes."""
    _emit_report("equite", config, days, out)


@app.command("daily")
def daily(config: Path = _CONFIG, out: Path | None = _OUT) -> None:
    """Rapport quotidien : décisions, risque et équité en un seul document.

    Il APPELLE les mêmes fonctions que les rapports individuels plutôt que de recalculer : deux
    chemins de calcul pour un même chiffre finissent par divergerment, et c'est alors la version
    fausse qu'on cite. Un échec de section est rapporté comme tel et n'invalide pas les autres — un
    rapport partiel honnête vaut mieux qu'un rapport absent.
    """
    cfg = load_or_exit(config)
    factory = session_factory_from_env()
    start, end = _window(1)
    sections: dict[str, Any] = {}
    for name, builder in BUILDERS.items():
        try:
            sections[name] = builder(cfg, factory, 1)
        except OkxqError as exc:
            sections[name] = {"ok": False, "error": exc.code, "message": str(exc), **exc.context}
        except SQLAlchemyError as exc:
            sections[name] = {"ok": False, "error": "BASE_INDISPONIBLE", "message": repr(exc)}
    payload = {
        "ok": all(bool(section.get("ok")) for section in sections.values()),
        "mode": cfg.mode.value,
        "account_scope": cfg.account.scope,
        "fenetre": {"debut": start.isoformat(), "fin": end.isoformat(), "jours": 1},
        "sections": sections,
        "ts": end.isoformat(),
    }
    _write(out, payload)
    emit(payload)
    if not payload["ok"]:
        raise typer.Exit(code=1)
