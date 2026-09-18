"""Un échec DEMO ne bascule JAMAIS vers LIVE — ni de domaine, ni d'en-tête, ni de clés (T63, §57).

Le danger est précis et il est banal : un client qui « réessaie autrement » quand DEMO ne répond pas.
Retirer l'en-tête `x-simulated-trading`, retomber sur le domaine de production, reprendre un autre
profil de région — chacun de ces gestes, écrit avec les meilleures intentions de robustesse, envoie un
ordre RÉEL avec de l'argent RÉEL en réponse à une panne de l'environnement de test. C'est la seule
défaillance de ce dépôt qui coûte de l'argent sans qu'aucune ligne de code n'ait l'air fautive.

La protection est structurelle plutôt que défensive : DEMO et LIVE sont deux instances distinctes,
`_demo` est fixé une fois pour toutes depuis `cfg.mode` à la construction, et rien dans le chemin
d'erreur n'y touche. Ces tests l'exercent en faisant échouer l'échange de toutes les façons qu'il sait
échouer, puis en vérifiant l'invariant après coup.

Hermétisme : transport `httpx.MockTransport`, horloge simulée, aucune socket. Aucun appel réel n'a
jamais été émis vers OKX depuis ce dépôt — ce test vérifie une propriété du code, pas du serveur.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from okxq.config import load_config
from okxq.config.modes import Mode
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import ConfigError, LiveGuardError
from okxq.exchange.base import OrderRequest, PlaceOutcome
from okxq.exchange.okx.adapter import REGION_PROFILES, OkxExchangeAdapter, demo_safety_invariant
from okxq.exchange.okx.authentication import DEMO_HEADER, OkxCredentials

#: L'en-tête écrit EN TOUTES LETTRES, et non repris de `DEMO_HEADER`.
#:
#: Une première version de ce test comparait l'en-tête reçu à la constante du code. Elle passait donc
#: encore après avoir renommé la constante en `x-simulated-trading-DISABLED` : le test comparait le
#: code à lui-même et n'aurait pas vu partir de vrais ordres. Ce que la simulation exige est une
#: chaîne précise attendue par OKX, pas une constante interne — c'est elle qu'on vérifie.
ENTETE_SIMULATION = ("x-simulated-trading", "1")

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
#: Domaines attendus par profil, écrits EN TOUTES LETTRES pour la même raison que l'en-tête de
#: simulation : les relire depuis `REGION_PROFILES` ferait comparer le code à lui-même, et une
#: édition malencontreuse qui ferait pointer `eea` vers le domaine global passerait inaperçue.
#: Source : `infra/capability_manifest.json` et docs-v5, consultés le 2026-09-18.
DOMAINES_ATTENDUS = {
    "global": "https://www.okx.com",
    "eea": "https://eea.okx.com",
    "us": "https://app.okx.com",
}


async def never_connects(url: str) -> Any:
    raise AssertionError(f"aucune socket ne doit être ouverte dans ce test ({url})")


def adapter(handler: Callable[[httpx.Request], httpx.Response], *, region: str = "global"):
    return OkxExchangeAdapter(
        cfg=load_config("configs/demo.yaml"),
        credentials=OkxCredentials("cle-de-test", "secret-de-test", "phrase-de-test"),
        profile=REGION_PROFILES[region],
        clock=SimulatedClock(T0),
        ws_connector=never_connects,
        transport=httpx.MockTransport(handler),
    )


def order() -> OrderRequest:
    return OrderRequest(
        account_scope="okx-demo",
        client_order_id="cl_T63",
        inst_id=INST,
        side="buy",
        contracts=Decimal("1"),
        price_limit=Decimal("65000"),
        order_type="limit",
        reduce_only=False,
    )


#: Façons dont l'échange peut échouer, telles qu'OKX les exprime. Chacune est un moment où un client
#: « robuste » pourrait être tenté de réessayer ailleurs.
ECHECS: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "service_indisponible": lambda r: httpx.Response(503, text="Service Unavailable"),
    "identifiants_refuses": lambda r: httpx.Response(
        401, json={"code": "50111", "msg": "Invalid Authorization", "data": []}
    ),
    "environnement_demo_absent": lambda r: httpx.Response(
        400, json={"code": "50102", "msg": "Timestamp request expired", "data": []}
    ),
    "corps_illisible": lambda r: httpx.Response(200, text="<html>maintenance</html>"),
    "echec_applicatif": lambda r: httpx.Response(
        200, json={"code": "51008", "msg": "Insufficient balance", "data": []}
    ),
}


@pytest.mark.parametrize("panne", sorted(ECHECS))
async def test_T63_no_failure_mode_turns_a_demo_adapter_into_a_live_one(panne: str) -> None:
    """Quelle que soit la panne, l'adaptateur reste DEMO et ne vise que le domaine de son profil."""
    vues: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vues.append(request)
        return ECHECS[panne](request)

    ox = adapter(handler)
    assert ox.demo is True and ox.name == "okx-demo"

    for _ in range(3):  # plusieurs tentatives : c'est au rejeu qu'un repli se glisserait
        reponse = await ox.place_order(order())
        # L'échec est RENDU, pas levé : un ordre dont l'issue est refusée ou inconnue n'est jamais
        # rejoué à l'aveugle. Ce qui compte ici est qu'aucune de ces issues n'ouvre un autre chemin.
        assert reponse.outcome in (PlaceOutcome.REJECTED, PlaceOutcome.UNKNOWN)

    assert vues, "aucune requête émise : le test ne vérifierait rien"
    for request in vues:
        # L'en-tête de simulation est présent sur CHAQUE requête, y compris les rejeux après échec.
        assert request.headers.get(ENTETE_SIMULATION[0]) == ENTETE_SIMULATION[1], (
            f"requête sans en-tête de simulation après « {panne} » : elle serait partie en réel"
        )
    assert demo_safety_invariant(ox)
    assert ox.demo is True and ox.rest.demo is True and ox.cfg.mode is Mode.DEMO


