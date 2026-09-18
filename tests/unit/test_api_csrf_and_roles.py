"""T65 : une action d'interface non autorisée ne produit AUCUN effet, et le refus est audité.

C'était la plus grosse lacune de tests du dépôt : l'authentification, les sessions signées, la
double soumission CSRF et l'audit des refus étaient implémentés, mais tous les tests d'interface
présentaient une clé valide. Une protection jamais éprouvée n'est pas une protection.

Le critère de réussite n'est pas seulement « le serveur répond 403 ». C'est « aucune ligne n'a été
écrite ». Un refus qui laisse une trace d'exécution derrière lui n'est pas un refus.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from okxq.api.app import create_app, role_key_for
from okxq.api.auth import COOKIE_CSRF, COOKIE_SESSION, HEADER_CSRF, Role
from okxq.config import load_config
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import Base, OperatorAction

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "tests" / "fixtures" / "configs" / "smoke.fixture.yaml"
SECRET = "secret-csrf-et-roles-de-test-0123456789"

COMMAND = "/api/v1/control/request-flatten"
BODY = {
    "reason": "test de refus : aucune action ne doit en résulter",
    "scope": "all",
    "request_id": "test-csrf-0001",
    "actor": "testeur",
}


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def app(factory):
    return create_app(
        cfg=load_config(CONFIG),
        session_factory=factory,
        operator_secret=SECRET,
        configure_logs=False,
    )


def _actions(factory) -> list[OperatorAction]:
    with factory() as session:
        return list(session.query(OperatorAction).all())


def _requested(factory) -> list[OperatorAction]:
    """Demandes RÉELLEMENT enregistrées, c'est-à-dire actionnables par le runtime.

    La table ``operator_actions`` porte aussi les refus, écrits par la piste d'audit avec le statut
    DENIED : consigner un refus est une exigence, pas une fuite. Ce qu'un refus ne doit jamais
    produire, c'est une ligne que le runtime pourrait exécuter.
    """
    return [a for a in _actions(factory) if a.status not in ("DENIED", "CSRF_REJECTED")]


def _denials(factory) -> list[OperatorAction]:
    return [a for a in _actions(factory) if a.status in ("DENIED", "CSRF_REJECTED")]


def _assert_refused_without_effect(factory) -> None:
    """Le critère : aucune demande actionnable, et le refus laisse une trace d'audit."""
    assert _requested(factory) == [], "un refus qui écrit une demande actionnable n'est pas un refus"
    denials = _denials(factory)
    assert denials, "un refus non audité est invérifiable après coup"
    for row in denials:
        # Un refus est terminé au moment où il est prononcé : il n'attend rien du runtime.
        assert row.completed_at is not None


def _key(role: Role) -> str:
    return role_key_for(SECRET, role)


# --- absence de clé ------------------------------------------------------------------------------------


def test_T65_an_anonymous_command_is_refused_and_writes_nothing(app, factory) -> None:
    response = TestClient(app).post(COMMAND, json=BODY)
    assert response.status_code == 403
    _assert_refused_without_effect(factory)


# --- rôle insuffisant ---------------------------------------------------------------------------------


def test_T65_a_reader_cannot_request_a_flatten_and_writes_nothing(app, factory) -> None:
    """Le rôle de lecture est délibérément incapable d'agir : l'interface est en lecture seule.

    Un lecteur qui peut demander un flatten rendrait la distinction des rôles décorative.
    """
    response = TestClient(app).post(
        COMMAND, json=BODY, headers={"Authorization": f"Bearer {_key(Role.READER)}"}
    )
    assert response.status_code == 403
    _assert_refused_without_effect(factory)


