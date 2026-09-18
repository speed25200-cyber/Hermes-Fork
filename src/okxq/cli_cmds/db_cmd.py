"""``okxq db upgrade|migrate|downgrade|current|history|check`` : migrations de schéma (§44, §67).

Pourquoi un groupe CLI au lieu d'un simple appel à ``alembic`` :

- l'URL de connexion porte un mot de passe ; elle est transmise à Alembic par les attributs du
  contexte, donc jamais écrite dans ``alembic.ini`` ni dans un journal. Les sorties n'affichent qu'une
  forme masquée (``render_as_string(hide_password=True)``) ;
- une seule connexion sert à lire l'état et à migrer, pour que l'état rapporté soit celui de la base
  réellement migrée et pas celui d'une autre session ;
- la sortie est du JSON machine-readable : un opérateur et un script de déploiement lisent la même
  chose, et ``check`` peut servir de garde-fou avant démarrage ;
- ``downgrade`` détruit des données : il exige ``--confirmer EFFACER`` et n'a aucune valeur par défaut
  de révision cible. Rien ici ne peut effacer une base « par distraction ».
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Connection
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from okxq.cli_cmds._common import emit, load_or_exit
from okxq.persistence.db import make_engine
from okxq.persistence.models import Base

app = typer.Typer(
    help="Migrations de schéma (Alembic). Aucune commande n'efface sans confirmation explicite."
)

# Le dossier des migrations est résolu depuis le paquet installé, pas depuis le répertoire courant :
# la CLI doit se comporter de la même façon lancée depuis n'importe où (conteneur, cron, poste).
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "persistence" / "migrations"

# Ordre de lecture identique à ``env.py`` : ``OKXQ_DATABASE_URL`` d'abord pour viser une base de test
# sans toucher à la variable générique du déploiement.
URL_ENV_VARS: tuple[str, ...] = ("OKXQ_DATABASE_URL", "DATABASE_URL")

# Mot exact attendu pour une migration descendante. Il est en français et explicite : personne ne le
# tape par réflexe ni par copier-coller d'une commande de montée.
CONFIRM_WORD = "EFFACER"

_REVISION_UP = typer.Option("head", "--revision", help="Révision cible (défaut : head).")
_REVISION_DOWN = typer.Option(
    ...,
    "--revision",
    help="Révision cible (ex. 0001, -1, base). Obligatoire : une destruction ne s'improvise pas.",
)
_CONFIRM = typer.Option(
    "",
    "--confirmer",
    help=f"Doit valoir exactement {CONFIRM_WORD} : une migration descendante détruit des données.",
)
_CONFIG = typer.Option(
    None,
    "--config",
    exists=True,
    dir_okay=False,
    help="Configuration à valider avant de migrer (optionnel) ; son mode est rappelé dans la sortie.",
)


def _env_url() -> str | None:
    """Première variable renseignée, ou ``None`` : aucune base n'est déduite d'autre chose."""
    for name in URL_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    sqlite_path = os.environ.get("OKXQ_SQLITE_PATH")
    return f"sqlite+pysqlite:///{sqlite_path}" if sqlite_path else None


def _optional_url() -> str | None:
    """URL utilisable, ou ``None`` si aucune n'est désignée. Une URL illisible est refusée ici.

    La validation a lieu dès la lecture pour que toutes les commandes — y compris celles qui
    n'ouvrent pas forcément de connexion — échouent avec le même message, et sans montrer l'URL.
    """
    url = _env_url()
    if url is None:
        return None
    try:
        make_url(url)
    except ArgumentError as exc:
        # Le contenu n'est pas répété dans le message : il porte un mot de passe.
        typer.echo("URL de base illisible ; son contenu n'est pas affiché car il porte un secret.", err=True)
        raise typer.Exit(code=1) from exc
    return url


def _required_url() -> str:
    """URL obligatoire : aucune base par défaut, pour ne jamais migrer une base non désignée (§44)."""
    url = _optional_url()
    if url is None:
        typer.echo(
            "aucune base désignée : définir "
            + " ou ".join(URL_ENV_VARS)
            + " (ou OKXQ_SQLITE_PATH pour une base locale). Aucune valeur par défaut n'est appliquée.",
            err=True,
        )
        raise typer.Exit(code=1)
    return url


def _masked(url: str) -> str:
    """Forme affichable : SQLAlchemy remplace le mot de passe par ``***``."""
    return make_url(url).render_as_string(hide_password=True)


def _alembic_config(connection: Connection | None = None) -> Config:
    """Contexte Alembic construit en mémoire : ni fichier ini, ni URL en clair dans la configuration."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


