"""Le rapport d'état a affirmé trois fois de suite le contraire de la réalité.

`docker compose logs`, sans liste de services, n'inclut que les services du profil ACTIF — et aucun
profil n'est actif quand on lance la commande sans `--profile`. Le conteneur `moteur`, déclaré sous
`profiles: ["paper"]`, en était donc absent. Le diagnostic lisait les journaux de tout sauf du seul
processus qui décide, et concluait « aucune décision » pendant qu'une décision par minute était
journalisée juste à côté.

Un diagnostic faux coûte plus cher qu'une absence de diagnostic : on le croit, et on va chercher la
panne ailleurs. Ces tests font tourner `deploy/etat.sh` contre un faux `docker` qui reproduit
exactement cette règle de compose — les journaux d'un service sous profil ne sortent QUE s'il est
nommé — et vérifient que le rapport voit ce qui se passe.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "etat.sh"

#: Services toujours actifs (aucun `profiles:`), puis celui que le profil `paper` active seul.
SANS_PROFIL = ("postgres", "migrate", "api")
SOUS_PROFIL = "moteur"

#: Une vraie ligne de décision, telle que `runtime.composition` l'écrit : `role` vient du contexte
#: lié au démarrage, `reason_codes` de l'enregistrement de décision.
DECISION = (
    '{"decision_id": "dec_01m2verrfgbd68w73afhstznza", "outcome": "NO_TRADE", '
    '"reason_codes": "SOFT_HALT", "intents": 0, "event": "decision", "mode": "PAPER", '
    '"role": "all", "account_scope": "paper-local", "level": "info"}'
)
PREDICTEUR = (
    '{"raison": "[PROTOCOL_VIOLATION] aucun modèle désigné", "effet": "NO_TRADE", '
    '"event": "predictor_indisponible", "mode": "PAPER", "role": "all", "level": "warning"}'
)


def terrain(tmp_path: Path) -> Path:
    """Un /opt/okxq de substitution : les fichiers d'environnement que le rapport lit."""
    env = tmp_path / "env"
    env.mkdir(parents=True, exist_ok=True)
    (env / "api.env").write_text(
        "OPERATOR_AUTH_SECRET=secret-de-test\nDATABASE_URL=x\nOKXQ_MODE=PAPER\n", encoding="utf-8"
    )
    return tmp_path