def test_T65_an_operator_can_request_a_flatten_and_it_is_only_a_request(app, factory) -> None:
    """Contre-épreuve : sans elle, un test de refus passerait aussi sur une API cassée.

    La réponse est 202 et non 200 : la demande est ENREGISTRÉE, pas exécutée. Le runtime l'exécute
    sous préconditions et renseigne son résultat.
    """
    response = TestClient(app).post(
        COMMAND, json=BODY, headers={"Authorization": f"Bearer {_key(Role.OPERATOR)}"}
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "REQUESTED"
    rows = _requested(factory)
    assert len(rows) == 1
    assert rows[0].action == "REQUEST_FLATTEN"
    # Une demande acceptée n'est pas une action effectuée : le distinguer est tout l'intérêt.
    assert rows[0].completed_at is None


def test_T65_the_same_request_id_is_idempotent(app, factory) -> None:
    """Un double-clic ne doit pas produire deux demandes de sortie de position."""
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_key(Role.OPERATOR)}"}
    first = client.post(COMMAND, json=BODY, headers=headers)
    second = client.post(COMMAND, json=BODY, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert len(_requested(factory)) == 1


# --- double soumission CSRF ---------------------------------------------------------------------------


def _session_cookies(app) -> tuple[str, str]:
    """Ouvre la page pour obtenir une session signée et son jeton CSRF, comme le ferait un navigateur."""
    client = TestClient(app)
    client.get(f"/?key={_key(Role.OPERATOR)}")
    session = client.cookies.get(COOKIE_SESSION)
    csrf = client.cookies.get(COOKIE_CSRF)
    assert session and csrf, "la page doit délivrer une session signée et un jeton CSRF"
    return session, csrf


def test_T65_a_session_without_the_csrf_header_is_refused_and_writes_nothing(app, factory) -> None:
    """Le cœur de la protection : le cookie seul ne suffit jamais pour une action modifiante.

    Un site tiers peut faire envoyer les cookies du navigateur ; il ne peut pas lire leur valeur
    pour la recopier dans un en-tête. C'est précisément ce que la double soumission exploite.
    """
    session, csrf = _session_cookies(app)
    client = TestClient(app)
    client.cookies.set(COOKIE_SESSION, session)
    client.cookies.set(COOKIE_CSRF, csrf)
    response = client.post(COMMAND, json=BODY)  # cookies envoyés, en-tête absent
    assert response.status_code == 403
    assert "CSRF" in response.text.upper()
    _assert_refused_without_effect(factory)


def test_T65_a_session_with_a_wrong_csrf_header_is_refused_and_writes_nothing(app, factory) -> None:
    session, csrf = _session_cookies(app)
    client = TestClient(app)
    client.cookies.set(COOKIE_SESSION, session)
    client.cookies.set(COOKIE_CSRF, csrf)
    response = client.post(COMMAND, json=BODY, headers={HEADER_CSRF: csrf + "altere"})
    assert response.status_code == 403
    _assert_refused_without_effect(factory)


def test_T65_a_session_with_the_matching_csrf_header_is_accepted(app, factory) -> None:
    """Contre-épreuve de la protection CSRF : l'interface légitime doit continuer à fonctionner."""
    session, csrf = _session_cookies(app)
    client = TestClient(app)
    client.cookies.set(COOKIE_SESSION, session)
    client.cookies.set(COOKIE_CSRF, csrf)
    response = client.post(COMMAND, json=BODY, headers={HEADER_CSRF: csrf})
    assert response.status_code == 202, response.text
    assert len(_requested(factory)) == 1


def test_T65_a_forged_session_cookie_is_refused_and_writes_nothing(app, factory) -> None:
    """Le cookie n'est pas une clé : c'est une session SIGNÉE. Une signature invalide ne passe pas."""
    client = TestClient(app)
    client.cookies.set(COOKIE_SESSION, "operator|2099-01-01T00:00:00Z|jeton|signature-inventee")
    client.cookies.set(COOKIE_CSRF, "jeton")
    response = client.post(COMMAND, json=BODY, headers={HEADER_CSRF: "jeton"})
    assert response.status_code == 403
    _assert_refused_without_effect(factory)


# --- aucune commande ne peut activer LIVE ---------------------------------------------------------


def test_T65_no_control_command_can_enable_live_or_change_the_mode(app, factory) -> None:
    """Le jeu de commandes est FERMÉ : il n'existe aucun verbe capable d'activer LIVE.

    Si un tel verbe apparaissait un jour, ce test échouerait — ce qui est le but.
    """
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_key(Role.ADMIN)}"}
    for forbidden in ("enable-live", "set-mode", "go-live", "force"):
        response = client.post(
            f"/api/v1/control/{forbidden}",
            json={**BODY, "request_id": f"test-{forbidden}"},
            headers=headers,
        )
        assert response.status_code in (404, 422), f"{forbidden} ne doit pas être une commande"
    assert _requested(factory) == []
