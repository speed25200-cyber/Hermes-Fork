"""Changer de profil compose n'arrête pas ce que l'ancien profil faisait tourner.

`docker compose up` ne touche pas aux services que le profil actif désactive, et `--remove-orphans`
ne les considère pas comme orphelins puisqu'ils figurent toujours dans le fichier. Au passage de
PAPER des cinq rôles séparés au moteur unique, les cinq anciens conteneurs ont donc survécu au
déploiement — sur l'image précédente — à côté du nouveau. Deux processus écrivaient le même
coupe-circuit, et celui qui ne collectait rien réimposait `data_stale` à chaque frontière : sept
conteneurs « healthy », aucune erreur dans les journaux, et une plateforme incapable de rien.

Ces tests font tourner `deploy/nettoyer_hors_profil.sh` avec un faux `docker` : on vérifie ce qu'il
SUPPRIME et, tout aussi important, ce qu'il ne supprime pas.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "nettoyer_hors_profil.sh"

#: Topologie réelle du dépôt : sans profil (toujours actifs), puis les deux découpages.
SANS_PROFIL = ["postgres", "migrate", "api"]
PAPER = [*SANS_PROFIL, "moteur"]
DEMO = [*SANS_PROFIL, "collector", "strategy", "risk", "gateway", "jev-worker"]


def faux_docker(tmp_path: Path, *, services: dict[str, list[str]], presents: list[str]) -> Path:
    """Un `docker` qui répond aux trois seules commandes que le script utilise.

    `rm -f` n'efface rien : il écrit le nom reçu dans un fichier, qui est la preuve du test.
    """
    binaire = tmp_path / "bin"
    binaire.mkdir(exist_ok=True)
    for profil, noms in services.items():
        (tmp_path / f"services-{profil}.txt").write_text("\n".join(noms) + "\n", encoding="utf-8")
    (tmp_path / "ps.txt").write_text("".join(f"{svc} okxq-{svc}-1\n" for svc in presents), encoding="utf-8")
    (tmp_path / "rm.log").write_text("", encoding="utf-8")
    (binaire / "docker").write_text(
        f"""#!/usr/bin/env bash
RACINE={tmp_path}
if [ "$1" = "compose" ]; then
  shift
  PROFIL=""
  if [ "${{1:-}}" = "--profile" ]; then PROFIL=$2; shift 2; fi
  case "${{1:-}}" in
    config) cat "$RACINE/services-$PROFIL.txt" 2>/dev/null; exit 0 ;;
    ps)     cat "$RACINE/ps.txt"; exit 0 ;;
  esac
  exit 0
fi
if [ "$1" = "rm" ]; then
  shift
  [ "$1" = "-f" ] && shift
  echo "$1" >> "$RACINE/rm.log"
  exit ${{FAUX_DOCKER_RM_CODE:-0}}
