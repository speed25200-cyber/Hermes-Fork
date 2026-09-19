"""Parité entre le moteur de RÉFÉRENCE (batch) et le moteur qui tourne en PRODUCTION (incrémental).

Cette parité est affirmée dans le docstring de `features/incremental.py` — « parité T16 » — et elle
n'était vérifiée nulle part. Aucun test n'exerçait le moteur incrémental, celui qui décide sur le
serveur. Trois déploiements de suite ont rendu `CAUSALITY_VIOLATION` pour trois raisons différentes,
et chaque correction partielle paraissait bonne faute d'un test qui compare au bon comportement.

`batch.py`, lui, avait raison depuis le début : il borne sur `available_at`, comme son docstring
l'annonce. Le moteur incrémental bornait sur `ts`. Les deux instants coïncident dans un jeu d'essai
écrit à la main, jamais sur un flux réel où le réseau ajoute un délai — d'où un défaut invisible en
test et systématique en production.

Ces tests comparent donc les deux moteurs sur des données où `ts` et `available_at` DIFFÈRENT. C'est
le seul cas qui distingue une borne juste d'une borne fausse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from okxq.domain.events import EventEnvelope
from okxq.features.batch import BatchStateBuilder, compute_batch
from okxq.features.incremental import IncrementalFeatureEngine
from okxq.features.registry import FeatureEngine, default_registry

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
COUPURE = T0 + timedelta(seconds=120)
INST = "BTC-USDT-SWAP"


def enveloppe(event_type: str, *, ts: datetime, recu_a: datetime, seq: int, payload: dict) -> EventEnvelope:
    """Une enveloppe où l'horodatage d'échange et l'instant de réception sont DISTINCTS."""
    return EventEnvelope(
        event_id=f"evt_{event_type}_{seq}",
        event_type=event_type,
        schema_version=1,
        source="test",
        exchange_ts=ts,
        receive_ts=recu_a,
        available_at=recu_a,
        ingest_seq=seq,
        payload_hash="h" * 64,
        payload={"inst_id": INST, **payload},
    )


def carnet(*, ts: datetime, recu_a: datetime, seq: int, bid: str, ask: str) -> EventEnvelope:
    return enveloppe(
        "book.snapshot",
        ts=ts,
        recu_a=recu_a,
        seq=seq,
        payload={
            "ts_ms": str(int(ts.timestamp() * 1000)),
            "seq_id": str(seq),
            "bids": [[bid, "10"]],
            "asks": [[ask, "10"]],
        },
    )


def echange(*, ts: datetime, recu_a: datetime, seq: int, prix: str) -> EventEnvelope:
    return enveloppe(
        "trade",
        ts=ts,
        recu_a=recu_a,
        seq=seq,
        payload={
            "ts_ms": str(int(ts.timestamp() * 1000)),
            "price": prix,
            "qty_contracts": "1",
            "side": "buy",
        },
    )


def marque(*, ts: datetime, recu_a: datetime, seq: int, valeur: str) -> EventEnvelope:
    return enveloppe(
        "mark_price",
        ts=ts,
        recu_a=recu_a,
        seq=seq,
        payload={"ts_ms": str(int(ts.timestamp() * 1000)), "mark_px": valeur},
    )


#: Le retard de livraison : ce qui sépare « le marché l'a fait » de « nous l'avons su ».
RETARD = timedelta(seconds=4)


