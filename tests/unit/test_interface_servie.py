"""L'interface doit être SERVIE, pas seulement « pas en erreur » (§59).

L'exploitant a ouvert son tableau de bord et reçu ceci :

    {"ok":true,"message":"API sans interface servie","mode":"PAPER","api":"/api/v1/docs"}

Code 200, `ok: true`, aucune erreur nulle part. Tous les contrôles passaient, et l'interface n'était
pas là. C'est la troisième fois dans ce dépôt qu'un chemin calculé pour une copie de travail se
révèle faux dans un paquet installé — après le manifeste de capacités et le module de données.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import okxq
from okxq.api.app import FRONTEND_DIR_ENV, create_app, default_frontend
from okxq.config import load_config
from okxq.domain.clocks import SimulatedClock
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import Base
from tests.unit.test_deployment_defects import T0

ROOT = Path(__file__).resolve().parents[2]
SECRET = "secret-operateur-de-test-suffisamment-long"


def client(**kwargs) -> TestClient:
    """Client authentifié. La fixture garde la porte FERMÉE (`acces_sans_cle: non`), volontairement :
    ce fichier vérifie que l'interface est SERVIE, pas qui a le droit d'y entrer. Mélanger les deux
    ferait passer un test d'interface pour un test d'authentification, et réciproquement."""
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    app = create_app(
        load_config(ROOT / "tests" / "fixtures" / "configs" / "smoke.fixture.yaml"),
        clock=SimulatedClock(T0),
        session_factory=make_session_factory(engine),
        operator_secret=SECRET,
        configure_logs=False,
        **kwargs,
    )
    from okxq.api.app import role_key_for
    from okxq.api.auth import Role

    return TestClient(app, headers={"Authorization": f"Bearer {role_key_for(SECRET, Role.ADMIN)}"})


def test_the_root_serves_html_not_json() -> None:
    """Le symptôme exact, reproduit : la racine doit rendre du HTML.

    Un test qui se contente du code 200 aurait validé la réponse JSON que l'exploitant a reçue. Ce
    qu'on vérifie, c'est ce qui est SERVI.
    """
    reponse = client().get("/")
    assert reponse.status_code == 200
    assert reponse.headers["content-type"].startswith("text/html"), (
        f"la racine rend « {reponse.headers['content-type']} » : l'interface n'est pas servie"
    )
    assert "API sans interface servie" not in reponse.text
    assert "<html" in reponse.text.lower()


def test_the_frontend_location_is_declared_not_guessed(monkeypatch) -> None:
    """L'emplacement vient de l'environnement ; deviner à partir de `__file__` a échoué deux fois.

    `parents[3]` désigne la racine du dépôt depuis `src/`, et `.../lib/python3.12` depuis
    `site-packages/`. L'image déclare donc `OKXQ_FRONTEND_DIR`, et le code le lit.
    """
    monkeypatch.setenv(FRONTEND_DIR_ENV, "/un/chemin/declare")
    assert default_frontend() == Path("/un/chemin/declare")
    monkeypatch.delenv(FRONTEND_DIR_ENV)
    # Sans déclaration, on retombe sur l'arborescence du dépôt : c'est le cas de la copie de travail.
    assert default_frontend() == ROOT / "frontend"


def test_the_guessed_path_would_escape_an_installed_package() -> None:
    """Contre-épreuve de l'ANCIENNE règle : elle sortait du paquet une fois installé.

    Ce test ne dépend d'aucune installation réelle : il montre que « trois niveaux au-dessus de
    `okxq/api/app.py` » ne peut pas désigner un répertoire de l'application quand le paquet vit sous
    `site-packages/`, puisque trois niveaux au-dessus on a déjà quitté `okxq`.
    """
    module = Path(okxq.__file__).resolve().parent / "api" / "app.py"
    devine = module.parents[3]
    paquet = Path(okxq.__file__).resolve().parent
    assert paquet not in devine.parents and devine != paquet, (
        "la règle devinée sort du paquet : elle ne peut pas désigner les fichiers livrés avec lui"
    )


def test_a_missing_interface_is_loud_not_a_polite_json(tmp_path) -> None:
    """Une interface demandée mais introuvable doit CRIER.

    Elle répondait `{"ok": true, "message": "API sans interface servie"}` : une dégradation qui se
    présente comme un succès. Personne ne cherche une panne devant un `ok: true`. Le journal porte
    désormais une erreur nommée, que le déploiement sait repérer.
    """
    from structlog.testing import capture_logs

    absent = tmp_path / "interface-qui-nexiste-pas"
    # `caplog` ne voit rien ici : la journalisation passe par structlog, que les tests ne routent pas
    # vers le module `logging`. Capturer au bon endroit est la différence entre vérifier le message
    # et vérifier qu'on n'a rien vu.
    with capture_logs() as journaux:
        appli = client(frontend_dir=absent)
    evenements = [e for e in journaux if e.get("event") == "interface_introuvable"]
    assert evenements, f"aucune erreur journalisée ; vu : {[e.get('event') for e in journaux]}"
    assert evenements[0]["log_level"] == "error"
    assert str(absent) in evenements[0]["chemin"]
    # L'API reste utilisable : une interface absente n'est pas une panne de l'API.
    assert appli.get("/api/v1/system/status").status_code == 200


def test_the_installer_checks_what_is_served_not_only_the_status_code() -> None:
    """`install.sh` validait un code 200 sur `/`. La réponse JSON en renvoyait un.

    Vérifier le code sans vérifier ce qui est servi, c'est valider une porte qui ouvre sur un mur.
    """
    texte = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "content_type" in texte, "l'installateur ne regarde pas ce qui est servi"
    assert "text/html*" in texte
    assert "l'interface n'est PAS servie" in texte


def test_the_image_declares_where_it_put_the_interface() -> None:
    """Le lien entre les deux moitiés : l'image pose la variable que le code lit."""
    dockerfile = (ROOT / "infra" / "Dockerfile").read_text(encoding="utf-8")
    assert f"{FRONTEND_DIR_ENV}=/app/frontend" in dockerfile
    assert "COPY --chown=10001:10001 frontend/ /app/frontend/" in dockerfile