fi
exit 0
""",
        encoding="utf-8",
    )
    (binaire / "docker").chmod(0o755)
    return binaire


def lancer(tmp_path: Path, binaire: Path, profil: str, **env: str) -> subprocess.CompletedProcess[str]:
    environnement = dict(os.environ, PATH=f"{binaire}:{os.environ['PATH']}", **env)
    return subprocess.run(
        ["bash", str(SCRIPT), profil],
        capture_output=True,
        text=True,
        env=environnement,
        cwd=tmp_path,
    )


def supprimes(tmp_path: Path) -> set[str]:
    return {ligne for ligne in (tmp_path / "rm.log").read_text(encoding="utf-8").split() if ligne}


def test_les_roles_de_lancien_decoupage_sont_supprimes(tmp_path: Path) -> None:
    """Le cas réel : PAPER est passé au moteur unique, les cinq rôles tournent encore."""
    binaire = faux_docker(
        tmp_path,
        services={"paper": PAPER},
        presents=[*PAPER, "collector", "strategy", "risk", "gateway", "jev-worker"],
    )
    res = lancer(tmp_path, binaire, "paper")
    assert res.returncode == 0, res.stderr
    assert supprimes(tmp_path) == {
        "okxq-collector-1",
        "okxq-strategy-1",
        "okxq-risk-1",
        "okxq-gateway-1",
        "okxq-jev-worker-1",
    }


def test_la_base_et_lapi_ne_sont_jamais_supprimees(tmp_path: Path) -> None:
    """Contre-épreuve : un nettoyage trop large détruirait PostgreSQL, donc les données.

    C'est le risque réel de ce script. Les noms sont écrits en clair ici, et non repris d'une
    constante du script : une liste qui se vérifie elle-même ne vérifie rien.
    """
    binaire = faux_docker(
        tmp_path,
        services={"paper": PAPER},
        presents=[*PAPER, "collector"],
    )
    res = lancer(tmp_path, binaire, "paper")
    assert res.returncode == 0, res.stderr
    effaces = supprimes(tmp_path)
    for garde in ("okxq-postgres-1", "okxq-migrate-1", "okxq-api-1", "okxq-moteur-1"):
        assert garde not in effaces, f"{garde} supprimé : le nettoyage déborde sur un service actif"


def test_le_moteur_paper_est_supprime_quand_on_passe_en_demo(tmp_path: Path) -> None:
    """Le nettoyage marche dans les deux sens : DEMO ne doit pas garder le moteur de PAPER.

    Sinon un processus à rôle `all` — qui décide — survivrait dans un mode où la décision et les
    clés doivent être séparées (§60).
    """
    binaire = faux_docker(
        tmp_path,
        services={"demo": DEMO},
        presents=[*DEMO, "moteur"],
    )
    res = lancer(tmp_path, binaire, "demo")
    assert res.returncode == 0, res.stderr
    assert supprimes(tmp_path) == {"okxq-moteur-1"}


def test_une_liste_de_services_illisible_ne_supprime_rien(tmp_path: Path) -> None:
    """Si compose ne répond pas, on refuse au lieu de deviner : une liste vide effacerait tout."""
    binaire = faux_docker(tmp_path, services={}, presents=[*PAPER, "collector"])
    res = lancer(tmp_path, binaire, "paper")
    assert res.returncode != 0, "un profil illisible doit faire échouer le nettoyage"
    assert supprimes(tmp_path) == set(), "rien ne doit être supprimé sans liste de référence"


def test_une_suppression_qui_echoue_nest_pas_un_succes(tmp_path: Path) -> None:
    """Un conteneur de trop qu'on n'arrive pas à supprimer, c'est la panne que ce script évite.

    Le signaler dans le texte tout en rendant 0 laisserait le déploiement se déclarer réussi avec
    deux écrivains du même état — exactement ce qu'on cherche à empêcher.
    """
    binaire = faux_docker(tmp_path, services={"paper": PAPER}, presents=[*PAPER, "collector"])
    res = lancer(tmp_path, binaire, "paper", FAUX_DOCKER_RM_CODE="1")
    assert res.returncode != 0, "une suppression impossible doit faire échouer le nettoyage"


def test_rien_a_faire_quand_la_topologie_est_deja_bonne(tmp_path: Path) -> None:
    """Idempotence : relancer un déploiement sain ne doit toucher à aucun conteneur."""
    binaire = faux_docker(tmp_path, services={"paper": PAPER}, presents=PAPER)
    res = lancer(tmp_path, binaire, "paper")
    assert res.returncode == 0, res.stderr
    assert supprimes(tmp_path) == set()


def test_linstallateur_appelle_bien_ce_nettoyage() -> None:
    """Un script correct que personne n'appelle ne corrige rien.

    C'est le mode d'échec le plus bête et le plus fréquent de ce dépôt : la logique est juste, le
    câblage manque, et les tests de la logique passent tous.
    """
    installateur = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "nettoyer_hors_profil.sh" in installateur, (
        "deploy/install.sh n'appelle pas le nettoyage : les conteneurs hors profil survivront"
    )


@pytest.mark.parametrize("script", ["nettoyer_hors_profil.sh", "install.sh", "etat.sh"])
def test_les_scripts_de_deploiement_sont_syntaxiquement_valides(script: str) -> None:
    """`bash -n` sur chaque script : une faute de syntaxe ne doit pas se découvrir sur le serveur."""
    res = subprocess.run(["bash", "-n", str(ROOT / "deploy" / script)], capture_output=True, text=True)
    assert res.returncode == 0, f"{script} : {res.stderr}"
