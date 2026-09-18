"""T67 : les gardes de la restauration, éprouvées sur de vraies archives.

La restauration complète exige un serveur PostgreSQL et reste donc NOT_RUN ici. Mais tout ce qui
protège AVANT d'écrire se teste sans base, et c'est la partie qui compte : `pg_restore --clean`
supprime puis recrée les objets de la base cible. Restaurer une archive tronquée détruirait le
journal financier courant sans rien rétablir.

Les tests construisent de vraies archives au format du script, puis vérifient que :
- une archive saine est déclarée exploitable, sans rien écraser ;
- une archive altérée d'UN SEUL OCTET est refusée AVANT toute écriture ;
- une confirmation absente ou incorrecte interrompt l'opération ;
- vérifier une archive ne demande pas la pile complète — sinon on la vérifierait moins souvent, et
  une sauvegarde jamais restaurée n'est pas une sauvegarde.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RESTORE = ROOT / "infra" / "restore.sh"
HORODATAGE = "20260918T120000Z"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """Construit une archive NON chiffrée au format attendu : dossier `okxq-<horodatage>` contenant
    `base.dump`, `manifeste.json` et `SHA256SUMS`, plus une empreinte externe `.sha256`."""
    contenu = tmp_path / f"okxq-{HORODATAGE}"
    contenu.mkdir()
    (contenu / "base.dump").write_bytes(b"PGDMP-faux-vidage-de-test\n" * 64)
    (contenu / "manifeste.json").write_text(
        '{"horodatage": "' + HORODATAGE + '", "base": "okxq_test", "synthetique": true}\n',
        encoding="utf-8",
    )
    sommes = "\n".join(f"{_sha256(contenu / nom)}  {nom}" for nom in sorted(("base.dump", "manifeste.json")))
    (contenu / "SHA256SUMS").write_text(sommes + "\n", encoding="utf-8")

    dossier = tmp_path / "backups"
    dossier.mkdir()
    chemin = dossier / f"okxq-{HORODATAGE}.tar"
    with tarfile.open(chemin, "w") as tar:
        tar.add(contenu, arcname=contenu.name)
    (chemin.with_suffix(".tar.sha256")).write_text(f"{_sha256(chemin)}  {chemin.name}\n", encoding="utf-8")
    shutil.rmtree(contenu)
    return chemin


def _run(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environnement = {**os.environ, "OKXQ_BACKUP_DIR": str(RESTORE.parent)}
    if env:
        environnement.update(env)
    return subprocess.run(
        ["bash", str(RESTORE), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=environnement,
    )


def test_T67_a_sound_archive_is_declared_usable_and_writes_nothing(archive: Path) -> None:
    """Contre-épreuve de toutes les autres : sans elle, un script qui refuse TOUT passerait aussi."""
    result = _run(["--file", str(archive), "--verify-only"])
    assert result.returncode == 0, result.stdout + result.stderr
    sortie = result.stdout
    assert "empreinte du fichier : OK" in sortie
    assert "empreintes internes : OK" in sortie
    assert "RIEN n'a été écrasé" in sortie
    # Le manifeste est affiché : on doit pouvoir constater CE QU'ON restaurerait avant de le faire.
    assert HORODATAGE in sortie


def test_T67_verification_does_not_require_the_full_stack(archive: Path) -> None:
    """Vérifier une archive ne touche aucune base. L'exiger avec Docker ferait vérifier moins
    souvent, et une sauvegarde jamais restaurée n'est pas une sauvegarde."""
    # PATH réduit aux binaires de base : ni `docker`, ni `docker compose`.
    result = _run(
        ["--file", str(archive), "--verify-only"],
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docker absent" not in result.stderr


def test_T67_a_single_flipped_byte_is_refused_before_any_write(archive: Path) -> None:
    """Un octet suffit. C'est tout l'intérêt de vérifier AVANT d'écraser : une archive corrompue
    détruirait la base courante sans rien rétablir."""
    octets = bytearray(archive.read_bytes())
    milieu = len(octets) // 2
    octets[milieu] ^= 0x01
    archive.write_bytes(bytes(octets))
    result = _run(["--file", str(archive), "--verify-only"])
    assert result.returncode != 0
    assert "altérée ou incomplète" in result.stderr


def test_T67_a_missing_archive_is_refused(tmp_path: Path) -> None:
    result = _run(["--file", str(tmp_path / "inexistante.tar"), "--verify-only"])
    assert result.returncode != 0
    assert "archive introuvable" in result.stderr


def test_T67_no_archive_selector_is_refused(tmp_path: Path) -> None:
    """Ni `--file` ni `--latest` : le script ne doit pas choisir une archive à notre place."""
    result = _run(["--verify-only"], env={"OKXQ_BACKUP_DIR": str(tmp_path)})
    assert result.returncode != 0
    assert "--file ou --latest est requis" in result.stderr


def test_T67_a_restore_without_confirmation_is_refused(archive: Path) -> None:
    """Sans entrée interactive et sans confirmation explicite, la restauration s'arrête.

    Le script exige le nom EXACT de la base précédée de RESTAURER : une faute de frappe doit
    interrompre l'opération, pas la lancer.
    """
    result = _run(["--file", str(archive), "--db", "okxq_test"])
    assert result.returncode != 0
    sortie = result.stdout + result.stderr
    # Soit Docker manque (écriture impossible ici), soit la confirmation manque : les deux sont des
    # refus AVANT écriture, et c'est ce qui est vérifié.
    assert ("OKXQ_RESTORE_CONFIRM absent" in sortie) or ("docker absent" in sortie)


def test_T67_an_incorrect_confirmation_phrase_is_refused(archive: Path) -> None:
    result = _run(
        ["--file", str(archive), "--db", "okxq_test"],
        env={"OKXQ_RESTORE_CONFIRM": "RESTAURER mauvaise_base"},
    )
    assert result.returncode != 0
    sortie = result.stdout + result.stderr
    assert ("confirmation incorrecte" in sortie) or ("docker absent" in sortie)


def test_T67_the_script_never_enables_live(archive: Path) -> None:
    """Une restauration ne doit pas pouvoir réactiver le direct par effet de bord."""
    texte = RESTORE.read_text(encoding="utf-8")
    assert "live_enabled: true" not in texte
    assert "LIVE reste désactivé" in texte
    result = _run(["--file", str(archive), "--verify-only"])
    assert "live_enabled" not in result.stdout
