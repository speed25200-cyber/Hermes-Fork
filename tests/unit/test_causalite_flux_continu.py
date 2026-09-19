"""Un flux qui ne s'arrête jamais ne peut pas satisfaire un refus « tu as des données trop récentes ».

Observé en marche réelle sur le VPS : le moteur PAPER, connecté au flux public d'OKX avec 467
instruments, rendait dix décisions en dix minutes, toutes en `FAILED` sur `CAUSALITY_VIOLATION`.

La cause : `IncrementalFeatureEngine.compute` refusait de calculer dès qu'un événement postérieur à
la coupure avait été ingéré. En REJEU c'est juste — on contrôle l'ordre de lecture, et un événement
postérieur signale un défaut. En MARCHE CONTINUE la tâche de drainage ingère en permanence : au
moment où la frontière de minute demande son calcul, des événements postérieurs sont forcément déjà
entrés. AUCUNE valeur de coupure ne satisfait à la fois ce refus et la frontière.

Ce que ce refus disait n'était pas « j'ai utilisé des données trop récentes » mais « j'en ai en
mémoire ». Sur un flux, c'est vrai en permanence, et cela n'apprend rien.

Ce qui porte réellement la garantie point-in-time, et qui n'est PAS touché :
  - la SÉLECTION : `_state` ne retient que `ts <= cutoff` ;
  - la VÉRIFICATION : `PointInTimeState.__post_init__` lève encore si l'état construit dépasse.

Ces tests vérifient les deux moitiés : que la marche continue aboutit, et qu'elle n'utilise pour
autant AUCUNE donnée postérieure à la coupure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from okxq.domain.errors import CausalityError
from okxq.domain.events import EventEnvelope
from okxq.features.incremental import IncrementalFeatureEngine
from okxq.features.registry import FeatureEngine, default_registry

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"


def enveloppe(event_type: str, *, available_at: datetime, seq: int, payload: dict) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt_{event_type}_{seq}",
        event_type=event_type,
        schema_version=1,
        source="test",
        exchange_ts=available_at,
        receive_ts=available_at,
        available_at=available_at,
        ingest_seq=seq,
        payload_hash="h" * 64,
        payload={"inst_id": INST, **payload},
    )


def carnet(*, at: datetime, seq: int, bid: str, ask: str) -> EventEnvelope:
    return enveloppe(
        "book.snapshot",
        available_at=at,
        seq=seq,
        payload={
            "ts_ms": str(int(at.timestamp() * 1000)),
            "seq_id": str(seq),
            "bids": [[bid, "10"]],
            "asks": [[ask, "10"]],
        },
    )


def echange(*, at: datetime, seq: int, prix: str) -> EventEnvelope:
    return enveloppe(
        "trade",
        available_at=at,
        seq=seq,
        payload={
            "ts_ms": str(int(at.timestamp() * 1000)),
            "price": prix,
            "qty_contracts": "1",
            "side": "buy",
        },
    )


def moteur(*, flux_continu: bool) -> IncrementalFeatureEngine:
    return IncrementalFeatureEngine(FeatureEngine(default_registry()), flux_continu=flux_continu)


#: La coupure est la frontière de minute ; le flux continue de livrer après elle.
COUPURE = T0 + timedelta(seconds=60)
APRES = COUPURE + timedelta(seconds=3)


def alimenter(inc: IncrementalFeatureEngine) -> None:
    """Deux événements avant la coupure, un après — exactement ce que produit un flux vivant."""
    inc.ingest(carnet(at=T0 + timedelta(seconds=30), seq=1, bid="100", ask="101"))
    inc.ingest(echange(at=T0 + timedelta(seconds=40), seq=2, prix="100.5"))
    inc.ingest(carnet(at=APRES, seq=3, bid="900", ask="901"))


def test_le_rejeu_refuse_toujours_un_evenement_posterieur_a_la_coupure() -> None:
    """Le défaut est INCHANGÉ : en rejeu, un événement postérieur reste un défaut du jeu de données.

    C'est la contre-épreuve du test suivant. Sans elle, relâcher la garde partout passerait
    inaperçu, et la recherche perdrait sa protection contre l'anticipation.
    """
    inc = moteur(flux_continu=False)
    alimenter(inc)
    with pytest.raises(CausalityError):
        inc.compute(INST, COUPURE)


def test_la_marche_continue_aboutit_malgre_un_evenement_posterieur() -> None:
    """Le défaut corrigé : sur un flux, le calcul doit aboutir au lieu d'échouer chaque minute."""
    inc = moteur(flux_continu=True)
    alimenter(inc)
    calcul = inc.compute(INST, COUPURE)
    assert calcul is not None
    assert calcul.vector.cutoff_at == COUPURE


