"""``okxq api run|keys|routes`` : service de lecture et de pilotage audité (§59, §67).

L'API ne place aucun ordre. Elle lit l'état et enregistre des DEMANDES opérateur que le runtime
exécute sous préconditions. C'est pour cela qu'elle peut tourner dans un conteneur sans aucun
identifiant d'échange, et c'est une propriété qu'il ne faut pas perdre.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import typer

from okxq.api.app import create_app, principal_roles, role_key_for
from okxq.api.auth import Role
from okxq.cli_cmds._common import emit, load_or_exit
from okxq.domain.errors import OkxqError

app = typer.Typer(help="Service HTTP de lecture et de demandes opérateur (jamais d'envoi d'ordre).")

#: Permissions du fichier de clés : lisible par son seul propriétaire. Une clé lisible par le groupe
#: sur un hôte partagé n'est plus une clé.
KEYFILE_MODE = stat.S_IRUSR | stat.S_IWUSR


def _operator_secret() -> str:
    secret = os.environ.get("OPERATOR_AUTH_SECRET", "")
    if not secret:
        typer.echo(
            "OPERATOR_AUTH_SECRET absent : une API de pilotage joignable sans clé n'est pas une "
            "commodité, c'est une porte ouverte",
            err=True,
        )
        raise typer.Exit(code=1)
    return secret


@app.command("run")
def run(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface d'écoute (boucle locale par défaut)."),
    port: int = typer.Option(8080, "--port", min=1, max=65535),
    frontend: Path | None = typer.Option(
        None, "--frontend", help="Répertoire de l'interface à servir (défaut : ./frontend)."
    ),
    synthetic_data: bool = typer.Option(
        False, "--synthetic-data", help="Affiche le bandeau « données synthétiques » dans l'interface."
    ),
) -> None:
    """Lance le service HTTP.

    L'écoute est sur la boucle locale PAR DÉFAUT : exposer une interface de pilotage sur toutes les
    interfaces doit être un choix explicite de l'opérateur, pas l'effet d'une valeur par défaut.
    """
    import uvicorn

    cfg = load_or_exit(config)
    secret = _operator_secret()
    directory = frontend if frontend is not None else Path("frontend")
    try:
        application = create_app(
            cfg,
            operator_secret=secret,
            frontend_dir=directory if directory.is_dir() else None,
            synthetic_data=synthetic_data,
        )
    except OkxqError as exc:
        emit({"ok": False, "error": exc.code, "message": str(exc), **exc.context})
        raise typer.Exit(code=1) from exc
    # `log_config=None` : uvicorn ne doit PAS reconfigurer le logging du processus. Sa configuration
    # par défaut coupe la propagation vers notre handler masqué, et le masquage des secrets ne peut
    # pas dépendre d'un ordre d'import.
    uvicorn.run(
        application,
        host=host,
        port=port,
        log_config=None,
        access_log=cfg.observability.log_level.upper() == "DEBUG",
    )


@app.command("keys")
def keys(
    out: Path = typer.Option(
        Path("artifacts/api_keys.txt"), "--out", help="Fichier de destination (permissions 0600)."
    ),
    show: bool = typer.Option(
        False,
        "--montrer",
        help="Écrit aussi les clés sur la sortie standard. À éviter : historique de shell et journaux.",
    ),
) -> None:
    """Dérive les clés d'accès par rôle depuis ``OPERATOR_AUTH_SECRET``.

    Les clés ne sont PAS affichées par défaut. Une clé passée sur la sortie standard finit dans
    l'historique du shell, dans les journaux du terminal et souvent dans un presse-papiers : elle
    cesse d'être un secret au moment où on la lit. Le fichier produit est en 0600, et la commande
    n'affiche que son chemin et une empreinte tronquée permettant de vérifier qu'on parle bien de la
    même clé sans la divulguer.
    """
    secret = _operator_secret()
    derived = {role: role_key_for(secret, Role(role)) for role in principal_roles()}
    out.parent.mkdir(parents=True, exist_ok=True)
    # Le fichier est créé avec ses permissions finales AVANT d'écrire : le créer en lecture large
    # puis le restreindre laisse une fenêtre où la clé est lisible par tous.
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, KEYFILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("# Clés d'accès dérivées par rôle. Ne pas committer, ne pas coller en conversation.\n")
        for role, key in derived.items():
            handle.write(f"{role}={key}\n")
    os.chmod(out, KEYFILE_MODE)
    emit(
        {
            "ok": True,
            "fichier": str(out),
            "permissions": oct(stat.S_IMODE(out.stat().st_mode)),
            "roles": sorted(derived),
            # Empreinte tronquée : suffisante pour comparer, insuffisante pour s'authentifier.
            "empreintes": {role: key[:8] + "…" for role, key in derived.items()},
            "avertissement": "les clés ne sont pas affichées ; utiliser --montrer en connaissance de cause",
        }
    )
    if show:
        for role, key in derived.items():
            typer.echo(f"{role}={key}")


def _minimum_role(route: Any) -> str | None:
    """Rôle minimal exigé par une route, lu dans la fermeture de sa dépendance ``require_role``.

    C'est de l'introspection, donc faillible : si la dépendance change de forme, cette fonction doit
    renvoyer ``None`` plutôt qu'une valeur inventée. Annoncer « reader » sur une route en réalité
    ouverte serait pire que ne rien annoncer.
    """
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return None
    for dependency in getattr(dependant, "dependencies", []):
        call = getattr(dependency, "call", None)
        closure = getattr(call, "__closure__", None) or ()
        for cell in closure:
            contents = cell.cell_contents
            if isinstance(contents, Role):
                return contents.value
    return None


def _flatten(routes: Any, prefix: str = "") -> list[dict[str, Any]]:
    """Aplati récursivement la table de routage.

    FastAPI range les routeurs inclus dans des conteneurs : ne regarder qu'un niveau sous-déclare la
    surface exposée. Pour un inventaire de sécurité, une sous-déclaration est le pire résultat
    possible — on croit avoir tout vu.
    """
    out: list[dict[str, Any]] = []
    for route in routes:
        path = prefix + str(getattr(route, "path", "") or "")
        # Un routeur inclus est exposé soit par `routes` (montage), soit derrière `original_router`
        # selon la version de FastAPI. Manquer l'un des deux sous-déclare la surface, donc on suit
        # les deux et on ne suppose rien de la forme du conteneur.
        nested = getattr(route, "routes", None)
        if not nested:
            included = getattr(route, "original_router", None)
            nested = getattr(included, "routes", None) if included is not None else None
        if nested:
            out.extend(_flatten(nested, path))
            continue
        if not path:
            continue
        methods = getattr(route, "methods", None)
        out.append(
            {
                "path": path,
                "methods": sorted(m for m in (methods or []) if m != "HEAD"),
                "name": getattr(route, "name", None),
                "role_minimal": _minimum_role(route),
                "type": type(route).__name__,
            }
        )
    return out


@app.command("routes")
def routes(config: Path = typer.Option(..., "--config", exists=True, dir_okay=False)) -> None:
    """Inventaire complet des routes servies et du rôle minimal exigé. Aucune écoute réseau."""
    cfg = load_or_exit(config)
    # Valeur locale et jetable : cette commande n'ouvre aucun port et ne sert aucune requête,
    # elle instancie l'application uniquement pour lire sa table de routage.
    application = create_app(cfg, operator_secret="inventaire-local-sans-ecoute", configure_logs=False)  # noqa: S106
    inventory = sorted(_flatten(application.routes), key=lambda r: (str(r["path"]), r["methods"]))
    sans_role = [r["path"] for r in inventory if r["role_minimal"] is None]
    emit(
        {
            "ok": True,
            "count": len(inventory),
            # Une route sans rôle détecté n'est pas forcément ouverte : elle peut être servie par le
            # middleware d'authentification, ou l'introspection peut avoir échoué. Les deux cas
            # méritent un regard, donc ils sont listés.
            "routes_sans_role_detecte": sans_role,
            "routes": inventory,
        }
    )
