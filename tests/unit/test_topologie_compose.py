"""Le processus qui DÉCIDE doit être celui qui COLLECTE (§52, §55).

L'état de marché vit en mémoire, dans le processus qui le remplit. Seul le rôle `collector` collecte,
et il n'existe aucun transport de sa mémoire vers celle des autres rôles.

Dans le découpage en six conteneurs, le processus qui décide n'avait donc JAMAIS vu une donnée de
marché. Sa porte de données restait fermée, le kill switch passait en SOFT_HALT sur `data_stale`, et
chaque frontière rendait NO_TRADE — indéfiniment, sans qu'aucun service ne soit en panne. Sept
conteneurs sains, des journaux sans erreur, et une plateforme incapable de rien.

C'est le genre de défaut qu'aucun test unitaire ne voit : chaque pièce fonctionne, c'est leur
assemblage qui ne tient pas. Ce test porte donc sur la TOPOLOGIE déclarée, pas sur le code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

#: Rôles qui font tourner la boucle décisionnelle, et rôles qui alimentent l'état de marché.
#: Repris de `runtime.composition` plutôt que recopiés : deux listes finiraient par diverger.
from okxq.runtime.composition import DECIDING_ROLES  # noqa: E402

ROLES_COLLECTEURS = frozenset({"collector", "all"})


def role_du_service(service: dict) -> str | None:
    commande = service.get("command")
    if not isinstance(commande, list):
        return None
    trouve = re.search(r"--role\s+(\S+)", " ".join(str(part) for part in commande))
    return trouve.group(1).strip("\"'") if trouve else None


def services_du_profil(profil: str) -> dict[str, dict]:
    """Services démarrés par `docker compose --profile <profil>`.

    Un service sans `profiles` démarre TOUJOURS ; un service qui en déclare ne démarre que si l'un
    d'eux est demandé. C'est la règle de compose, et c'est elle qui décide de la topologie réelle.
    """
    out = {}
    for nom, service in COMPOSE["services"].items():
        profils = service.get("profiles")
        if not profils or profil in profils:
            out[nom] = service
    return out


def test_the_compose_file_declares_roles_at_all() -> None:
    """Garde-fou du garde-fou : sans service portant un rôle, les tests suivants ne vérifient rien."""
    roles = {role_du_service(s) for s in COMPOSE["services"].values()}
    assert roles - {None}, "aucun service ne déclare de rôle"


#: DEMO et LIVE portent le MÊME défaut, et il n'est pas corrigé : leur découpage fait décider un
#: processus qui ne collecte pas. Il n'est pas corrigeable en déplaçant des conteneurs, parce que le
#: gateway y détient des clés et doit rester seul — il faut un vrai transport de données de marché
#: (message, base, mémoire partagée), qui reste à construire.
#:
#: `strict=True` est délibéré : le jour où le transport existera, ce test PASSERA, et son xfail
#: strict fera échouer la suite. C'est exactement ce qu'on veut — être forcé de rouvrir ce fichier
#: et d'acter la correction, au lieu de laisser une exception périmée couvrir un défaut disparu.
PROFILS_SANS_TRANSPORT = ("demo", "live")


@pytest.mark.parametrize(
    "profil",
    [
        "paper",
        *[
            pytest.param(
                p,
                marks=pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"défaut connu : le profil {p} fait décider un processus qui ne collecte pas. "
                        "Il manque un transport de l'état de marché entre rôles ; le gateway ne peut "
                        "pas être réuni aux autres puisqu'il détient les clés (§60)."
                    ),
                ),
            )
            for p in PROFILS_SANS_TRANSPORT
        ],
    ],
)
def test_a_profile_that_decides_also_collects_in_the_same_process(profil: str) -> None:
    """L'invariant : tout processus qui décide doit collecter lui-même.

    Sinon il décide sur un état de marché vide, pour toujours. Le jour où un transport existera
    (message, base, mémoire partagée), ce test devra être rouvert en connaissance de cause — pas
    contourné en silence.
    """
    services = services_du_profil(profil)
    decideurs = {n: role_du_service(s) for n, s in services.items()}
    decideurs = {n: r for n, r in decideurs.items() if r in DECIDING_ROLES}
    if not decideurs:
        pytest.skip(f"le profil {profil} ne fait décider personne")
    for nom, role in decideurs.items():
        assert role in ROLES_COLLECTEURS, (
            f"profil {profil} : le service « {nom} » décide (rôle {role}) mais ne collecte pas. "
            "Son état de marché restera vide et toutes ses décisions seront NO_TRADE/DATA_STALE."
        )


def test_paper_runs_a_single_engine_because_there_is_no_transport_yet() -> None:
    """PAPER réunit les rôles dans un processus, et c'est sûr : il n'y a aucun secret à séparer.

    En PAPER la plateforme n'ouvre aucune connexion privée — `build_exchange_adapter` rend un
    `VirtualExchange` — donc aucun identifiant d'échange n'existe. Réunir les rôles ne réunit rien
    de sensible. En DEMO et en LIVE, le gateway détient des clés et doit rester seul (§60).
    """
    paper = services_du_profil("paper")
    roles = {role_du_service(s) for s in paper.values()} - {None}
    assert roles == {"all"}, f"PAPER devrait ne faire tourner qu'un moteur complet, vu : {roles}"


def test_the_split_roles_are_kept_for_the_modes_that_need_them() -> None:
    """Le découpage n'est pas supprimé : il reste pour DEMO et LIVE, où il est nécessaire.

    Contre-épreuve du test précédent : supprimer purement et simplement les rôles séparés aurait
    aussi fait passer « PAPER ne fait tourner qu'un moteur », en détruisant la séparation des
    secrets exigée dès que de vraies clés existent.
    """
    demo = services_du_profil("demo")
    roles = {role_du_service(s) for s in demo.values()} - {None}
    assert "gateway" in roles, "le gateway doit rester un processus distinct en DEMO"
    assert "all" not in roles, "DEMO ne doit pas réunir les rôles : le gateway y détient des clés"


def test_only_the_gateway_service_can_ever_receive_exchange_keys() -> None:
    """Quel que soit le profil, aucun service autre que `gateway` ne lit `env/gateway.env`."""
    for nom, service in COMPOSE["services"].items():
        fichiers = service.get("env_file") or []
        chemins = [f["path"] if isinstance(f, dict) else str(f) for f in fichiers]
        porteurs = [c for c in chemins if "gateway.env" in c]
        assert not porteurs or nom == "gateway", f"{nom} lit {porteurs} : séparation des secrets rompue"