def test_la_marche_continue_nutilise_pas_la_donnee_posterieure() -> None:
    """Le cœur de l'affaire : aboutir ne doit pas vouloir dire regarder après la coupure.

    Le carnet postérieur porte un prix absurdement éloigné (900/901 contre 100/101). S'il fuitait
    dans le calcul, le mid en porterait la trace — c'est exactement l'anticipation qu'on interdit.
    """
    apres_seulement = moteur(flux_continu=True)
    apres_seulement.ingest(carnet(at=T0 + timedelta(seconds=30), seq=1, bid="100", ask="101"))
    apres_seulement.ingest(echange(at=T0 + timedelta(seconds=40), seq=2, prix="100.5"))
    sans_futur = apres_seulement.compute(INST, COUPURE)

    avec_futur = moteur(flux_continu=True)
    alimenter(avec_futur)
    avec = avec_futur.compute(INST, COUPURE)

    assert avec.vector.names == sans_futur.vector.names
    assert avec.vector.values == sans_futur.vector.values, (
        "un événement postérieur à la coupure a changé les valeurs calculées : anticipation"
    )
    assert avec.vector.masks == sans_futur.vector.masks


def test_un_carnet_a_la_coupure_est_bien_retenu() -> None:
    """Aboutir en jetant le carnet serait une fausse victoire : on décide alors sur rien.

    `quote_update_rate_60s` n'est calculée que si un état de carnet ANTÉRIEUR OU ÉGAL à la coupure a
    été retenu — l'historique de cotation n'est construit que dans ce cas. Sa présence prouve donc
    la sélection, sans dépendre d'un jeu d'essai assez riche pour produire un spread.
    """
    inc = moteur(flux_continu=True)
    alimenter(inc)
    calcul = inc.compute(INST, COUPURE)
    valeurs = dict(zip(calcul.vector.names, calcul.vector.values, strict=True))
    assert valeurs["quote_update_rate_60s"] is not None, (
        "aucun état de carnet retenu : le calcul aboutit en ne regardant plus le marché"
    )


def test_un_carnet_uniquement_posterieur_ne_sert_jamais_de_repli() -> None:
    """Quand TOUT le carnet est postérieur à la coupure, il n'y a rien à retenir — et c'est correct.

    Le calcul doit aboutir sans carnet plutôt que se rabattre sur le carnet vivant : ce repli serait
    précisément l'anticipation. Avant correction, cette situation levait `CausalityError` à la
    vérification de l'état construit ; s'en remettre au carnet vivant l'aurait fait « passer » au
    prix d'une fuite du futur.
    """
    inc = moteur(flux_continu=True)
    inc.ingest(echange(at=T0 + timedelta(seconds=40), seq=1, prix="100.5"))
    inc.ingest(carnet(at=APRES, seq=2, bid="900", ask="901"))
    calcul = inc.compute(INST, COUPURE)
    valeurs = dict(zip(calcul.vector.names, calcul.vector.values, strict=True))
    assert valeurs["quote_update_rate_60s"] is None, (
        "un carnet postérieur à la coupure a servi de repli : anticipation"
    )
    assert calcul.vector.cutoff_at == COUPURE, "le calcul doit aboutir sans carnet, pas échouer"
