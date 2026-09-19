"""Un taux de financement n'est « réglé » que si son échéance est passée.

Défaut observé en marche réelle : toutes les décisions échouaient sur
`CAUSALITY_VIOLATION`, avec le message « taux de funding réglé utilisé avant son heure de
règlement ». Le contrôle avait raison ; c'est la normalisation qui fabriquait l'anomalie.

Le flux d'OKX livre, dans un même message, l'échéance À VENIR (`fundingTime`, dans le futur) et un
taux déjà réglé de la période précédente (`settFundingRate`). Le normaliseur posait
`settled = bool(realized)` — donc vrai dès qu'un taux réglé figurait dans le message — tout en datant
l'enregistrement sur l'échéance à venir. Il produisait un « taux réglé » dont l'heure de règlement
n'était pas encore arrivée : une information fausse, pas une donnée manquante.

`settled` doit dire « CE taux, à CETTE échéance, a été réglé », et non « un taux réglé traîne dans
le message ».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from okxq.data.normalizer import Normalizer
from okxq.domain.clocks import SimulatedClock

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"


def normaliseur() -> Normalizer:
    return Normalizer(SimulatedClock(T0), source="test", id_factory=lambda n: f"evt_test_{n:04d}")


def message_funding(*, echeance: datetime, observe_a: datetime, taux_regle: str | None) -> dict:
    """Un message de la forme que pousse réellement OKX sur le canal `funding-rate`."""
    ligne = {
        "instId": INST,
        "fundingRate": "0.0001",
        "nextFundingRate": "0.00012",
        "fundingTime": str(int(echeance.timestamp() * 1000)),
        "nextFundingTime": str(int((echeance + timedelta(hours=8)).timestamp() * 1000)),
        "ts": str(int(observe_a.timestamp() * 1000)),
    }
    if taux_regle is not None:
        ligne["settFundingRate"] = taux_regle
    return {"arg": {"channel": "funding-rate", "instId": INST}, "data": [ligne]}


def test_une_echeance_a_venir_nest_jamais_reglee() -> None:
    """Le cas réel : OKX annonce la prochaine échéance ET un taux réglé dans le même message.

    Sans ce contrôle, l'enregistrement se présente comme un règlement futur — ce qui n'existe pas —
    et la plateforme refuse toutes ses décisions.
    """
    env = normaliseur().normalize_ws(
        message_funding(echeance=T0 + timedelta(hours=4), observe_a=T0, taux_regle="0.00008")
    )[0]
    assert env.event_type == "funding"
    assert env.payload["settled"] is False, (
        "une échéance dans le futur est annoncée comme réglée : la décision échouera en causalité"
    )
    assert env.payload["realized_rate"] is None, (
        "un taux réalisé est conservé sur une échéance non réglée : information fabriquée"
    )


def test_une_echeance_passee_avec_taux_realise_est_bien_reglee() -> None:
    """Contre-épreuve : ne jamais rien marquer réglé serait aussi faux, et viderait la feature.

    Sans ce test, poser `settled = False` en dur passerait le test précédent avec les honneurs.
    """
    env = normaliseur().normalize_ws(
        message_funding(echeance=T0 - timedelta(hours=4), observe_a=T0, taux_regle="0.00008")
    )[0]
    assert env.payload["settled"] is True, "une échéance passée avec taux réalisé doit être réglée"
    assert env.payload["realized_rate"] == "0.00008"


def test_une_echeance_passee_sans_taux_realise_reste_une_estimation() -> None:
    """Le temps écoulé ne suffit pas : il faut aussi que l'échange ait publié le taux réalisé."""
    env = normaliseur().normalize_ws(
        message_funding(echeance=T0 - timedelta(hours=4), observe_a=T0, taux_regle=None)
    )[0]
    assert env.payload["settled"] is False
    assert env.payload["realized_rate"] is None


def test_lecheance_et_le_taux_estime_restent_transmis_dans_tous_les_cas() -> None:
    """Refuser de conclure au règlement ne doit pas faire perdre l'information disponible.

    L'échéance à venir et le taux estimé sont des données légitimes : ce sont les seules qu'on ait
    avant le règlement, et le coût de financement s'estime avec elles.
    """
    env = normaliseur().normalize_ws(
        message_funding(echeance=T0 + timedelta(hours=4), observe_a=T0, taux_regle="0.00008")
    )[0]
    assert env.payload["funding_rate"] == "0.0001"
    assert env.payload["next_funding_rate"] == "0.00012"
    assert env.payload["funding_time_ms"] == str(int((T0 + timedelta(hours=4)).timestamp() * 1000))