@contextmanager
def _connected(url: str) -> Iterator[tuple[Config, Connection]]:
    """Une connexion unique, et une transaction possédée ici, partagées avec Alembic.

    La transaction appartient à l'appelant (``connection.begin()``) : une migration interrompue est
    annulée en bloc — jamais de schéma à moitié créé — et l'état lu avant et après est celui de la
    base réellement migrée. L'échec de connexion est rapporté avec l'URL masquée, jamais brute.
    """
    try:
        engine = make_engine(url)
        connection = engine.connect()
    except SQLAlchemyError as exc:
        # Diagnostic utile (hôte, port, pilote manquant) sans le mot de passe.
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        typer.echo(f"base inutilisable ({_masked(url)}) : {detail}", err=True)
        raise typer.Exit(code=1) from exc
    try:
        with connection.begin():
            yield _alembic_config(connection), connection
    finally:
        connection.close()
        engine.dispose()


def _script(cfg: Config) -> ScriptDirectory:
    return ScriptDirectory.from_config(cfg)


def _heads(cfg: Config) -> tuple[str, ...]:
    return tuple(_script(cfg).get_heads())


def _applied(connection: Connection) -> tuple[str, ...]:
    return MigrationContext.configure(connection).get_current_heads()


def _pending(cfg: Config, applied: tuple[str, ...]) -> list[str] | None:
    """Révisions restant à appliquer, ou ``None`` si la base porte une révision inconnue du dépôt."""
    try:
        revisions = _script(cfg).iterate_revisions("heads", applied if applied else "base")
        return [rev.revision for rev in revisions]
    except CommandError:
        # Cas réel : la base a été migrée par une autre version du code. On ne devine pas.
        return None


def _mode(config: Path | None) -> str | None:
    """Mode de la configuration fournie, ou ``None`` si l'opérateur n'en a pas donné."""
    if config is None:
        return None
    return load_or_exit(config).mode.value


def _label(item: Any) -> str | None:
    """Nom lisible d'un objet de schéma ; ``None`` quand Alembic n'a rien à nommer (schéma par défaut)."""
    if item is None:
        return None
    for attribute in ("fullname", "name"):
        value = getattr(item, attribute, None)
        if isinstance(value, str):
            return value
    return str(item)


def _describe(diff: Any) -> dict[str, Any]:
    """Traduit une directive Alembic en JSON stable, sans dépendre de son format interne."""
    if isinstance(diff, list):
        return {"kind": "modifications", "details": [_describe(item) for item in diff]}
    return {"kind": str(diff[0]), "details": [_label(item) for item in diff[1:]]}


def _state(url: str, cfg: Config, connection: Connection, extra: dict[str, Any]) -> dict[str, Any]:
    applied = _applied(connection)
    heads = _heads(cfg)
    return {
        "database": _masked(url),
        # Aucune révision appliquée : absence de donnée, donc ``None`` et pas une liste vide trompeuse.
        "current": list(applied) or None,
        "heads": list(heads),
        "at_head": bool(applied) and set(applied) == set(heads),
        "pending": _pending(cfg, applied),
        **extra,
    }


