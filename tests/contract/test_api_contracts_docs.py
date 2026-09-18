"""Les contrats documentés doivent décrire le code RÉEL, pas celui d'il y a trois semaines.

`docs/api_contracts.md` référençait trois documents qui n'existaient pas : le fichier d'index et la
matrice des exigences (§46) pointaient dans le vide, et une docstring du code renvoyait le lecteur
vers `docs/api_contracts/typesafe_jev.md`. Un contrat manquant se remarque ; un contrat PÉRIMÉ, non —
il garde l'autorité d'un document tout en décrivant un autre système. D'où le choix de GÉNÉRER ces
documents depuis le code et le manifeste de capacités, et de vérifier ici qu'ils n'ont pas dérivé.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "docs" / "api_contracts.md"
DOCS = ("okx_public.md", "okx_private.md", "typesafe_jev.md")


@pytest.mark.parametrize("name", DOCS)
def test_every_document_referenced_by_the_index_exists(name: str) -> None:
    assert f"docs/api_contracts/{name}" in INDEX.read_text(encoding="utf-8")
    assert (ROOT / "docs" / "api_contracts" / name).is_file()


def test_the_documents_still_match_the_code_they_describe() -> None:
    """Rejoue le générateur en mode `--check` : il échoue si un document a dérivé de sa source."""
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "render_api_contracts.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert done.returncode == 0, (
        "document de contrat périmé — relancer "
        f"`uv run python scripts/render_api_contracts.py`\n{done.stdout}{done.stderr}"
    )


def test_the_private_contract_lists_every_allowlisted_endpoint() -> None:
    """Contre-épreuve du générateur : un endpoint ajouté à l'allowlist DOIT apparaître au document.

    Sans cette vérification, le test précédent se contenterait de comparer un fichier à lui-même si
    le rendu cessait un jour de lire l'allowlist.
    """
    from okxq.exchange.okx.rest_private import ALLOWLIST, FORBIDDEN_PATH_PREFIXES

    text = (ROOT / "docs" / "api_contracts" / "okx_private.md").read_text(encoding="utf-8")
    manquants = [e.path for e in ALLOWLIST.values() if f"`{e.path}`" not in text]
    assert not manquants, f"endpoints autorisés absents du contrat : {manquants}"
    non_dits = [p for p in FORBIDDEN_PATH_PREFIXES if f"`{p}`" not in text]
    assert not non_dits, f"préfixes refusés absents du contrat : {non_dits}"


def test_no_contract_claims_a_validation_that_never_happened() -> None:
    """Aucun de ces contrats n'a été confronté au vrai service : les documents doivent le DIRE.

    C'est la phrase qui empêche un lecteur pressé de prendre « vérifié sur fixtures » pour
    « vérifié en connexion ». Un contrat rejoué sur ses propres fixtures ne prouve rien du serveur.
    """
    for name in DOCS:
        text = (ROOT / "docs" / "api_contracts" / name).read_text(encoding="utf-8").lower()
        assert "statut de validation" in text
        assert "non validé en connexion" in text or "non exécuté" in text or "aucun appel réel" in text
