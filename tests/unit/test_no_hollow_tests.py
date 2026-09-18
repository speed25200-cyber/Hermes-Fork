"""Garde-fou sur les tests eux-mêmes : aucun test ne doit passer sans rien affirmer.

Un test creux est pire qu'un test absent. Absent, il laisse un trou visible dans la matrice de
couverture ; creux, il compte pour vert et fait croire que le comportement est vérifié. Deux tests
creux ont été écrits puis rattrapés à la lecture pendant la construction de ce dépôt — l'un comptait
les lignes d'une base vide sans rien y insérer, l'autre n'assertait que des valeurs d'énumération
sans exercer la file qu'il prétendait tester. Ce contrôle les aurait attrapés tout seul.

Il est volontairement syntaxique : il ne juge pas la PERTINENCE d'une affirmation, seulement sa
présence. Un test qui affirme une banalité passera ici — mais un test qui n'affirme rien du tout,
non.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]

#: Noms d'attributs qui portent une affirmation sans passer par `assert`.
CONTEXTES_AFFIRMANTS = frozenset({"raises", "warns"})


def _affirme_quelque_chose(fonction: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for noeud in ast.walk(fonction):
        if isinstance(noeud, ast.Assert):
            return True
        # `pytest.raises(...)` / `pytest.warns(...)` : l'affirmation est le contexte lui-même.
        if isinstance(noeud, ast.Attribute) and noeud.attr in CONTEXTES_AFFIRMANTS:
            return True
        # Une aide dont le nom commence par `assert` ou `_assert` porte l'affirmation.
        if isinstance(noeud, ast.Call):
            cible = noeud.func
            nom = getattr(cible, "id", None) or getattr(cible, "attr", None) or ""
            if nom.startswith(("assert", "_assert")):
                return True
    return False


def test_no_test_function_is_hollow() -> None:
    """Parcourt tous les fichiers de test et exige au moins une affirmation par test."""
    creux: list[str] = []
    fichiers = sorted(TESTS.rglob("test_*.py"))
    assert fichiers, "aucun fichier de test trouvé : ce contrôle ne vérifierait rien"
    for chemin in fichiers:
        arbre = ast.parse(chemin.read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            if not isinstance(noeud, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not noeud.name.startswith("test_"):
                continue
            if not _affirme_quelque_chose(noeud):
                creux.append(f"{chemin.relative_to(TESTS.parent)}::{noeud.name}")
    assert not creux, "des tests ne vérifient rien et comptent pourtant pour verts : " + ", ".join(creux)


def test_the_detector_actually_detects_a_hollow_test() -> None:
    """Contrôle du contrôle. Sans lui, une erreur dans le détecteur le rendrait muet, et un
    garde-fou muet est indistinguable d'un dépôt sain."""
    creux = ast.parse("def test_rien():\n    x = 1 + 1\n").body[0]
    assert isinstance(creux, ast.FunctionDef)
    assert _affirme_quelque_chose(creux) is False

    for source in (
        "def test_avec_assert():\n    assert 1 == 1\n",
        "def test_avec_raises():\n    with pytest.raises(ValueError):\n        f()\n",
        "def test_avec_aide():\n    assert_refuse(quelque_chose)\n",
    ):
        plein = ast.parse(source).body[0]
        assert isinstance(plein, ast.FunctionDef)
        assert _affirme_quelque_chose(plein) is True, source
