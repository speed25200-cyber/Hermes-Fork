"""``okxq replay run`` : parcours complet HORS LIGNE sur un jeu de données archivé (§68.2).

Deux scénarios, tous deux étiquetés comme des SCÉNARIOS DE TEST — ce ne sont pas des stratégies, et
leurs résultats ne mesurent aucun avantage de marché :

- ``flat`` : aucune intention n'est produite. Il prouve que « ne rien faire » est un chemin complet et
  journalisé, et que la comptabilité reste équilibrée à zéro position.
- ``scripted`` : un aller-retour borné par instrument, à des frontières fixées, via une intention qui
  traverse RÉELLEMENT le Risk Engine puis le gateway. Il prouve que la chaîne
  intention → approbation → envoi → fill → ledger fonctionne, et qu'aucun ordre ne part sans approbation.

Le rapport est machine-readable et porte les invariants vérifiés.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import typer

from okxq.accounting.ledger import Ledger
from okxq.backtest.engine import BacktestEngine, DecisionContext, build_market_from_events
from okxq.backtest.virtual_exchange import ChaosOptions, VirtualExchange
from okxq.cli_cmds._common import emit, load_or_exit
from okxq.config.schema import AppConfig
from okxq.data.replay import load_dataset
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import DataQualityError, OkxqError
from okxq.domain.events import ApprovedOrder, OrderIntent, RiskAction, RiskDecision
from okxq.domain.instruments import round_contracts_risk_reducing, validate_order_size
from okxq.domain.money import Side
from okxq.domain.orders import OrderKind
from okxq.domain.reasons import ReasonCode
from okxq.exchange.base import OrderRequest
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import Base

app = typer.Typer(help="Replay hors ligne d'un jeu de données (aucun réseau, aucun endpoint privé).")

SCENARIOS = ("flat", "scripted")
TEST_POLICY_LABEL = "SCENARIO_DE_TEST"


@dataclass(slots=True)
class ScenarioState:
    entered: set[str]
    exited: set[str]


def _approved_order(intent: OrderIntent, *, now: Any) -> ApprovedOrder:
    """Approbation explicite d'un scénario de test : elle passe par les MÊMES contrats qu'en direct.

    Le hash approuvé est celui du payload normalisé exact ; une modification ultérieure invaliderait
    l'approbation (T49). Cette approbation est étiquetée ``SCENARIO_DE_TEST`` dans ses reason codes.
    """
    decision = RiskDecision(
        decision_id=f"rd_replay_{intent.intent_id}",
        intent_id=intent.intent_id,
        intent_hash=intent.payload_hash(),
        action=RiskAction.ALLOW,
        allowed_payload_hash=intent.payload_hash(),
        allowed_contracts=intent.contracts,
        limits_version="replay-test",
        position_version="replay-test",
        created_at=now,
        expires_at=now + timedelta(milliseconds=1000),
        reason_codes=[TEST_POLICY_LABEL, ReasonCode.OK.value],
    )
    return ApprovedOrder(intent=intent, decision=decision, payload_hash=intent.payload_hash())


async def _run(
    cfg: AppConfig, dataset: Path, scenario: str, *, chaos: ChaosOptions | None = None
) -> dict[str, Any]:
    manifest, events = load_dataset(dataset, verify=True)
    if not events:
        raise DataQualityError("jeu de données vide", path=str(dataset))
    clock = SimulatedClock(events[0].available_at)
    market = build_market_from_events(events)
    engine_db = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine_db)
    ledger = Ledger(
        make_session_factory(engine_db),
        account_scope=cfg.account.scope,
        clock=clock,
        settle_ccy=cfg.account.settlement_currency,
    )
    initial = cfg.account.paper_initial_equity
    ledger.record_external_cashflow(
        amount=initial,
        occurred_at=clock.now_utc(),
        idempotency_key="replay:initial-funding",
        description="dotation initiale PAPER (flux externe, neutralisé dans le PnL de stratégie)",
    )
    exchange = VirtualExchange(
        clock=clock,
        market=market,
        account_scope=cfg.account.scope,
        initial_cash=initial,
        chaos=chaos or ChaosOptions(),
    )
    state = ScenarioState(entered=set(), exited=set())

    async def decision(ctx: DecisionContext) -> dict[str, Any]:
        if scenario == "flat":
            # NO_TRADE est une décision parfaitement valide, et elle est journalisée comme telle.
            return {
                "outcome": "NO_TRADE",
                "reason_codes": [ReasonCode.EDGE_BELOW_COSTS.value],
                "policy": TEST_POLICY_LABEL,
            }
        sent: list[dict[str, Any]] = []
        for inst_id, view in sorted(ctx.market.book_views().items()):
            if not view.valid or view.best_ask is None or view.best_bid is None:
                continue
            spec = ctx.market.specs.get(inst_id)
            if spec is None:
                continue
            enter = ctx.boundary_index == 1 and inst_id not in state.entered
            exit_ = ctx.boundary_index == 3 and inst_id in state.entered and inst_id not in state.exited
            if not (enter or exit_):
                continue
            side = Side.BUY if enter else Side.SELL
            # Taille minimale admissible : le scénario prouve la chaîne, il ne prend pas de risque.
            contracts = round_contracts_risk_reducing(spec.min_size, spec)
            if contracts.value <= 0:
                continue
            try:
                validate_order_size(contracts, spec)
            except OkxqError:
                continue
            limit = view.best_ask if side is Side.BUY else view.best_bid
            now = ctx.market.last_available_at or ctx.cutoff_at
            # Identifiants DÉTERMINISTES pour un scénario de test : la latence simulée est dérivée du
            # client_order_id, donc des identifiants aléatoires rendraient le rapport irreproductible
            # (T70). En exploitation, l'identifiant est généré une fois puis persisté avant envoi (§52).
            tag = f"{ctx.boundary_index:03d}-{inst_id}-{side.value}"
            intent = OrderIntent(
                intent_id=f"int_replay_{tag}",
                decision_id=f"dec_replay_{tag}",
                target_id=f"tgt_replay_{tag}",
                account_scope=cfg.account.scope,
                inst_id=inst_id,
                side=side,
                contracts=contracts.abs,
                price_limit=limit,
                order_type=OrderKind.IOC,
                reduce_only=exit_,
                ttl_ms=cfg.execution.max_order_intent_age_ms,
                reason=TEST_POLICY_LABEL,
                client_order_id=f"cl_replay_{tag}",
                created_at=now,
                expires_at=now + timedelta(milliseconds=cfg.execution.max_order_intent_age_ms),
            )
            approved = _approved_order(intent, now=now)
            # Aucun ordre ne part sans ApprovedOrder : c'est le type lui-même qui l'impose.
            response = await ctx.exchange.place_order(
                OrderRequest(
                    account_scope=approved.intent.account_scope,
                    client_order_id=approved.intent.client_order_id,
                    inst_id=approved.intent.inst_id,
                    side=approved.intent.side.value,
                    contracts=approved.intent.contracts,
                    price_limit=approved.intent.price_limit,
                    order_type=approved.intent.order_type.value,
                    reduce_only=approved.intent.reduce_only,
                )
            )
            if enter:
                state.entered.add(inst_id)
            if exit_:
                state.exited.add(inst_id)
            sent.append(
                {
                    "inst_id": inst_id,
                    "side": side.value,
                    "contracts": format(contracts.abs, "f"),
                    "outcome": response.outcome.value,
                    "client_order_id": response.client_order_id,
                    "approved_payload_hash": approved.payload_hash,
                }
            )
        if not sent:
            return {
                "outcome": "NO_TRADE",
                "reason_codes": [ReasonCode.TURNOVER_NOT_JUSTIFIED.value],
                "policy": TEST_POLICY_LABEL,
            }
        return {"outcome": "TRADE", "orders": sent, "policy": TEST_POLICY_LABEL}

    engine = BacktestEngine(
        clock=clock,
        market=market,
        exchange=exchange,
        ledger=ledger,
        decision=decision,
        interval_seconds=cfg.runtime.decision_interval_seconds,
        decision_budget_ms=cfg.runtime.decision_budget_ms,
        dataset=str(manifest.get("dataset", dataset.name)),
    )
    report = await engine.run(events)
    out = report.as_dict()
    out["mode"] = cfg.mode.value
    out["scenario"] = scenario
    out["quality_level"] = manifest.get("quality_level")
    out["synthetic_warning"] = (
        "Résultats de SCÉNARIO DE TEST sur données synthétiques : aucune preuve d'avantage de marché."
        if manifest.get("quality_level") == "synthetic"
        else None
    )
    equity = ledger.equity({k: v for k, v in market.marks.items()})
    out["ledger"] = {
        "equity": format(equity.equity, "f"),
        "cash_collateral": format(equity.cash_collateral, "f"),
        "unrealized_pnl": format(equity.unrealized_pnl, "f"),
        "realized_pnl_cum": format(equity.realized_pnl_cum, "f"),
        "fees_cum": format(equity.fees_cum, "f"),
        "funding_cum": format(equity.funding_cum, "f"),
        "external_cashflow_cum": format(equity.external_cashflow_cum, "f"),
        "unmarked_instruments": list(equity.unmarked_instruments),
    }
    out["strategy_pnl"] = format(equity.equity - equity.external_cashflow_cum, "f")
    return out


@app.command("run")
def run(
    dataset: Path = typer.Option(..., "--dataset", exists=True, file_okay=False),
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    scenario: str = typer.Option("flat", "--scenario", help=f"Scénario de test : {', '.join(SCENARIOS)}"),
    assert_invariants: bool = typer.Option(
        False, "--assert-invariants", help="Sort en erreur si un invariant échoue."
    ),
    report: Path | None = typer.Option(None, "--report", help="Chemin du rapport JSON."),
    quiet: bool = typer.Option(False, "--quiet", help="N'affiche que la synthèse."),
) -> None:
    """Rejoue un jeu de données et écrit un rapport machine-readable."""
    cfg = load_or_exit(config)
    if scenario not in SCENARIOS:
        typer.echo(f"scénario inconnu : {scenario} (attendu : {', '.join(SCENARIOS)})", err=True)
        raise typer.Exit(code=1)
    try:
        result = asyncio.run(_run(cfg, dataset, scenario))
    except OkxqError as exc:
        typer.echo(f"replay impossible : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if quiet:
        emit(
            {
                "ok": result["ok"],
                "scenario": scenario,
                "mode": result["mode"],
                "events_applied": result["events_applied"],
                "boundaries": result["boundaries"],
                "fills": len(result["fills"]),
                "ledger_equity": result["ledger"]["equity"],
                "strategy_pnl": result["strategy_pnl"],
                "report": str(report) if report else None,
                "synthetic_warning": result["synthetic_warning"],
            }
        )
    else:
        emit(result)
    if assert_invariants and not result["ok"]:
        raise typer.Exit(code=1)
