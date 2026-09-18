"""Réponse partiellement réussie d'un envoi par lot : traitement ITEM PAR ITEM (T37, §46).

Pourquoi ce cas compte. OKX répond à ``POST /api/v5/trade/batch-orders`` avec DEUX niveaux de code :
un ``code`` global et un ``sCode`` par item. Le code global vaut ``"0"`` si tout a réussi, ``"1"`` si
tout a échoué, et ``"2"`` si le lot est PARTIELLEMENT réussi. Un client qui ne lit que le code global
se trompe dans les deux sens, et les deux erreurs sont graves :

- lire ``code == "2"`` comme un échec fait « oublier » des ordres qui existent réellement sur
  l'échange : positions non suivies, réservations libérées à tort, protections manquantes ;
- lire ``code == "2"`` comme un succès invente des ordres qui n'ont jamais été acceptés : on attend des
  fills qui ne viendront pas, et l'exposition réelle diverge de l'exposition comptée.

Il n'existe donc aucun verdict global : chaque item porte le sien. Et un item ABSENT de la réponse
n'est pas un item refusé — c'est un item de statut INCONNU, qui impose une réconciliation avant tout
renvoi (§52.1), jamais un nouvel envoi à l'aveugle.

Hermétisme : transport HTTP factice (``httpx.MockTransport``), horloge simulée, aucun réseau. Les
formes de réponse sont celles documentées par OKX v5 ; elles sont ici des fixtures, pas des preuves
d'un contrat revalidé en ligne.
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
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import ExchangeError
from okxq.exchange.base import OrderRequest, PlaceOutcome, PlaceResponse
from okxq.exchange.okx.adapter import REGION_PROFILES, OkxExchangeAdapter
from okxq.exchange.okx.authentication import OkxCredentials
from okxq.exchange.okx.rest_private import OkxResponse, item_code

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
OK_ID = "cl_T37_ok"
BAD_ID = "cl_T37_refuse"
ABSENT_ID = "cl_T37_absent"

# ``sCode`` d'un item refusé (solde insuffisant). La valeur exacte importe : elle doit ressortir telle
# quelle dans la réponse de l'item, sinon l'opérateur ne peut pas diagnostiquer le refus.
BAD_S_CODE = "51008"
BAD_S_MSG = "Order failed. Insufficient USDT balance in account"


async def never_connects(url: str) -> Any:
    """Connecteur WebSocket qui refuse d'exister : aucun test ici n'ouvre le flux privé."""
    raise AssertionError(f"aucune socket ne doit être ouverte dans ce test ({url})")


def adapter(handler: Callable[[httpx.Request], httpx.Response]) -> OkxExchangeAdapter:
    """Adaptateur OKX en mode DEMO, branché sur un transport factice. Un adaptateur neuf par test."""
    return OkxExchangeAdapter(
        cfg=load_config("configs/demo.yaml"),
        credentials=OkxCredentials("cle-de-test", "secret-de-test", "phrase-de-test"),
        profile=REGION_PROFILES["global"],
        clock=SimulatedClock(T0),
        ws_connector=never_connects,
        transport=httpx.MockTransport(handler),
    )


def requests(*client_order_ids: str) -> list[OrderRequest]:
    return [
        OrderRequest(
            account_scope="okx-demo",
            client_order_id=cid,
            inst_id=INST,
            side="buy",
            contracts=Decimal("1"),
            price_limit=Decimal("65000"),
            order_type="limit",
            reduce_only=False,
        )
        for cid in client_order_ids
    ]


def batch_handler(
    *, code: str, items: list[dict[str, str]], seen: list[list[dict[str, Any]]] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Réponse de lot OKX à la forme documentée : ``code`` global + un ``sCode`` par item."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v5/trade/batch-orders"
        # Le drapeau DEMO est immuable et présent sur CHAQUE requête (T63).
        assert request.headers.get("x-simulated-trading") == "1"
        if seen is not None:
            seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"code": code, "msg": "message de lot", "data": items})

    return handler


def item(client_order_id: str, *, s_code: str = "0", order_id: str = "") -> dict[str, str]:
    return {
        "clOrdId": client_order_id,
        "ordId": order_id,
        "tag": "okxq",
        "sCode": s_code,
        "sMsg": "" if s_code == "0" else BAD_S_MSG,
    }


# --- lecture des codes : deux niveaux, jamais un seul ---------------------------------------------------


def test_T37_un_lot_partiellement_reussi_nest_ni_un_succes_ni_un_echec_global() -> None:
    """``code == "2"`` est explicitement un état PARTIEL : ni ``ok``, ni traitable en bloc.

    La contre-épreuve accompagne le refus : ``code == "0"`` reste un succès global, et ``code == "1"``
    n'est pas partiel. Un client qui rendrait ``ok`` pour tout — ou pour rien — échouerait ici.
    """
    partiel = OkxResponse(http_status=200, code="2", msg="partiel", data=[])
    assert partiel.partial is True
    assert partiel.ok is False
    complet = OkxResponse(http_status=200, code="0", msg="", data=[])
    assert complet.ok is True
    assert complet.partial is False
    echec = OkxResponse(http_status=200, code="1", msg="tout a échoué", data=[])
    assert echec.ok is False
    assert echec.partial is False


def test_T37_le_code_dun_item_absent_vaut_succes_et_un_item_refuse_porte_le_sien() -> None:
    """``item_code`` : un ``sCode`` manquant vaut ``"0"`` (OKX l'omet sur un item réussi).

    Cette convention n'est acceptable que parce que l'ABSENCE D'ITEM, elle, n'est jamais traitée comme
    un succès — ce que vérifie le test dédié plus bas. Sans cette seconde garantie, le défaut de
    ``sCode`` deviendrait un « succès par défaut ».
    """
    assert item_code({"clOrdId": OK_ID, "ordId": "ex_1"}) == ("0", "")
    assert item_code(item(BAD_ID, s_code=BAD_S_CODE)) == (BAD_S_CODE, BAD_S_MSG)


# --- l'adaptateur : un verdict par ordre ----------------------------------------------------------------


async def test_T37_un_lot_partiellement_reussi_est_traite_item_par_item() -> None:
    """Le cas central : un lot ``code == "2"``, un item accepté, un item refusé.

    L'ordre accepté doit ressortir ACK avec son identifiant d'échange — le perdre au prétexte que le
    lot n'a pas entièrement réussi laisserait un ordre vivant hors de notre journal. L'ordre refusé
    doit ressortir REJECTED avec SON code, pas avec le code global du lot : « 2 » ne dit rien à
    l'opérateur, « 51008 » lui dit quoi corriger.
    """
    seen: list[list[dict[str, Any]]] = []
    okx = adapter(
        batch_handler(
            code="2",
            items=[item(OK_ID, order_id="ex_1"), item(BAD_ID, s_code=BAD_S_CODE)],
            seen=seen,
        )
    )
    try:
        out = await okx.place_orders_batch(requests(OK_ID, BAD_ID))
    finally:
        await okx.rest.aclose()

    assert [r.client_order_id for r in out] == [OK_ID, BAD_ID]
    accepte, refuse = out
    assert accepte.outcome is PlaceOutcome.ACK
    assert accepte.exchange_order_id == "ex_1"
    assert accepte.code == "0"
    assert refuse.outcome is PlaceOutcome.REJECTED
    assert refuse.code == BAD_S_CODE
    assert refuse.message == BAD_S_MSG
    assert refuse.exchange_order_id is None
    # Un seul appel réseau pour le lot, et le payload envoyé porte bien les deux identifiants.
    assert okx.rest.requests_sent == 1
    assert [o["clOrdId"] for o in seen[0]] == [OK_ID, BAD_ID]


async def test_T37_un_item_absent_de_la_reponse_est_inconnu_et_jamais_suppose_reussi() -> None:
    """Un ordre envoyé dont l'item ne revient pas : statut UNKNOWN — l'ordre existe peut-être.

    C'est le cas le plus dangereux, parce qu'il est silencieux : rien ne le signale sauf le décompte
    des items. Le supposer réussi inventerait une position ; le supposer refusé libérerait une
    réservation sur un ordre peut-être vivant. La seule réponse honnête est « je ne sais pas », qui
    déclenche la réconciliation avant tout renvoi (§52.1, T32).
    """
    okx = adapter(batch_handler(code="2", items=[item(OK_ID, order_id="ex_1")]))
    try:
        out = await okx.place_orders_batch(requests(OK_ID, ABSENT_ID))
    finally:
        await okx.rest.aclose()

    by_id = {r.client_order_id: r for r in out}
    assert by_id[OK_ID].outcome is PlaceOutcome.ACK
    inconnu = by_id[ABSENT_ID]
    assert inconnu.outcome is PlaceOutcome.UNKNOWN
    assert inconnu.outcome is not PlaceOutcome.REJECTED
    assert inconnu.exchange_order_id is None  # aucune mesure ⇒ None, jamais une chaîne vide inventée


async def test_T37_un_lot_entierement_reussi_acquitte_chaque_ordre() -> None:
    """CONTRE-ÉPREUVE : sans elle, un adaptateur qui refuserait tout passerait les tests de refus.

    Lot ``code == "0"``, tous les ``sCode`` à ``"0"`` : chaque ordre est ACK, avec son propre
    identifiant d'échange (et non celui du premier item).
    """
    okx = adapter(
        batch_handler(
            code="0",
            items=[item(OK_ID, order_id="ex_1"), item(BAD_ID, order_id="ex_2")],
        )
    )
    try:
        out = await okx.place_orders_batch(requests(OK_ID, BAD_ID))
    finally:
        await okx.rest.aclose()

    assert [r.outcome for r in out] == [PlaceOutcome.ACK, PlaceOutcome.ACK]
    assert [r.exchange_order_id for r in out] == ["ex_1", "ex_2"]


async def test_T37_un_lot_entierement_refuse_rejette_chaque_ordre_avec_son_motif() -> None:
    """Lot ``code == "1"`` : chaque ordre est refusé, et chacun porte le motif de SON item.

    Deux motifs différents dans le même lot : si l'implémentation recopiait un seul code sur tous les
    items, ou le code global, cette assertion tomberait.
    """
    okx = adapter(
        batch_handler(
            code="1",
            items=[item(OK_ID, s_code="51000"), item(BAD_ID, s_code=BAD_S_CODE)],
        )
    )
    try:
        out = await okx.place_orders_batch(requests(OK_ID, BAD_ID))
    finally:
        await okx.rest.aclose()

    assert [r.outcome for r in out] == [PlaceOutcome.REJECTED, PlaceOutcome.REJECTED]
    assert [r.code for r in out] == ["51000", BAD_S_CODE]


async def test_T37_un_item_reussi_dans_un_lot_globalement_en_echec_reste_acquitte() -> None:
    """L'item prime sur le global : ``code == "1"`` avec un ``sCode == "0"`` reste un ACK.

    La situation est contradictoire côté fournisseur, et c'est précisément pour cela qu'elle est
    testée : l'ordre qui a été accepté doit entrer dans notre journal, sinon nous perdrions la trace
    d'un ordre vivant. La vérité de l'exchange est au niveau de l'item.
    """
    okx = adapter(
        batch_handler(code="1", items=[item(OK_ID, order_id="ex_1"), item(BAD_ID, s_code=BAD_S_CODE)])
    )
    try:
        out = await okx.place_orders_batch(requests(OK_ID, BAD_ID))
    finally:
        await okx.rest.aclose()

    assert out[0].outcome is PlaceOutcome.ACK
    assert out[0].exchange_order_id == "ex_1"
    assert out[1].outcome is PlaceOutcome.REJECTED


async def test_T37_une_ecriture_ambigue_rend_tous_les_items_inconnus_et_aucun_refuse() -> None:
    """Coupure pendant l'envoi du lot : TOUS les items deviennent UNKNOWN, aucun n'est refusé.

    Une écriture interrompue ne prouve rien : le lot peut être arrivé entièrement, partiellement ou
    pas du tout. Les marquer REJECTED autoriserait un renvoi à l'aveugle — exactement ce que §48
    interdit. Ils restent inconnus jusqu'à la réconciliation.
    """

    def coupure(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("coupure simulée", request=request)

    okx = adapter(coupure)
    try:
        out = await okx.place_orders_batch(requests(OK_ID, BAD_ID))
    finally:
        await okx.rest.aclose()

    assert [r.outcome for r in out] == [PlaceOutcome.UNKNOWN, PlaceOutcome.UNKNOWN]
    assert all(r.exchange_order_id is None for r in out)
    assert all(r.code == "EXCHANGE_AMBIGUOUS" for r in out)


async def test_T37_un_lot_vide_nappelle_pas_le_reseau_et_ne_fabrique_aucune_reponse() -> None:
    """Aucun ordre à envoyer ⇒ aucune requête et aucune réponse : pas d'item inventé.

    Un lot vide qui produirait un appel réseau (ou une réponse) ferait croire à un envoi ; ici la
    liste est vide et le compteur de requêtes reste à zéro.
    """
    okx = adapter(batch_handler(code="0", items=[]))
    try:
        assert await okx.place_orders_batch([]) == []
    finally:
        await okx.rest.aclose()
    assert okx.rest.requests_sent == 0


async def test_T37_un_refus_au_niveau_du_lot_nest_jamais_converti_en_acquittement() -> None:
    """Limite de débit sur le lot : rien n'a été accepté, et surtout rien n'est acquitté.

    OKX répond ici par un code de rate limit ; le client le remonte en erreur typée. Ce qui compte
    pour T37 est qu'aucune ``PlaceResponse`` ACK ne soit fabriquée à partir d'une réponse qui ne
    contient aucun item accepté.
    """
    okx = adapter(
        lambda request: httpx.Response(200, json={"code": "50011", "msg": "Rate limit", "data": []})
    )
    try:
        with pytest.raises(ExchangeError) as err:
            await okx.place_orders_batch(requests(OK_ID, BAD_ID))
    finally:
        await okx.rest.aclose()
    assert err.value.code == "RATE_LIMITED"


async def test_T37_un_ordre_unitaire_lit_aussi_le_code_de_son_item() -> None:
    """Le même principe hors lot : un envoi unitaire dont le ``sCode`` refuse n'est pas un succès.

    Le code global peut valoir ``"0"`` tandis que l'item refuse. Ne lire que le global rendrait un ACK
    pour un ordre qui n'existe pas — le pire des deux mondes, puisque rien ne le contredira ensuite.
    """

    async def envoyer(single: dict[str, str]) -> PlaceResponse:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v5/trade/order"
            return httpx.Response(200, json={"code": "0", "msg": "", "data": [single]})

        okx = adapter(handler)
        try:
            return await okx.place_order(requests(OK_ID)[0])
        finally:
            await okx.rest.aclose()

    acquitte = await envoyer(item(OK_ID, order_id="ex_1"))
    assert acquitte.outcome is PlaceOutcome.ACK
    assert acquitte.exchange_order_id == "ex_1"

    refuse = await envoyer(item(OK_ID, s_code=BAD_S_CODE))
    assert refuse.outcome is PlaceOutcome.REJECTED
    assert refuse.code == BAD_S_CODE
