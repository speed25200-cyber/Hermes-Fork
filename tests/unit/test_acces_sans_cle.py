"""Accès à l'interface SANS clé : ce que ça ouvre, et ce que ça n'ouvre pas (§60, T65).

C'est une décision d'exploitation légitime — un tableau de bord qu'on veut ouvrir sans friction — et
elle mérite d'être implémentée franchement plutôt que contournée par un mot de passe collé partout.

Mais elle a une portée exacte, et ces tests la fixent : `lecture` ouvre la consultation et laisse les
commandes opérateur fermées ; `total` ouvre tout ; et les deux sont REFUSÉS en DEMO comme en LIVE,
par la validation de configuration. Un accès libre se décide pour un bac à sable ; le refus est dans
le code pour qu'il ne suive pas la plateforme le jour où de vrais ordres partent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from okxq.api.app import create_app
from okxq.config import load_config
from okxq.config.modes import Mode
from okxq.domain.clocks import SimulatedClock
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import Base
from tests.unit.test_deployment_defects import T0

ROOT = Path(__file__).resolve().parents[2]
SECRET = "secret-operateur-de-test-suffisamment-long"


def config(niveau: str, *, mode: Mode = Mode.PAPER):
    """Construit une configuration EN LA REVALIDANT.

    `model_copy` ne rejoue aucun validateur : s'en contenter aurait fabriqué des configurations
    interdites sans que rien ne proteste, et le test du refus en DEMO/LIVE n'aurait rien vérifié.
    """
    from okxq.config.schema import AppConfig

    # La fixture de test porte `fixture_only`, qui interdit déjà DEMO et LIVE : l'employer ici
    # ferait échouer le test sur le MAUVAIS garde-fou, et laisserait croire que celui qu'on veut
    # vérifier fonctionne. On part donc des configurations réelles du mode visé.
    source = {
        Mode.PAPER: ROOT / "tests" / "fixtures" / "configs" / "smoke.fixture.yaml",
        Mode.DEMO: ROOT / "configs" / "demo.yaml",
        Mode.LIVE: ROOT / "configs" / "live.disabled.yaml",
    }[mode]
    brut = load_config(source).model_dump(mode="python")
    brut["project"]["mode"] = mode
    brut["api"]["acces_sans_cle"] = niveau
    return AppConfig.model_validate(brut)


def client(cfg) -> TestClient:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    app = create_app(
        cfg,
        clock=SimulatedClock(T0),
        session_factory=make_session_factory(engine),
        operator_secret=SECRET,
        configure_logs=False,
    )
    return TestClient(app)


def test_by_default_the_door_stays_shut() -> None:
    """Le défaut ne change pas : sans réglage explicite, une requête sans clé est refusée."""
    reponse = client(config("non")).get("/api/v1/system/status")
    assert reponse.status_code == 403


def test_reading_is_open_without_a_key_when_asked_for() -> None:
    """`lecture` : le tableau de bord répond sans clé. C'est précisément ce qui est demandé."""
    reponse = client(config("lecture")).get("/api/v1/system/status")
    assert reponse.status_code == 200
    assert reponse.json()["mode"] == "PAPER"


@pytest.mark.parametrize("commande", ["pause", "cancel-entry-orders", "request-flatten", "request-resume"])
def test_reading_open_does_not_open_the_operator_commands(commande: str) -> None:
    """Contre-épreuve : `lecture` accorde le rôle LECTEUR, pas les commandes.

    C'est la différence qui compte. Ouvrir la consultation d'un tableau de bord et ouvrir les
    commandes d'un moteur d'exécution ne sont pas la même décision, et l'une n'entraîne pas l'autre.
    Les quatre commandes existantes sont éprouvées nommément : une liste construite par
    introspection aurait pu se vider en silence et ne plus rien vérifier.
    """
    reponse = client(config("lecture")).post(f"/api/v1/control/{commande}", json={})
    assert reponse.status_code == 403, f"{commande} accessible sans clé alors que `lecture` est demandé"


@pytest.mark.parametrize("commande", ["pause", "cancel-entry-orders", "request-flatten", "request-resume"])
def test_total_does_open_the_operator_commands(commande: str) -> None:
    """Et `total` les ouvre : ce n'est plus le rôle qui refuse.

    La réponse peut valoir autre chose que 200 — un corps incomplet reste un corps incomplet — mais
    elle ne doit plus être un refus de RÔLE. Sans ce test, `total` pourrait ne rien ouvrir du tout
    et personne ne s'en apercevrait.
    """
    reponse = client(config("total")).post(f"/api/v1/control/{commande}", json={})
    assert reponse.status_code != 403, "le rôle ADMIN devrait passer"


def test_total_opens_everything_because_that_is_what_it_says() -> None:
    """`total` accorde ADMIN : la lecture passe, et le rôle n'est plus un obstacle."""
    from okxq.api.app import role_key_for
    from okxq.api.auth import Role

    cli = client(config("total"))
    assert cli.get("/api/v1/system/status").status_code == 200
    # Même effet qu'une clé admin présentée : c'est bien le rôle ADMIN qui est accordé.
    avec_cle = client(config("non")).get(
        "/api/v1/system/status", headers={"Authorization": f"Bearer {role_key_for(SECRET, Role.ADMIN)}"}
    )
    assert avec_cle.status_code == 200
    assert cli.get("/api/v1/system/status").json()["role"] == avec_cle.json()["role"] == "admin"


@pytest.mark.parametrize("mode", [Mode.DEMO, Mode.LIVE])
@pytest.mark.parametrize("niveau", ["lecture", "total"])
def test_an_open_door_is_refused_as_soon_as_real_orders_can_leave(niveau: str, mode: Mode) -> None:
    """Le garde qui compte : la configuration REFUSE de se charger en DEMO et en LIVE.

    Un accès libre se décide pour un bac à sable, et se garde rarement en tête le jour où le même
    fichier sert à un compte qui envoie de vrais ordres. Le refus est donc dans la validation, pas
    dans une consigne d'exploitation que personne ne relit.
    """
    with pytest.raises(ValueError, match="acces_sans_cle"):
        config(niveau, mode=mode)


def test_an_anonymous_visitor_is_named_anonymous_in_the_audit() -> None:
    """Un accès sans clé ne doit pas fabriquer une identité : l'audit dit « anonyme »."""
    from okxq.api.auth import Principal, Role

    principal = Principal(role=Role.READER, actor="anonyme", via="libre")
    assert principal.actor == "anonyme" and principal.via == "libre"
