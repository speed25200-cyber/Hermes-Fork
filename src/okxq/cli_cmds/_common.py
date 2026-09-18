"""Aides communes aux commandes : chargement de config, sortie JSON, code de sortie NOT_IMPLEMENTED."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.orm import Session, sessionmaker

from okxq.config import AppConfig, load_config
from okxq.domain.errors import ConfigError
from okxq.persistence.db import create_all, make_engine, make_session_factory

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


def database_url_from_env() -> str:
    """``DATABASE_URL`` (PostgreSQL) ou, à défaut, une base SQLite locale ``OKXQ_SQLITE_PATH``.

    Aucune valeur par défaut silencieuse : sans l'une des deux variables, la commande échoue.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    sqlite_path = os.environ.get("OKXQ_SQLITE_PATH")
    if sqlite_path:
        return f"sqlite+pysqlite:///{sqlite_path}"
    typer.echo("DATABASE_URL ou OKXQ_SQLITE_PATH requis pour lire l'état de risque", err=True)
    raise typer.Exit(code=1)


def session_factory_from_env() -> sessionmaker[Session]:
    """Fabrique de sessions ; pour SQLite local le schéma est créé s'il manque (PostgreSQL : migrations)."""
    url = database_url_from_env()
    engine = make_engine(url)
    if url.startswith("sqlite"):
        create_all(engine)
    return make_session_factory(engine)
