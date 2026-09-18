"""CLI ``okxq`` (§67). Les groupes sont montés depuis ``okxq.cli_cmds``."""

from __future__ import annotations

import importlib

import typer

from okxq import __version__

app = typer.Typer(
    help="okx-quant-jev — plateforme quantitative OKX + JEV. LIVE désactivé par défaut.", no_args_is_help=True
)

# (nom de sous-commande, module, attribut)
_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("config", "okxq.cli_cmds.config_cmd", "app"),
    ("doctor", "okxq.cli_cmds.config_cmd", "doctor_app"),
    ("db", "okxq.cli_cmds.db_cmd", "app"),
    ("data", "okxq.cli_cmds.data_cmd", "app"),
    ("replay", "okxq.cli_cmds.replay_cmd", "app"),
    ("research", "okxq.cli_cmds.research_cmd", "app"),
    ("backtest", "okxq.cli_cmds.backtest_cmd", "app"),
    ("paper", "okxq.cli_cmds.run_cmd", "paper_app"),
    ("shadow", "okxq.cli_cmds.run_cmd", "shadow_app"),
    ("demo", "okxq.cli_cmds.run_cmd", "demo_app"),
    ("live", "okxq.cli_cmds.run_cmd", "live_app"),
    ("jev", "okxq.cli_cmds.jev_cmd", "app"),
    ("risk", "okxq.cli_cmds.risk_cmd", "app"),
    ("control", "okxq.cli_cmds.control_cmd", "app"),
    ("reports", "okxq.cli_cmds.reports_cmd", "app"),
    ("api", "okxq.cli_cmds.api_cmd", "app"),
)

for _name, _module, _attr in _GROUPS:
    try:
        _mod = importlib.import_module(_module)
    except ModuleNotFoundError as exc:  # groupe pas encore livré : la commande n'apparaît pas
        if exc.name != _module:
            raise
        continue
    app.add_typer(getattr(_mod, _attr), name=_name)


@app.callback()
def _root(version: bool = typer.Option(False, "--version", help="Affiche la version.")) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


if __name__ == "__main__":  # pragma: no cover
    app()