def _upgrade(revision: str, config: Path | None) -> None:
    mode = _mode(config)
    url = _required_url()
    with _connected(url) as (cfg, connection):
        before = _applied(connection)
        command.upgrade(cfg, revision)
        after = _applied(connection)
        payload = _state(
            url,
            cfg,
            connection,
            {
                "mode": mode,
                "revision_requested": revision,
                "revision_before": list(before) or None,
                "applied_now": [rev for rev in after if rev not in before],
            },
        )
    emit(payload)


@app.command("upgrade")
def upgrade(revision: str = _REVISION_UP, config: Path | None = _CONFIG) -> None:
    """Applique les migrations manquantes jusqu'à la révision demandée (création de schéma, non destructive)."""
    _upgrade(revision, config)


@app.command("migrate")
def migrate(revision: str = _REVISION_UP, config: Path | None = _CONFIG) -> None:
    """Alias de ``upgrade`` attendu par le §67 (``okxq db migrate --config ...``)."""
    _upgrade(revision, config)


@app.command("downgrade")
def downgrade(
    revision: str = _REVISION_DOWN, confirmer: str = _CONFIRM, config: Path | None = _CONFIG
) -> None:
    """DESTRUCTIF : redescend le schéma et perd les données concernées. Sauvegarde vérifiée exigée (§44)."""
    if confirmer != CONFIRM_WORD:
        typer.echo(
            "migration descendante refusée : elle détruit des données. Vérifier d'abord qu'une "
            f"sauvegarde est restaurable (§44), puis relancer avec --confirmer {CONFIRM_WORD}.",
            err=True,
        )
        raise typer.Exit(code=1)
    mode = _mode(config)
    url = _required_url()
    with _connected(url) as (cfg, connection):
        before = _applied(connection)
        command.downgrade(cfg, revision)
        after = _applied(connection)
        payload = _state(
            url,
            cfg,
            connection,
            {
                "mode": mode,
                "revision_requested": revision,
                "revision_before": list(before) or None,
                "removed_now": [rev for rev in before if rev not in after],
                "destructive": True,
            },
        )
    emit(payload)


@app.command("current")
def current() -> None:
    """Révision appliquée en base, révisions en attente et écart éventuel avec la tête du dépôt."""
    url = _required_url()
    with _connected(url) as (cfg, connection):
        payload = _state(url, cfg, connection, {})
    emit(payload)


@app.command("history")
def history() -> None:
    """Historique des révisions du dépôt ; ``applied`` reste ``None`` si aucune base n'est désignée."""
    url = _optional_url()
    applied: tuple[str, ...] | None = None
    if url is not None:
        with _connected(url) as (_cfg, connection):
            applied = _applied(connection)
    cfg = _alembic_config()
    rows = [
        {
            "revision": script.revision,
            "down_revision": script.down_revision,
            "is_head": script.is_head,
            "doc": script.doc,
            # ``None`` quand on ne sait pas : ne jamais afficher « non appliquée » sans base consultée.
            "applied": None if applied is None else script.revision in applied,
        }
        for script in _script(cfg).walk_revisions()
    ]
    emit(
        {
            "database": None if url is None else _masked(url),
            "heads": list(_heads(cfg)),
            "revisions": rows,
        }
    )


@app.command("check")
def check() -> None:
    """Compare le schéma en base aux modèles. Sort en code 1 s'il diverge ; ne corrige jamais tout seul."""
    url = _required_url()
    with _connected(url) as (cfg, connection):
        context = MigrationContext.configure(
            connection,
            # Les types sont comparés : un NUMERIC de précision différente est une divergence réelle
            # (§44, tests de dépassement). Les valeurs par défaut serveur sont comparées de la même
            # façon, car un DEFAULT manquant change le comportement des insertions concurrentes.
            opts={"compare_type": True, "compare_server_default": True},
        )
        differences = [_describe(diff) for diff in compare_metadata(context, Base.metadata)]
        payload = _state(url, cfg, connection, {})
    aligned = payload["at_head"] is True and not differences
    payload["differences"] = differences
    payload["aligned"] = aligned
    emit(payload)
    if not aligned:
        raise typer.Exit(code=1)