def flux_avec_retard() -> list[EventEnvelope]:
    """Des événements normaux, puis trois dont la réception tombe APRÈS la coupure.

    Les trois derniers portent un horodatage d'échange ANTÉRIEUR à la coupure : une borne posée sur
    `ts` les accepterait, une borne posée sur `available_at` les écarte. Leurs valeurs sont
    volontairement aberrantes pour que toute fuite se voie.
    """
    evs = [
        carnet(ts=T0 + timedelta(seconds=30), recu_a=T0 + timedelta(seconds=31), seq=1, bid="100", ask="101"),
        echange(ts=T0 + timedelta(seconds=40), recu_a=T0 + timedelta(seconds=41), seq=2, prix="100.5"),
        marque(ts=T0 + timedelta(seconds=50), recu_a=T0 + timedelta(seconds=51), seq=3, valeur="100.4"),
        carnet(ts=T0 + timedelta(seconds=90), recu_a=T0 + timedelta(seconds=91), seq=4, bid="102", ask="103"),
        echange(ts=T0 + timedelta(seconds=100), recu_a=T0 + timedelta(seconds=101), seq=5, prix="102.5"),
    ]
    # Horodatés avant la coupure, reçus après : indisponibles au moment de décider.
    tard = COUPURE - timedelta(seconds=2)
    evs += [
        carnet(ts=tard, recu_a=COUPURE + RETARD, seq=6, bid="900", ask="901"),
        echange(ts=tard, recu_a=COUPURE + RETARD, seq=7, prix="900.5"),
        marque(ts=tard, recu_a=COUPURE + RETARD, seq=8, valeur="900.4"),
    ]
    return evs


def par_reference(evs: list[EventEnvelope]) -> dict[str, float | None]:
    _, calculs = compute_batch(
        FeatureEngine(default_registry()), BatchStateBuilder(evs), [COUPURE], instruments=[INST]
    )
    v = calculs[0].vector
    return dict(zip(v.names, v.values, strict=True))


def par_incremental(evs: list[EventEnvelope]) -> dict[str, float | None]:
    inc = IncrementalFeatureEngine(FeatureEngine(default_registry()), flux_continu=True)
    for env in sorted(evs, key=lambda e: (e.available_at, e.ingest_seq)):
        inc.ingest(env)
    v = inc.compute(INST, COUPURE).vector
    return dict(zip(v.names, v.values, strict=True))


def test_les_deux_moteurs_calculent_la_meme_chose_malgre_les_retards() -> None:
    """Le test qui manquait, et qui aurait évité trois cycles de correction au jugé.

    Sur un flux, `ts` et `available_at` diffèrent toujours. Un moteur qui borne le mauvais instant
    voit des données que l'autre écarte, et les deux divergent — silencieusement, puisqu'aucune
    exception n'est levée quand la fuite reste sous le seuil de la vérification.
    """
    evs = flux_avec_retard()
    reference = par_reference(evs)
    production = par_incremental(evs)
    assert set(reference) == set(production), "les deux moteurs ne produisent pas les mêmes features"
    ecarts = {
        nom: (reference[nom], production[nom])
        for nom in reference
        if reference[nom] != production[nom] and not (reference[nom] is None and production[nom] is None)
    }
    assert not ecarts, f"le moteur de production diverge de la référence : {ecarts}"


def test_la_reference_elle_meme_ignore_les_evenements_recus_apres_la_coupure() -> None:
    """Contre-épreuve : si la référence les prenait, comparer à elle ne prouverait rien.

    On lui retire les trois événements en retard : son résultat doit être IDENTIQUE, ce qui établit
    qu'elle ne les utilisait pas. Sans ce contrôle, le test précédent pourrait passer avec deux
    moteurs faux de la même façon.
    """
    complet = par_reference(flux_avec_retard())
    sans_retard = par_reference(flux_avec_retard()[:5])
    assert complet == sans_retard, (
        "la référence utilise des événements reçus après la coupure : elle ne peut pas servir d'étalon"
    )


@pytest.mark.parametrize("retard_s", [1, 4, 30])
def test_la_parite_tient_quel_que_soit_le_retard(retard_s: int) -> None:
    """Un délai de livraison plus ou moins long ne doit rien changer à l'accord des deux moteurs."""
    tard = COUPURE - timedelta(seconds=2)
    evs = [
        *flux_avec_retard()[:5],
        carnet(ts=tard, recu_a=COUPURE + timedelta(seconds=retard_s), seq=6, bid="900", ask="901"),
    ]
    assert par_reference(evs) == par_incremental(evs), f"divergence avec un retard de {retard_s} s"
