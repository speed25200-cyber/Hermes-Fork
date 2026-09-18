"""Tests de ``scripts/security_check.py`` (§64, T66).

Un contrôle de sécurité qui ne détecte rien n'est pas un contrôle : chaque règle est donc testée dans
les DEUX sens — une violation doit être refusée, et la configuration conforme ne doit produire aucun
bruit. Un contrôle bruyant se fait désactiver, un contrôle aveugle se fait oublier ; les deux échecs
sont ici des tests rouges.

Les valeurs « en forme de secret » utilisées ci-dessous sont inventées (hexadécimal et UUID tirés au
hasard). Elles n'ont jamais été des identifiants valides chez aucun fournisseur.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "security_check.py"


def _load() -> ModuleType:
    """Charge le script par chemin : ``scripts/`` n'est pas un paquet importable."""
    spec = importlib.util.spec_from_file_location("okxq_security_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sc = _load()

# Formes authentiques d'identifiants OKX, mais valeurs inventées : c'est précisément ce que le contrôle
# doit reconnaître quand elles sont affectées à un nom de variable de déploiement.
FAUSSE_CLE_UUID = "7d41a0c8-52bf-4e19-9a3b-16c8d0e4f215"
FAUX_SECRET_HEX = "9B4E07C1D2A6F38504EB1C7A9D630F52"
FAUX_MOT_DE_PASSE = "e7c1b9a4f2d8"

# Ces trois chaînes sont ASSEMBLÉES morceau par morceau, et les autres passent par une interpolation.
# Écrites en clair sur une ligne, elles feraient échouer `security_check.py` sur CE fichier : un test
# qui viole la règle qu'il vérifie n'est pas un test, c'est une exception déguisée. Assembler la valeur
# garde la règle sans liste blanche de chemins.
ENTETE_CLE_PRIVEE = "-----BEGIN OPENSSH PRIVATE" + " KEY-----"
VRAI = "true"
UN = "1"

COMPOSE_CONFORME = """
name: okxq
services:
  postgres:
    image: postgres:16-alpine
    env_file:
      - path: env/postgres.env
        required: false
  collector:
    env_file: [env/collector.env]
  gateway:
    env_file: [env/gateway.env]
  jev-worker:
    env_file: [env/jev-worker.env]
  api:
    env_file: [env/api.env]
"""

EXEMPLES = {
    "collector.env.example": "DATABASE_URL=postgresql+psycopg://okxq:remplacer-hors-git@postgres:5432/x\n",
    "gateway.env.example": (
        "OKX_API_KEY=remplacer-par-la-cle-reelle-hors-git\n"
        "OKX_API_SECRET=remplacer-par-le-secret-reel-hors-git\n"
        "OKX_API_PASSPHRASE=remplacer-par-la-passphrase-hors-git\n"
    ),
    "jev-worker.env.example": "TYPESAFE_API_KEY=remplacer-par-la-cle-typesafe-hors-git\n",
    "api.env.example": "OPERATOR_AUTH_SECRET=remplacer-par-le-secret-operateur-hors-git\n",
    "db.env.example": "POSTGRES_USER=okxq\nPOSTGRES_PASSWORD=remplacer-hors-git\n",
}


@pytest.fixture
def depot(tmp_path: Path) -> Path:
    """Dépôt minimal CONFORME : c'est la référence négative de tous les tests qui suivent."""
    (tmp_path / "infra" / "env").mkdir(parents=True)
    (tmp_path / "configs").mkdir()
    (tmp_path / "compose.yaml").write_text(COMPOSE_CONFORME, encoding="utf-8")
    for nom, contenu in EXEMPLES.items():
        (tmp_path / "infra" / "env" / nom).write_text(contenu, encoding="utf-8")
    (tmp_path / "configs" / "base.yaml").write_text(
        "project:\n  mode: PAPER\n  live_enabled: false\n", encoding="utf-8"
    )
    (tmp_path / "configs" / "live.disabled.yaml").write_text(
        "# LIVE documenté mais DÉSACTIVÉ\nproject:\n  mode: LIVE\n  live_enabled: false\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text("OKX_API_KEY=\nTYPESAFE_API_KEY=\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# faux depot\n", encoding="utf-8")
    return tmp_path


def _checks(findings: list[object]) -> set[str]:
    return {f.check for f in findings}  # type: ignore[attr-defined]


# ══ Référence négative ════════════════════════════════════════════════════════════════════════════


def test_depot_conforme_ne_declenche_aucune_anomalie(depot: Path) -> None:
    findings, inventaire = sc.run_all_checks(depot)
    assert findings == [], [f.render() for f in findings]
    assert inventaire.files, "l'inventaire ne doit pas être vide, sinon le vert ne prouve rien"


def test_le_depot_reel_passe_les_controles() -> None:
    """Le contrat du script : code 0 sur ce dépôt. Ce test échoue si quelqu'un committe un secret."""
    findings, inventaire = sc.run_all_checks(ROOT)
    assert findings == [], [f.render() for f in findings]
    assert len(inventaire.files) > 50


# ══ 1. Secrets committés ══════════════════════════════════════════════════════════════════════════


def test_secret_okx_committe_est_refuse(depot: Path) -> None:
    (depot / "deploy.sh").write_text(
        f"#!/bin/sh\nexport OKX_API_SECRET={FAUX_SECRET_HEX}\n", encoding="utf-8"
    )
    findings, _ = sc.run_all_checks(depot)
    assert "secret_committe" in _checks(findings)
    assert any("OKX_API_SECRET" in f.detail and f.path == "deploy.sh" for f in findings)


def test_cle_okx_en_forme_uuid_est_refusee(depot: Path) -> None:
    (depot / "note.txt").write_text(f"OKX_API_KEY={FAUSSE_CLE_UUID}\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert any(f.check == "secret_committe" and f.path == "note.txt" for f in findings)


def test_cle_typesafe_committee_est_refusee(depot: Path) -> None:
    (depot / "notes.md").write_text(f"TYPESAFE_API_KEY: {FAUX_SECRET_HEX}\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert any(f.check == "secret_committe" for f in findings)


def test_mot_de_passe_dans_une_url_est_refuse(depot: Path) -> None:
    (depot / "notes.txt").write_text(
        f"postgresql+psycopg://okxq:{FAUX_MOT_DE_PASSE}@postgres:5432/okxq\n", encoding="utf-8"
    )
    findings, _ = sc.run_all_checks(depot)
    assert any(f.check == "secret_committe" and "URL" in f.detail for f in findings)


def test_bloc_de_cle_privee_est_refuse(depot: Path) -> None:
    (depot / "id_rsa").write_text(ENTETE_CLE_PRIVEE + "\nb3BlbnNzaC1rZXktdjEAAAAA\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert any("clé privée" in f.detail for f in findings)


def test_jeton_de_fournisseur_est_refuse(depot: Path) -> None:
    (depot / "client.py").write_text('JETON = "sk-' + "a1b2c3d4e5f6g7h8i9j0k1" + '"\n', encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert any(f.check == "secret_committe" for f in findings)


@pytest.mark.parametrize(
    "ligne",
    [
        "OKX_API_SECRET=",
        "OKX_API_SECRET=remplacer-par-le-secret-reel-hors-git",
        'OKX_API_KEY="${OKX_API_KEY_POSE:-}"',
        "OKX_API_SECRET=$S",
        "OKX_API_KEY=...",
        "OKX_API_SECRET=SHOULDNOTLEAK123456",
        "OKX_API_PASSPHRASE=changeme",
        "OKX_API_SECRET=votre-secret-ici",
        'grep -q "^OKX_API_KEY=.\\+" /root/ancien/.env',
    ],
)
def test_valeurs_de_remplacement_ne_sont_pas_signalees(depot: Path, ligne: str) -> None:
    """Modèles, fixtures de rédaction et substitutions de shell ne sont pas des secrets."""
    (depot / "modele.txt").write_text(ligne + "\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert [f.render() for f in findings if f.check == "secret_committe"] == []


def test_empreintes_et_identifiants_legitimes_ne_sont_pas_signales(depot: Path) -> None:
    """Un dépôt quantitatif est plein de chaînes à forte entropie parfaitement licites."""
    (depot / "uv.lock").write_text(
        'sdist = { url = "https://x/y.tar.gz", hash = "sha256:'
        'f8e20f682c9aabd000bcf4a7ed8aa6f473c1adfecccae34ec24e823d156f4af0" }\n'
        f'config_hash = "{FAUX_SECRET_HEX}"\n'
        f'execution_key = "{FAUSSE_CLE_UUID}"\n',
        encoding="utf-8",
    )
    findings, _ = sc.run_all_checks(depot)
    assert [f.render() for f in findings] == []


def test_is_placeholder_distingue_le_faux_du_vrai() -> None:
    assert sc.is_placeholder("")
    assert sc.is_placeholder("remplacer-par-le-secret-reel")
    assert sc.is_placeholder("${VARIABLE}")
    assert sc.is_placeholder("aaaaaaaaaaaaaaaa")
    assert not sc.is_placeholder(FAUX_SECRET_HEX)
    assert not sc.is_placeholder(FAUSSE_CLE_UUID)


# ══ 2. Fichiers d'environnement versionnés ════════════════════════════════════════════════════════


@pytest.mark.parametrize("nom", [".env", ".env.production", "env/gateway.env", "configs/local.env"])
def test_fichier_env_non_exemple_est_refuse(depot: Path, nom: str) -> None:
    chemin = depot / nom
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text("OKXQ_MODE=PAPER\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert any(f.check == "env_suivi_par_git" and f.path == nom for f in findings)


@pytest.mark.parametrize("nom", [".env.example", "infra/env/risk.env.example", "env/api.env.sample"])
def test_modeles_env_sont_acceptes(depot: Path, nom: str) -> None:
    chemin = depot / nom
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text("OKXQ_MODE=PAPER\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert [f.render() for f in findings if f.check == "env_suivi_par_git"] == []


# ══ 2 bis. Sauvegardes de base ════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "nom",
    [
        "backups/okxq-20260918T120000Z.tar.enc",
        "backups/okxq-20260918T120000Z.tar",
        "backups/notes.txt",
        "okxq-20260918T120000Z.tar.enc",
        "base.dump",
        "vidage.sql.gz",
    ],
)
def test_sauvegarde_publiable_est_refusee(depot: Path, nom: str) -> None:
    """Un vidage contient ordres, fills, comptabilité et équité : sa publication est irréversible."""
    chemin = depot / nom
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(b"PGDMP-faux\n")
    findings, _ = sc.run_all_checks(depot)
    assert any(f.check == "sauvegarde_versionnee" and f.path == nom for f in findings)


@pytest.mark.parametrize("nom", ["configs/base.yaml", "docs/reprise.md", "infra/backup.sh"])
def test_fichiers_ordinaires_ne_sont_pas_pris_pour_des_sauvegardes(depot: Path, nom: str) -> None:
    chemin = depot / nom
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text("# rien de sensible\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert [f.render() for f in findings if f.check == "sauvegarde_versionnee"] == []


# ══ 3. Activation de LIVE ═════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "contenu",
    [
        "project:\n  live_enabled: true\n",
        "project:\n  live_enabled: 'yes'\n",
        "project:\n  okxq_live_enabled: on\n",
        "risk:\n  allow_live: true\n",
        f"services:\n  gateway:\n    environment:\n      LIVE_ENABLED: '{VRAI}'\n",
    ],
)
def test_configuration_activant_live_est_refusee(depot: Path, contenu: str) -> None:
    (depot / "configs" / "essai.yaml").write_text(contenu, encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert "live_active" in _checks(findings), contenu


def test_variable_denvironnement_activant_live_est_refusee(depot: Path) -> None:
    (depot / "lancer.sh").write_text(f"#!/bin/sh\nOKXQ_ALLOW_LIVE={UN} okxq live run\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert any(f.check == "live_active" and f.path == "lancer.sh" for f in findings)


@pytest.mark.parametrize(
    "contenu",
    [
        "project:\n  mode: LIVE\n  live_enabled: false\n",
        "project:\n  live_enabled: false\n",
        "# LIVE est désactivé par défaut et exige un manifeste signé\nroutes:\n  - /health/live\n",
        "api:\n  liveness_path: /health/live\n",
        "env:\n  OKXQ_LIVE_ENABLED: 'false'\n",
    ],
)
def test_live_desactive_ou_mentionne_nest_pas_signale(depot: Path, contenu: str) -> None:
    """Le contrôle est structurel : le mot « live » dans un texte ou une route ne l'active pas."""
    (depot / "configs" / "essai.yaml").write_text(contenu, encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert [f.render() for f in findings if f.check == "live_active"] == []


def test_yaml_illisible_est_signale_plutot_quignore(depot: Path) -> None:
    """Un YAML qu'on ne peut pas analyser n'est pas un YAML conforme : ne pas le passer en silence."""
    (depot / "configs" / "casse.yaml").write_text("project:\n  - a\n b: c\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert any("illisible" in f.detail for f in findings)


# ══ 4. Séparation des secrets par service ═════════════════════════════════════════════════════════


def test_cles_okx_hors_du_gateway_sont_refusees(depot: Path) -> None:
    compose = COMPOSE_CONFORME.replace(
        "  collector:\n    env_file: [env/collector.env]",
        "  collector:\n    environment:\n      OKX_API_KEY: x\n      OKX_API_SECRET: y",
    )
    (depot / "compose.yaml").write_text(compose, encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    fautes = [f for f in findings if f.check == "separation_secrets"]
    assert fautes, "le collecteur ne doit jamais recevoir les clés OKX (§60)"
    assert all("collector" in f.path for f in fautes)


def test_bloc_denvironnement_partage_est_refuse(depot: Path) -> None:
    """Le cas exact que §60 qualifie de violation : un ancrage YAML qui injecte le secret partout.

    Le parseur développe l'ancrage dans chaque service, donc l'analyse structurelle le voit — ce qu'une
    simple recherche textuelle de « OKX_API_KEY » par service manquerait.
    """
    (depot / "compose.yaml").write_text(
        "x-commun: &commun\n"
        "  environment:\n"
        "    OKX_API_KEY: ${OKX_API_KEY}\n"
        "services:\n"
        "  gateway:\n"
        "    <<: *commun\n"
        "  collector:\n"
        "    <<: *commun\n"
        "  api:\n"
        "    <<: *commun\n",
        encoding="utf-8",
    )
    findings, _ = sc.run_all_checks(depot)
    coupables = {f.path for f in findings if f.check == "separation_secrets"}
    assert "compose.yaml#services.collector" in coupables
    assert "compose.yaml#services.api" in coupables
    assert "compose.yaml#services.gateway" not in coupables  # le gateway est le propriétaire légitime


def test_env_file_global_est_refuse(depot: Path) -> None:
    """Un ``.env`` unique déclare TOUS les secrets : le référencer les injecte tous, partout."""
    (depot / "compose.yaml").write_text(
        "services:\n  collector:\n    env_file: [.env]\n  gateway:\n    env_file: [env/gateway.env]\n",
        encoding="utf-8",
    )
    findings, _ = sc.run_all_checks(depot)
    assert any(
        f.check == "separation_secrets" and "collector" in f.path and "OKX_API_KEY" in f.detail
        for f in findings
    )


def test_cle_typesafe_hors_du_worker_jev_est_refusee(depot: Path) -> None:
    compose = COMPOSE_CONFORME.replace(
        "  gateway:\n    env_file: [env/gateway.env]",
        "  gateway:\n    env_file: [env/gateway.env, env/jev-worker.env]",
    )
    (depot / "compose.yaml").write_text(compose, encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert any("TYPESAFE_API_KEY" in f.detail and "gateway" in f.path for f in findings)


def test_cle_operateur_hors_de_lapi_est_refusee(depot: Path) -> None:
    compose = COMPOSE_CONFORME.replace(
        "  jev-worker:\n    env_file: [env/jev-worker.env]",
        "  jev-worker:\n    env_file: [env/jev-worker.env, env/api.env]",
    )
    (depot / "compose.yaml").write_text(compose, encoding="utf-8")
    findings, _ = sc.run_all_checks(depot)
    assert any("OPERATOR_AUTH_SECRET" in f.detail and "jev-worker" in f.path for f in findings)


def test_separation_du_depot_reel_est_conforme() -> None:
    """Le vrai compose.yaml : gateway seul avec OKX, jev-worker seul avec TypeSafe, api seule avec la clé."""
    assert sc.check_compose_separation(ROOT) == []


def test_compose_absent_est_une_anomalie_et_non_un_succes(tmp_path: Path) -> None:
    """Un contrôle qui ne trouve rien à contrôler doit échouer, pas rendre « tout va bien »."""
    findings = sc.check_compose_separation(tmp_path)
    assert findings and findings[0].check == "separation_secrets"
    assert "absent" in findings[0].detail


# ══ 5. Couverture et interface en ligne de commande ═══════════════════════════════════════════════


def test_depot_vide_est_refuse_par_le_controle_de_couverture(tmp_path: Path) -> None:
    findings, _ = sc.run_all_checks(tmp_path)
    assert "couverture" in _checks(findings)


def test_modeles_env_manquants_sont_signales(tmp_path: Path) -> None:
    for index in range(6):
        (tmp_path / f"fichier{index}.txt").write_text("x\n", encoding="utf-8")
    findings, _ = sc.run_all_checks(tmp_path)
    assert any(f.check == "couverture" and "env.example" in f.detail for f in findings)


def test_main_rend_zero_sur_le_depot_reel(capsys: pytest.CaptureFixture[str]) -> None:
    assert sc.main(["--root", str(ROOT)]) == 0
    assert "AUCUNE ANOMALIE" in capsys.readouterr().out


def test_main_rend_un_et_decrit_lanomalie(depot: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (depot / "fuite.env").write_text(f"OKX_API_SECRET={FAUX_SECRET_HEX}\n", encoding="utf-8")
    assert sc.main(["--root", str(depot)]) == 1
    sortie = capsys.readouterr().out
    assert "ANOMALIE" in sortie
    assert "fuite.env" in sortie


def test_sortie_json_est_exploitable_par_lintegration_continue(
    depot: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (depot / "fuite.env").write_text(f"OKX_API_SECRET={FAUX_SECRET_HEX}\n", encoding="utf-8")
    assert sc.main(["--root", str(depot), "--json"]) == 1
    charge = json.loads(capsys.readouterr().out)
    assert charge["ok"] is False
    assert charge["fichiers_inventories"] > 0
    assert {a["check"] for a in charge["anomalies"]} >= {"secret_committe", "env_suivi_par_git"}


def test_le_rapport_ne_recopie_jamais_la_valeur_du_secret(
    depot: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Un rapport de fuite ne doit pas devenir lui-même une fuite (journaux de CI publics)."""
    (depot / "fuite.env").write_text(f"OKX_API_SECRET={FAUX_SECRET_HEX}\n", encoding="utf-8")
    sc.main(["--root", str(depot)])
    assert FAUX_SECRET_HEX not in capsys.readouterr().out


def test_machine_access_secrets_are_detected_too() -> None:
    """Un mot de passe root committé donne la MACHINE, donc tous les secrets qu'elle porte.

    Ce nom manquait à la liste surveillée alors que c'est le secret le plus puissant du déploiement :
    les clés d'échange, la clé sémantique et la base vivent toutes sur cette machine. Un contrôle qui
    attrape la clé d'API mais laisse passer l'accès à l'hôte protège la serrure en oubliant la porte.
    """
    valeur = "K7mQx4ZrLpWvNtBdHsYc"  # forme d'un mot de passe généré, sans marqueur de gabarit
    for nom in ("VPS_PASSWORD", "ROOT_PASSWORD", "SSH_PASSWORD", "SSHPASS"):
        assert sc.scan_line("faux.env", 1, f"{nom}={valeur}"), nom

    # Contre-épreuve : une RÉFÉRENCE à un secret n'est pas un secret, et la signaler rendrait le
    # contrôle inutilisable sur les fichiers de workflow qui doivent bien nommer leurs secrets.
    for reference in (
        "VPS_PASSWORD: ${{ secrets.VPS_PASSWORD }}",
        "ROOT_PASSWORD=${ROOT_PASSWORD}",
        "SSH_PASSWORD=remplacer-hors-git",
    ):
        assert not sc.scan_line("x.yml", 1, reference), reference


def test_a_path_after_an_equals_sign_is_not_taken_for_a_secret() -> None:
    """`grep ^OPERATOR_AUTH_SECRET= /chemin/fichier` n'affecte rien : ce n'est pas un secret.

    Le motif tolérait un blanc après `=` et prenait donc le CHEMIN pour une valeur de 21 caractères.
    Or un shell comme un fichier `.env` affectent une valeur VIDE dès qu'un blanc suit le signe égal.
    Un garde-fou qui crie sur du code sain finit par être désactivé, et c'est ainsi qu'on cesse de
    voir les vrais.
    """
    from scripts.security_check import SECRET_ASSIGNMENT, is_placeholder

    ligne = "grep ^OPERATOR_AUTH_SECRET= /opt/okxq/env/api.env | cut -d= -f2-"
    trouve = SECRET_ASSIGNMENT.search(ligne)
    assert trouve is None or is_placeholder(trouve.group("value"))


def test_a_yaml_assignment_with_a_space_is_still_caught() -> None:
    """Contre-épreuve : en YAML, `cle: valeur` avec un espace est la forme NORMALE.

    Resserrer la règle sur `=` ne doit pas ouvrir un trou sur `:`.
    """
    from scripts.security_check import SECRET_ASSIGNMENT, is_placeholder

    # La valeur est ASSEMBLÉE, pas écrite : `security_check.py` signale — à juste titre — toute
    # chaîne qui ressemble à un identifiant réel, y compris dans un test. C'est la valeur qui décide,
    # jamais le chemin du fichier, et ce test ne doit pas devenir l'exception qui affaiblit la règle.
    faux = "9f2c41ab" + "7de84c0f" + "a1b3e5d7" + "c8a60f24"
    trouve = SECRET_ASSIGNMENT.search(f"  OKX_API_SECRET: {faux}")
    assert trouve is not None
    assert not is_placeholder(trouve.group("value"))


def test_a_real_secret_right_after_an_equals_sign_is_still_caught() -> None:
    """Et la forme shell sans espace — celle d'un vrai `.env` — reste attrapée."""
    from scripts.security_check import SECRET_ASSIGNMENT, is_placeholder

    faux = "9f2c41ab" + "7de84c0f" + "a1b3e5d7" + "c8a60f24"
    trouve = SECRET_ASSIGNMENT.search(f"OKX_API_SECRET={faux}")
    assert trouve is not None
    assert not is_placeholder(trouve.group("value"))