def test_a_key_in_a_header_does_not_cause_an_infinite_redirect() -> None:
    """La redirection qui retire la clé de l'URL se déclenchait même sans clé dans l'URL.

    Sa condition portait sur « une clé a été acceptée », sans regarder d'OÙ elle venait. Présentée
    par l'en-tête `Authorization`, la clé provoquait donc une redirection vers la MÊME adresse — qui
    la représentait, qui redirigeait encore. Boucle infinie sur toute page HTML consultée avec un
    en-tête plutôt qu'un paramètre d'URL, c'est-à-dire sur tout usage propre de l'API.

    Rediriger n'a de sens que s'il y a quelque chose à retirer.
    """
    reponse = client().get("/", follow_redirects=False)
    assert reponse.status_code == 200, f"redirection inattendue vers {reponse.headers.get('location')}"


def test_a_key_in_the_url_is_still_stripped_by_a_redirect() -> None:
    """Contre-épreuve : quand la clé EST dans l'URL, la redirection doit toujours l'en retirer.

    C'est la raison d'être du mécanisme — une clé dans une URL traverse l'historique du navigateur
    et l'en-tête Referer — et le correctif ne doit pas l'avoir supprimée.
    """
    from okxq.api.app import role_key_for
    from okxq.api.auth import Role

    brut = TestClient(client().app, follow_redirects=False)  # type: ignore[attr-defined]
    reponse = brut.get(f"/?key={role_key_for(SECRET, Role.ADMIN)}")
    assert reponse.status_code == 303
    assert "key=" not in reponse.headers["location"]
