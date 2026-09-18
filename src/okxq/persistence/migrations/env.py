"""Contexte d'exécution des migrations.

Deux principes commandent ce fichier :

1. L'URL de connexion porte un mot de passe. Elle ne doit donc apparaître ni dans `alembic.ini`, ni
   dans un message, ni dans un journal — même à l'occasion d'une erreur. Les messages d'échec parlent
   du *nom de la variable* attendue, jamais de son contenu.
2. Aucune base par défaut. Sans URL explicite, la migration s'arrête : migrer « la base locale » par
   défaut ferait courir le risque d'écrire dans une base DEMO ou LIVE que personne n'a désignée
   (§44 : ne jamais mélanger les bases DEMO et LIVE).

Trois façons de désigner la base, par ordre de priorité : une connexion déjà ouverte
(`config.attributes["connection"]` — c'est ce que fait `okxq db`, pour posséder la transaction et lire
l'état sur la même connexion que la migration), une URL passée par le programme appelant
(`config.attributes["okxq_url"]`, sans passer par l'environnement ni par un fichier), ou les variables
d'environnement ci-dessous, utilisées quand la commande `alembic` est lancée directement.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, create_engine
from sqlalchemy.engine import URL

from okxq.persistence.models import Base

config = context.config

# `alembic.ini` n'existe que lorsque la commande passe par l'exécutable `alembic` ; piloté depuis la
# CLI `okxq db`, le contexte est construit en mémoire et la configuration de journalisation de
# l'application ne doit pas être écrasée.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Source unique de vérité du schéma (§44) : la comparaison d'autogénération se fait contre les modèles.
target_metadata = Base.metadata

# Ordre de lecture documenté ; `OKXQ_DATABASE_URL` d'abord pour permettre de viser une base de test
# sans écraser la variable générique du déploiement.
URL_ENV_VARS: tuple[str, ...] = ("OKXQ_DATABASE_URL", "DATABASE_URL")


def resolve_url() -> str | URL:
    """URL de connexion, sans jamais la révéler en cas d'échec."""
    provided = config.attributes.get("okxq_url")
    if provided is not None:
        return provided if isinstance(provided, URL) else str(provided)
    for name in URL_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    raise RuntimeError(
        "URL de base absente : définir "
        + " ou ".join(URL_ENV_VARS)
        + " (le contenu n'est jamais affiché ni journalisé)"
    )


def _dialect_is_sqlite(url: str | URL) -> bool:
    name = url.get_backend_name() if isinstance(url, URL) else url.split(":", 1)[0].split("+", 1)[0]
    return name.startswith("sqlite")


def _configure_kwargs(*, sqlite: bool) -> dict[str, Any]:
    """Options communes aux modes hors ligne et en ligne.

    `render_as_batch` n'est activé que sur SQLite : ce moteur ne sait pas modifier une colonne en
    place, et sans mode « batch » une migration ultérieure échouerait au lieu de recréer la table.
    """
    return {
        "target_metadata": target_metadata,
        "compare_type": True,
        "compare_server_default": True,
        "render_as_batch": sqlite,
        # Une transaction par migration lorsque c'est Alembic qui possède la connexion : une révision
        # interrompue ne laisse pas un schéma à moitié créé. Si l'appelant a déjà ouvert la
        # transaction (CLI, tests), elle reste la sienne et l'annulation est globale.
        "transaction_per_migration": True,
    }


def run_migrations_offline() -> None:
    """Génère le SQL sans se connecter (`--sql`) : utile pour faire relire un changement avant de l'appliquer."""
    url = resolve_url()
    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(sqlite=_dialect_is_sqlite(url)),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Applique les migrations sur une connexion fournie par l'appelant, ou sur une connexion ouverte ici."""
    existing = config.attributes.get("connection")
    if isinstance(existing, Connection):
        # L'appelant possède la transaction (tests, ou migration incluse dans une opération plus large).
        sqlite = _dialect_is_sqlite(existing.engine.url)
        context.configure(connection=existing, **_configure_kwargs(sqlite=sqlite))
        with context.begin_transaction():
            context.run_migrations()
        return

    url = resolve_url()
    # `echo=False` explicite : le journal SQL de SQLAlchemy afficherait l'URL et les paramètres.
    engine = create_engine(url, echo=False, future=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, **_configure_kwargs(sqlite=_dialect_is_sqlite(url)))
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
