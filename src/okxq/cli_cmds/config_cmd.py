"""``okxq config validate`` et ``okxq doctor``."""

from __future__ import annotations

import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

from okxq import __version__
from okxq.cli_cmds._common import emit, load_or_exit
from okxq.config.live_guard import verify_live_authorization
from okxq.config.modes import Mode
from okxq.domain.errors import LiveGuardError

app = typer.Typer(help="Configuration : validation stricte et diagnostic.")
doctor_app = typer.Typer(help="Diagnostic de l'environnement.")


@app.command("validate")
def validate(config: Path = typer.Option(..., "--config", exists=True, dir_okay=False)) -> None:
    """Valide un fichier de configuration (schéma strict, contradictions, absence de secrets)."""
    cfg = load_or_exit(config)
    emit(
        {
            "ok": True,
            "path": cfg.source_path,
            "mode": cfg.mode.value,
            "live_enabled": cfg.project.live_enabled,
            "config_hash": cfg.config_hash,
            "fixture_only": cfg.project.fixture_only,
        }
    )


def _check_secret_presence(name: str) -> str:
    return "présent" if os.environ.get(name) else "absent"


@doctor_app.callback(invoke_without_command=True)
def doctor(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    offline: bool = typer.Option(True, "--offline/--online", help="Sans réseau (défaut)."),
) -> None:
    """Vérifie l'environnement : Python, dépendances, secrets (présence seulement), mode, garde LIVE."""
    cfg = load_or_exit(config)
    checks: list[dict[str, str]] = []
    checks.append({"check": "python", "status": "ok", "detail": platform.python_version()})
    checks.append({"check": "okxq", "status": "ok", "detail": __version__})
    for mod in (
        "pydantic",
        "sqlalchemy",
        "polars",
        "pyarrow",
        "duckdb",
        "sklearn",
        "lightgbm",
        "cvxpy",
        "fastapi",
    ):
        try:
            __import__(mod)
            checks.append({"check": f"import {mod}", "status": "ok", "detail": ""})
        except Exception as exc:  # pragma: no cover - dépend de l'installation
            checks.append({"check": f"import {mod}", "status": "fail", "detail": str(exc)})
    checks.append({"check": "mode", "status": "ok", "detail": cfg.mode.value})
    checks.append(
        {"check": "DATABASE_URL", "status": "info", "detail": _check_secret_presence("DATABASE_URL")}
    )
    checks.append(
        {
            "check": "OPERATOR_AUTH_SECRET",
            "status": "info",
            "detail": _check_secret_presence("OPERATOR_AUTH_SECRET"),
        }
    )
    if cfg.mode.uses_private_streams:
        for name in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
            checks.append({"check": name, "status": "info", "detail": _check_secret_presence(name)})
    if cfg.jev.enabled:
        checks.append(
            {
                "check": "TYPESAFE_API_KEY",
                "status": "info",
                "detail": _check_secret_presence("TYPESAFE_API_KEY"),
            }
        )
    if cfg.mode is Mode.LIVE:
        try:
            verify_live_authorization(
                cfg,
                manifest_path=Path(os.environ.get("OKXQ_LIVE_MANIFEST", "artifacts/live_approval.json")),
                operator_secret=os.environ.get("OPERATOR_AUTH_SECRET"),
                now=datetime.now(tz=UTC),
                code_commit=os.environ.get("OKXQ_CODE_COMMIT"),
            )
            checks.append({"check": "live_guard", "status": "ok", "detail": "manifeste vérifié"})
        except LiveGuardError as exc:
            checks.append({"check": "live_guard", "status": "blocked", "detail": str(exc)})
    checks.append(
        {"check": "network", "status": "skipped" if offline else "not_run", "detail": "mode hors ligne"}
    )
    failed = [c for c in checks if c["status"] == "fail"]
    emit({"ok": not failed, "checks": checks})
    if failed:
        sys.exit(1)
