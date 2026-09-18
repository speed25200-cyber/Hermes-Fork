"""Commandes de lancement : ``paper run``, ``shadow run``, ``demo preflight|run``, ``live ...`` (§67).

Le démarrage par défaut n'appelle aucun endpoint privé. ``live`` exécute intégralement le workflow §71
(garde par manifeste signé) et n'offre aucune option de contournement.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from datetime import UTC, datetime
from pathlib import Path

import typer

from okxq.cli_cmds._common import emit, load_or_exit, not_implemented
from okxq.config.live_guard import verify_live_authorization
from okxq.config.modes import Mode
from okxq.domain.errors import LiveGuardError

paper_app = typer.Typer(help="Mode PAPER : données publiques réelles, ordres simulés localement.")
shadow_app = typer.Typer(help="Mode SHADOW : décisions archivées avant résultat, aucun ordre.")
demo_app = typer.Typer(help="Mode DEMO : compte de démonstration OKX (x-simulated-trading).")
live_app = typer.Typer(help="Mode LIVE : DÉSACTIVÉ par défaut ; manifeste d'approbation signé requis.")

ROLES = ("all", "collector", "strategy", "risk", "gateway", "jev-worker", "api")


def _run(config: Path, expected: Mode, role: str, max_minutes: float | None) -> None:
    cfg = load_or_exit(config)
    if cfg.mode is not expected:
        typer.echo(
            f"la configuration est en mode {cfg.mode.value}, commande {expected.value} refusée", err=True
        )
        raise typer.Exit(code=1)
    if role not in ROLES:
        typer.echo(f"rôle inconnu : {role} (attendu : {', '.join(ROLES)})", err=True)
        raise typer.Exit(code=1)
    try:
        composition = importlib.import_module("okxq.runtime.composition")
    except ModuleNotFoundError:
        not_implemented(f"okxq {expected.value.lower()} run (composition non livrée)")
        return
    asyncio.run(composition.run_process(cfg, role=role, max_minutes=max_minutes))


@paper_app.command("run")
def paper_run(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    role: str = typer.Option("all", "--role", help="Processus à lancer : " + ", ".join(ROLES)),
    max_minutes: float | None = typer.Option(
        None, "--max-minutes", help="Arrêt contrôlé après N minutes (tests)."
    ),
) -> None:
    """Lance le mode PAPER (aucune clé OKX requise, aucun ordre réel)."""
    _run(config, Mode.PAPER, role, max_minutes)


@shadow_app.command("run")
def shadow_run(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    role: str = typer.Option("all", "--role"),
    max_minutes: float | None = typer.Option(None, "--max-minutes"),
) -> None:
    """Lance le mode SHADOW (forward test : décisions archivées, aucun ordre)."""
    _run(config, Mode.SHADOW, role, max_minutes)


@demo_app.command("preflight")
def demo_preflight(config: Path = typer.Option(..., "--config", exists=True, dir_okay=False)) -> None:
    """Vérifie clés DEMO, profil, en-tête x-simulated-trading, compte (net/isolated) — sans envoyer d'ordre."""
    cfg = load_or_exit(config)
    if cfg.mode is not Mode.DEMO:
        typer.echo("configuration non DEMO", err=True)
        raise typer.Exit(code=1)
    missing = [n for n in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE") if not os.environ.get(n)]
    if missing:
        emit(
            {
                "ok": False,
                "status": "NOT_RUN",
                "reason": f"clés DEMO absentes : {missing}",
                "live_fallback": "jamais",
            }
        )
        raise typer.Exit(code=1)
    try:
        preflight = importlib.import_module("okxq.exchange.okx.preflight")
    except ModuleNotFoundError:
        not_implemented("okxq demo preflight (connecteur DEMO non livré)")
        return
    result = asyncio.run(preflight.run(cfg))
    emit(result)
    raise typer.Exit(code=0 if result.get("ok") else 1)


@demo_app.command("run")
def demo_run(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    role: str = typer.Option("all", "--role"),
    max_minutes: float | None = typer.Option(None, "--max-minutes"),
) -> None:
    """Lance le mode DEMO (ordres sur le compte de démonstration OKX uniquement)."""
    _run(config, Mode.DEMO, role, max_minutes)


@live_app.command("preflight")
def live_preflight(config: Path = typer.Option(..., "--config", exists=True, dir_okay=False)) -> None:
    """Vérifie la garde LIVE (§71) : manifeste signé, gates, commit, hash de configuration. N'envoie rien."""
    cfg = load_or_exit(config)
    try:
        proofs = verify_live_authorization(
            cfg,
            manifest_path=Path(os.environ.get("OKXQ_LIVE_MANIFEST", "artifacts/live_approval.json")),
            operator_secret=os.environ.get("OPERATOR_AUTH_SECRET"),
            now=datetime.now(tz=UTC),
            code_commit=os.environ.get("OKXQ_CODE_COMMIT"),
        )
    except LiveGuardError as exc:
        emit({"ok": False, "status": "LIVE_NOT_AUTHORIZED", "reason": str(exc)})
        raise typer.Exit(code=1) from exc
    emit(
        {
            "ok": True,
            "status": "LIVE_GUARD_PASSED",
            "proofs": proofs,
            "note": "le preflight connecté reste requis",
        }
    )


@live_app.command("run")
def live_run(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    role: str = typer.Option("all", "--role"),
) -> None:
    """LIVE : refusé sans manifeste d'approbation valide. Il n'existe pas d'option --force."""
    cfg = load_or_exit(config)
    if cfg.mode is not Mode.LIVE:
        typer.echo("configuration non LIVE", err=True)
        raise typer.Exit(code=1)
    try:
        verify_live_authorization(
            cfg,
            manifest_path=Path(os.environ.get("OKXQ_LIVE_MANIFEST", "artifacts/live_approval.json")),
            operator_secret=os.environ.get("OPERATOR_AUTH_SECRET"),
            now=datetime.now(tz=UTC),
            code_commit=os.environ.get("OKXQ_CODE_COMMIT"),
        )
    except LiveGuardError as exc:
        emit({"ok": False, "status": "LIVE_NOT_AUTHORIZED", "reason": str(exc)})
        raise typer.Exit(code=1) from exc
    _run(config, Mode.LIVE, role, None)
