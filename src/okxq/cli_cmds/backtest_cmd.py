"""``okxq backtest run|stress`` : parcours HORS LIGNE sur un jeu archivé, avec invariants (§68).

Ce groupe ne mesure aucun avantage de marché et ne doit pas être lu comme tel. Il répond à deux
questions vérifiables :

- la chaîne complète tient-elle sur un historique donné, et la comptabilité reste-t-elle équilibrée ?
- le résultat survit-il à une hypothèse de coûts plus dure que celle retenue ?

La seconde question est la plus utile. Un résultat qui disparaît dès qu'on stresse les coûts n'était
pas un résultat : c'était une erreur d'estimation. C'est pourquoi ``stress`` existe à côté de ``run``
et pourquoi son verdict est explicite.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer

from okxq.cli_cmds._common import emit, load_or_exit
from okxq.cli_cmds.replay_cmd import SCENARIOS, _run
from okxq.config.schema import AppConfig
from okxq.domain.errors import OkxqError
from okxq.domain.money import dec

app = typer.Typer(help="Backtest hors ligne sur données archivées (aucun réseau, aucun endpoint privé).")

#: Multiplicateurs de coûts appliqués par ``stress``. Le premier est la référence ; les suivants
#: durcissent l'hypothèse. Ils ne sont pas configurables : un stress qu'on peut assouplir jusqu'à
#: obtenir le verdict souhaité ne teste plus rien.
STRESS_MULTIPLIERS: tuple[str, ...] = ("1", "2", "3")


def _report_path(base: Path | None, suffix: str) -> Path | None:
    if base is None:
        return None
    return base.with_name(f"{base.stem}-{suffix}{base.suffix or '.json'}")


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _stressed(cfg: AppConfig, multiplier: Decimal) -> AppConfig:
    """Configuration identique, hypothèse de coûts durcie.

    Seules les hypothèses de COÛT bougent : durcir aussi les limites de risque mélangerait deux
    questions et rendrait le verdict illisible.
    """
    execution = cfg.execution.model_copy(
        update={
            # Une participation admissible plus faible force des tailles plus petites, donc plus de
            # passages, donc plus de frais : c'est la traduction la plus honnête d'un marché plus dur.
            # `dec` refuse un float par conception (un float monétaire est une erreur silencieuse) :
            # la fraction passe par son écriture décimale exacte avant division.
            "max_order_participation_fraction": float(
                dec(str(cfg.execution.max_order_participation_fraction)) / multiplier
            ),
            "max_depth_consumption_fraction": float(
                dec(str(cfg.execution.max_depth_consumption_fraction)) / multiplier
            ),
        }
    )
    return cfg.model_copy(update={"execution": execution})


@app.command("run")
def run(
    dataset: Path = typer.Option(..., "--dataset", exists=True, file_okay=False),
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    scenario: str = typer.Option("scripted", "--scenario", help=f"Scénario : {', '.join(SCENARIOS)}"),
    report: Path | None = typer.Option(None, "--report", help="Chemin du rapport JSON."),
    assert_invariants: bool = typer.Option(
        True, "--assert-invariants/--no-assert-invariants", help="Échoue si un invariant est violé."
    ),
) -> None:
    """Exécute un parcours complet et vérifie les invariants comptables et d'exécution."""
    cfg = load_or_exit(config)
    if scenario not in SCENARIOS:
        typer.echo(f"scénario inconnu : {scenario} (attendu : {', '.join(SCENARIOS)})", err=True)
        raise typer.Exit(code=1)
    try:
        result = asyncio.run(_run(cfg, dataset, scenario))
    except OkxqError as exc:
        emit({"ok": False, "error": exc.code, "message": str(exc), **exc.context})
        raise typer.Exit(code=1) from exc
    if report is not None:
        _write(report, result)
    emit(
        {
            "ok": result["ok"],
            "scenario": scenario,
            "mode": result["mode"],
            "dataset": result.get("dataset"),
            "boundaries": result["boundaries"],
            "fills": len(result["fills"]),
            "strategy_pnl": result["strategy_pnl"],
            "invariants": result["invariants"],
            "synthetic_warning": result["synthetic_warning"],
            "report": str(report) if report else None,
        }
    )
    if assert_invariants and not result["ok"]:
        raise typer.Exit(code=1)


@app.command("stress")
def stress(
    dataset: Path = typer.Option(..., "--dataset", exists=True, file_okay=False),
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    scenario: str = typer.Option("scripted", "--scenario"),
    report: Path | None = typer.Option(None, "--report", help="Rapport JSON de synthèse."),
) -> None:
    """Rejoue le même parcours sous des hypothèses de coûts croissantes et rend un verdict.

    ``survit_au_stress`` est vrai seulement si le résultat de stratégie ne se dégrade pas d'un
    multiplicateur à l'autre au point de changer de signe. Un résultat qui ne survit pas n'est pas
    un mauvais résultat : c'est l'indication que les coûts étaient sous-estimés.
    """
    cfg = load_or_exit(config)
    runs: list[dict[str, Any]] = []
    for raw in STRESS_MULTIPLIERS:
        multiplier = dec(raw)
        try:
            result = asyncio.run(_run(_stressed(cfg, multiplier), dataset, scenario))
        except OkxqError as exc:
            emit(
                {
                    "ok": False,
                    "multiplicateur": raw,
                    "error": exc.code,
                    "message": str(exc),
                    **exc.context,
                }
            )
            raise typer.Exit(code=1) from exc
        runs.append(
            {
                "multiplicateur": raw,
                "ok": result["ok"],
                "strategy_pnl": result["strategy_pnl"],
                "fills": len(result["fills"]),
                "invariants_ok": result["ok"],
            }
        )
    reference = dec(runs[0]["strategy_pnl"])
    worst = min(dec(r["strategy_pnl"]) for r in runs)
    # Le signe est le critère : un résultat positif qui devient négatif sous stress n'est pas robuste.
    survives = all(r["ok"] for r in runs) and (reference <= 0 or worst > 0)
    payload = {
        "ok": all(r["ok"] for r in runs),
        "scenario": scenario,
        "mode": cfg.mode.value,
        "multiplicateurs": list(STRESS_MULTIPLIERS),
        "runs": runs,
        "reference_pnl": format(reference, "f"),
        "pire_pnl": format(worst, "f"),
        "survit_au_stress": survives,
        "avertissement": (
            "Scénario de test sur données archivées : aucune preuve d'avantage de marché, quel que "
            "soit le verdict de robustesse."
        ),
    }
    if report is not None:
        _write(report, payload)
    emit(payload)
    if not payload["ok"]:
        raise typer.Exit(code=1)