async def test_T63_a_demo_failure_never_reaches_another_region_domain() -> None:
    """Le domaine vient du profil de compte, jamais d'un repli choisi après une erreur."""
    hotes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hotes.append(f"{request.url.scheme}://{request.url.host}")
        return httpx.Response(503, text="Service Unavailable")

    for region, profile in REGION_PROFILES.items():
        ox = adapter(handler, region=region)
        reponse = await ox.place_order(order())
        assert reponse.outcome in (PlaceOutcome.REJECTED, PlaceOutcome.UNKNOWN)
        attendu = DOMAINES_ATTENDUS[region]
        assert profile.rest_base_url == attendu, f"profil {region} : domaine déclaré inattendu"
        assert hotes[-1] == attendu, f"profil {region} : requête partie vers {hotes[-1]} au lieu de {attendu}"
    # Trois profils, trois domaines DISTINCTS : un effondrement vers un domaine unique serait
    # exactement le repli que ce test existe pour interdire.
    assert len(set(hotes)) == len(DOMAINES_ATTENDUS) == len(REGION_PROFILES)


async def test_T63_the_demo_flag_cannot_be_flipped_after_construction() -> None:
    """`demo` est dérivé du MODE, pas d'un drapeau modifiable : il n'existe aucun interrupteur."""
    ox = adapter(lambda r: httpx.Response(503))
    assert isinstance(type(ox).demo, property), "`demo` doit rester une lecture dérivée"
    with pytest.raises(AttributeError):
        ox.demo = False  # type: ignore[misc]
    assert ox.demo is True


async def test_T63_a_rest_client_built_for_live_is_refused_by_a_demo_adapter() -> None:
    """Le montage incohérent est refusé à la construction, pas découvert au premier ordre.

    C'est la porte la plus étroite du système : un client REST LIVE réutilisé par mégarde dans un
    adaptateur DEMO signerait de vraies requêtes. Le contrôle est en dur (`_rest.demo != _demo`).
    """
    from okxq.exchange.okx.rate_limits import DEFAULT_RULES, RateLimiter
    from okxq.exchange.okx.rest_private import OkxPrivateRestClient

    clock = SimulatedClock(T0)
    live_rest = OkxPrivateRestClient(
        credentials=OkxCredentials("cle-de-test", "secret-de-test", "phrase-de-test"),
        profile=REGION_PROFILES["global"],
        demo=False,  # <- client de PRODUCTION
        clock=clock,
        rate_limiter=RateLimiter(DEFAULT_RULES, clock=clock),
        account_scope="okx-demo",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "0", "data": []})),
    )
    with pytest.raises(ConfigError):
        OkxExchangeAdapter(
            cfg=load_config("configs/demo.yaml"),
            credentials=OkxCredentials("cle-de-test", "secret-de-test", "phrase-de-test"),
            profile=REGION_PROFILES["global"],
            clock=clock,
            ws_connector=never_connects,
            rest=live_rest,
        )


async def test_T63_T64_reaching_live_still_requires_an_authorization() -> None:
    """Contre-épreuve : même en demandant LIVE explicitement, la porte reste fermée (T64).

    Ce test complète T63 par l'autre bout. T63 dit qu'aucune panne ne fait GLISSER vers LIVE ; ici on
    vérifie que le chemin délibéré est lui aussi refusé sans autorisation vérifiée. Une panne DEMO ne
    peut donc pas non plus « débloquer » LIVE indirectement : il n'existe pas de chemin à débloquer.
    """
    demo = load_config("configs/demo.yaml")
    # `AppConfig.mode` est une PROPRIÉTÉ dérivée de `project.mode` : on ne peut pas basculer le mode
    # en écrivant un attribut, il faut refaire la section projet. C'est déjà une protection en soi.
    assert isinstance(type(demo).mode, property)
    assert demo.model_copy(update={"mode": Mode.LIVE}).mode is Mode.DEMO
    cfg = demo.model_copy(update={"project": demo.project.model_copy(update={"mode": Mode.LIVE})})
    assert cfg.mode is Mode.LIVE
    with pytest.raises(LiveGuardError):
        OkxExchangeAdapter(
            cfg=cfg,
            credentials=OkxCredentials("cle-de-test", "secret-de-test", "phrase-de-test"),
            profile=REGION_PROFILES["global"],
            clock=SimulatedClock(T0),
            ws_connector=never_connects,
            transport=httpx.MockTransport(lambda r: httpx.Response(503)),
        )


def test_T63_the_constant_and_the_wire_format_still_agree() -> None:
    """Et si la constante du code s'écarte de l'en-tête attendu, on veut le savoir ICI."""
    assert DEMO_HEADER == ENTETE_SIMULATION


def test_T63_the_json_fixture_shapes_used_here_are_the_documented_ones() -> None:
    """Garde-fou du garde-fou : les corps d'erreur doivent rester des formes OKX valides."""
    for name, make in ECHECS.items():
        response = make(httpx.Request("POST", "https://example.invalid"))
        if response.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(response.content)
            assert "code" in body and "msg" in body, f"forme d'erreur inattendue pour {name}"
