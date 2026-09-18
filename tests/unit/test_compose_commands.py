"""Les commandes déclarées dans `compose.yaml` doivent exister et être acceptées par la CLI.

Le service de migration portait `okxq db upgrade head`. `head` est une OPTION, pas un positionnel :
la CLI rejetait la commande. Le service échouait donc à chaque démarrage, et comme les cinq rôles
écrivains attendent sa terminaison réussie, aucun ne démarrait jamais — seule l'API montait, donnant
une console vivante devant un système mort. Une erreur d'une seule ligne, invisible sans déployer.

Ce test relit `compose.yaml` et soumet chaque commande à l'analyseur d'arguments de la vraie CLI,
sans rien exécuter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer._click.core import Context
from typer._click.exceptions import UsageError
from typer.main import get_command

from okxq.cli import app

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.yaml"


def compose_commands() -> list[tuple[str, list[str]]]:
    """Commandes `okxq` déclarées par les services, sous la forme (service, arguments)."""
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    out: list[tuple[str, list[str]]] = []
    for name, service in (document.get("services") or {}).items():
        command = service.get("command")
        if not isinstance(command, list) or not command or command[0] != "okxq":
            continue
        out.append((name, [str(part) for part in command[1:]]))
    return out


def test_compose_declares_at_least_one_okxq_command() -> None:
    """Garde-fou du garde-fou : un test qui ne trouve rien à vérifier ne vérifie rien."""
    assert compose_commands(), "aucune commande okxq trouvée dans compose.yaml"


@pytest.mark.parametrize(("service", "arguments"), compose_commands(), ids=[s for s, _ in compose_commands()])
def test_every_compose_command_is_accepted_by_the_cli(service: str, arguments: list[str]) -> None:
    """Soumet la commande à l'ANALYSEUR d'arguments de la vraie CLI, sans rien exécuter.

    Deux pièges ont rendu deux versions précédentes de ce test VACUOUS, et ils valent d'être écrits :

    1. passer par `--help` ne valide rien : l'aide court-circuite l'analyse et sort en 0 même avec un
       positionnel de trop ;
    2. Typer embarque sa PROPRE copie de Click. Un `isinstance(commande, click.Group)` contre le
       paquet `click` installé est toujours faux, donc la descente dans les sous-groupes ne se faisait
       pas et l'analyse portait sur le groupe racine — qui accepte n'importe quel argument.

    D'où le typage canard (`get_command`) et l'exception importée depuis la copie de Typer.
    """
    command: Any = get_command(app)
    context: Any = Context(command, info_name="okxq")
    remaining = list(arguments)
    # Descente dans la chaîne de sous-groupes (`db` → `upgrade`).
    while hasattr(command, "get_command") and remaining:
        name = remaining.pop(0)
        sub = command.get_command(context, name)
        assert sub is not None, f"service {service} : sous-commande inconnue « {name} »"
        context = Context(sub, parent=context, info_name=name)
        command = sub
    assert not hasattr(command, "get_command"), (
        f"service {service} : `okxq {' '.join(arguments)}` s'arrête sur un groupe, pas une commande"
    )
    try:
        command.make_context(info_name=command.name, args=remaining, parent=context)
    except UsageError as exc:  # option inconnue, positionnel de trop, valeur manquante
        raise AssertionError(
            f"service {service} : `okxq {' '.join(arguments)}` refusé par la CLI — {exc}"
        ) from exc
