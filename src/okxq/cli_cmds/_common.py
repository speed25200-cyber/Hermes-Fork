"""Aides communes aux commandes : chargement de config, sortie JSON, code de sortie NOT_IMPLEMENTED."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from okxq.config import AppConfig, load_config
from okxq.domain.errors import ConfigError

NOT_IMPLEMENTED_EXIT = 2


def load_or_exit(config: Path) -> AppConfig:
    try:
        return load_config(config)
    except ConfigError as exc:
        typer.echo(f"configuration refusée : {exc}", err=True)
        raise typer.Exit(code=1) from exc


def emit(obj: Any) -> None:
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def not_implemented(what: str) -> None:
    """Sortie honnête : la commande existe, elle n'est pas fonctionnelle."""
    typer.echo(f"NOT_IMPLEMENTED: {what}", err=True)
    sys.exit(NOT_IMPLEMENTED_EXIT)