def faux_docker(tmp_path: Path) -> Path:
    """Un `docker` qui applique la règle de compose qui nous a piégés.

    `logs` sans nom de service ne rend QUE les services sans profil. Nommer `moteur` le fait
    apparaître. C'est le comportement réel, et c'est lui qu'on veut voir respecté par le script.
    """
    binaire = tmp_path / "bin"
    binaire.mkdir(exist_ok=True)
    (binaire / "docker").write_text(
        f"""#!/usr/bin/env bash
SANS_PROFIL="{" ".join(SANS_PROFIL)}"
[ "$1" = "compose" ] || exit 0
shift
[ "${{1:-}}" = "--profile" ] && shift 2
case "${{1:-}}" in
  config)
    for s in $SANS_PROFIL {SOUS_PROFIL}; do echo "$s"; done
    ;;
  ps)
    shift
    if [ "${{1:-}}" = "-a" ]; then shift; fi
    if [ "${{1:-}}" = "--format" ]; then
      case "$2" in
        *Name*) for s in $SANS_PROFIL {SOUS_PROFIL}; do echo "$s okxq-$s-1"; done ;;
        *)      for s in $SANS_PROFIL {SOUS_PROFIL}; do echo "$s"; done ;;
      esac
    else
      echo "NAME IMAGE SERVICE STATUS"
    fi
    ;;
  logs)
    shift
    nommes=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --tail=*|--no-color|--since) [ "$1" = "--since" ] && shift ;;
        -*) ;;
        *) nommes="$nommes $1" ;;
      esac
      shift
    done
    if [ -z "$nommes" ]; then nommes="$SANS_PROFIL"; fi
    for s in $nommes; do
      if [ "$s" = "{SOUS_PROFIL}" ]; then
        echo '{SOUS_PROFIL}-1  | {DECISION}'
        echo '{SOUS_PROFIL}-1  | {PREDICTEUR}'
      else
        echo "$s-1  | {{\\"event\\": \\"bruit\\", \\"level\\": \\"info\\"}}"
      fi
    done
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    (binaire / "docker").chmod(0o755)
    return binaire


def lancer(tmp_path: Path) -> str:
    binaire = faux_docker(tmp_path)
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            PATH=f"{binaire}:{os.environ['PATH']}",
            OKXQ_DIR=str(terrain(tmp_path)),
            # Un port que rien n'écoute : les appels HTTP échouent vite et le rapport doit continuer.
            OKXQ_PORT="9",
        ),
    )
    return res.stdout


def test_le_diagnostic_voit_les_decisions_dun_service_sous_profil(tmp_path: Path) -> None:
    """Le défaut exact : `moteur` est sous `profiles: ["paper"]`, donc invisible sans être nommé."""
    sortie = lancer(tmp_path)
    assert "décisions (10 min) :" in sortie, "la ligne de comptage des décisions a disparu"
    ligne = next(li for li in sortie.splitlines() if "décisions (10 min)" in li)
    assert ligne.split()[-1] == "1", f"le rapport ne voit pas la décision du moteur : {ligne!r}"


def test_le_diagnostic_rapporte_le_motif_de_la_derniere_decision(tmp_path: Path) -> None:
    """« NO_TRADE » sans motif n'apprend rien ; le motif dit s'il faut chercher une panne."""
    sortie = lancer(tmp_path)
    ligne = next(li for li in sortie.splitlines() if "motifs de la dernière décision" in li)
    assert "SOFT_HALT" in ligne, f"motif absent : {ligne!r}"


def test_le_diagnostic_nomme_le_role_qui_decide(tmp_path: Path) -> None:
    sortie = lancer(tmp_path)
    ligne = next(li for li in sortie.splitlines() if "rôles qui décident" in li)
    assert "all" in ligne, f"le rôle décideur n'est pas rapporté : {ligne!r}"


def test_le_diagnostic_distingue_labsence_de_modele_dune_panne(tmp_path: Path) -> None:
    """Sans cette ligne, on lit « NO_TRADE » et on cherche un défaut là où il n'y en a pas.

    La plateforme refuse d'agir faute de modèle validé : c'est le comportement voulu (§11), pas une
    panne. Le rapport doit le dire, sinon chaque lecteur refait l'enquête.
    """
    sortie = lancer(tmp_path)
    ligne = next(li for li in sortie.splitlines() if "prédicteur" in li)
    assert "INDISPONIBLE" in ligne, f"l'absence de modèle n'est pas signalée : {ligne!r}"


def test_les_journaux_applicatifs_contiennent_le_moteur(tmp_path: Path) -> None:
    """Contre-épreuve : la section des journaux souffrait du même aveuglement."""
    sortie = lancer(tmp_path)
    debut = sortie.index("journaux applicatifs")
    fin = sortie.index("disque et mémoire")
    assert f"{SOUS_PROFIL}-1" in sortie[debut:fin], "les journaux applicatifs omettent le moteur"


def test_postgres_est_exclu_des_journaux_applicatifs(tmp_path: Path) -> None:
    """Ses points de contrôle, une ligne toutes les cinq minutes, noyaient tout le reste."""
    sortie = lancer(tmp_path)
    debut = sortie.index("journaux applicatifs")
    fin = sortie.index("disque et mémoire")
    assert "postgres-1" not in sortie[debut:fin], "PostgreSQL pollue encore les journaux applicatifs"


def test_le_diagnostic_est_la_derniere_section(tmp_path: Path) -> None:
    """Il était au milieu : sur la page d'un run, personne ne le voyait jamais."""
    sortie = lancer(tmp_path)
    sections = [li for li in sortie.splitlines() if li.startswith("=====")]
    assert sections, "le rapport n'a plus de sections"
    assert "diagnostic" in sections[-1], f"le diagnostic n'est pas en dernier : {sections[-1]!r}"
