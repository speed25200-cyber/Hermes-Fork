"""Funding : règlements réellement traversés, et refus du taux final comme feature antérieure (T25, T26).

Le funding est le poste de coût le plus facile à comptabiliser de travers, pour deux raisons opposées.

**T25 — un paiement DISCRET.** Un règlement de funding n'est ni continu ni proratisable : il existe à
un instant, et il ne nous concerne QUE si la position était détenue à cet instant. Proratiser le
paiement sur toutes les minutes de l'horizon (ou le compter parce que le règlement « tombe dans la
journée ») fabrique des flux qui n'ont jamais eu lieu — et l'erreur est silencieuse, puisqu'elle
produit des montants du bon ordre de grandeur. La convention testée est §37 :
``cashflow = -signed_notional_at_settlement × realized_rate``, un long payant un taux positif.

**T26 — une FUITE TEMPORELLE.** Le taux réellement réglé n'est connu qu'après le règlement. L'employer
comme feature d'une décision ANTÉRIEURE, c'est faire prédire au modèle une quantité qu'il connaît
déjà : la recherche devient brillante et le live décevant, sans qu'aucun test ne rougisse. La
décision emploie le taux connu ou estimé À SON INSTANT, jamais le taux final futur.

Les tests sont hermétiques : aucune horloge système, aucune I/O. Les instants sont posés autour d'un
``T0`` fixe, et les montants sont des ``Decimal``. Une mesure absente est ``None``, jamais ``0``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from okxq.accounting.funding import (
    FundingObservation,
    detect_funding_lookahead,
    expected_funding_cost,
    funding_cashflow,
    funding_rate_known_at,
    parse_funding_event,
    settle_if_crossed,
    settlements_crossed,
    signed_notional_at_settlement,
)
from okxq.domain.errors import CausalityError, DataQualityError
from okxq.domain.events import CostBasis, EventEnvelope, Liquidity, QualityFlag
from okxq.domain.instruments import InstrumentSpec, InstrumentState
from okxq.domain.money import Side
from okxq.features.derivatives import compute_funding, latest_settled
from okxq.features.state import FundingRecord, PointInTimeMarketState
from okxq.portfolio.costs import CostComponent, CostModel, FeeSchedule

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INST = "TEST-USDT-SWAP"

# Règlements du contrat (lus dans les événements, jamais codés en dur comme « toutes les 8 heures »).
SETTLEMENT_PAST = T0 - timedelta(hours=4)
SETTLEMENT_NEXT = T0 + timedelta(hours=4)
SETTLEMENT_AFTER = T0 + timedelta(hours=12)

RATE_ESTIMATE = Decimal("0.0002")  # taux courant, connu avant le règlement
RATE_REALIZED_PAST = Decimal("0.0001")  # taux effectivement réglé au règlement passé
RATE_REALIZED_NEXT = Decimal("0.0009")  # taux FINAL du prochain règlement : inconnaissable à T0


def spec() -> InstrumentSpec:
    return InstrumentSpec(
        inst_id=INST,
        valid_from=T0 - timedelta(days=30),
        observed_at=T0,
        settle_ccy="USDT",
        base_ccy="TEST",
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=Decimal("0.001"),
        tick_size=Decimal("0.01"),
        lot_size=Decimal("1"),
        min_size=Decimal("1"),
        state=InstrumentState.LIVE,
        provenance="fixture:T25-T26",
    )


def observation(
    *,
    settled: bool,
    funding_time: datetime,
    available_at: datetime,
    funding_rate: Decimal = RATE_ESTIMATE,
    realized_rate: Decimal | None = None,
    next_funding_rate: Decimal | None = None,
    ingest_seq: int = 0,
) -> FundingObservation:
    return FundingObservation(
        inst_id=INST,
        funding_rate=funding_rate,
        next_funding_rate=next_funding_rate,
        funding_time=funding_time,
        next_funding_time=funding_time + timedelta(hours=8),
        settled=settled,
        realized_rate=realized_rate,
        available_at=available_at,
        receive_ts=available_at,
        ingest_seq=ingest_seq,
    )


def record(
    *,
    settled: bool,
    funding_time: datetime,
    available_at: datetime,
    funding_rate: float,
    realized_rate: float | None = None,
) -> FundingRecord:
    """Un événement ``funding`` côté FEATURES. Les features sont des statistiques : elles sont en float,
    contrairement aux montants comptables qui restent en ``Decimal``."""
    return FundingRecord(
        ts=available_at,
        available_at=available_at,
        funding_rate=funding_rate,
        next_funding_rate=None,
        funding_time=funding_time,
        next_funding_time=funding_time + timedelta(hours=8),
        settled=settled,
        realized_rate=realized_rate,
    )


# --- T25 : un flux uniquement aux règlements réellement traversés -------------------------------------


def test_T25_un_reglement_traverse_produit_le_flux_signe_exact() -> None:
    """Position longue détenue au règlement, taux positif ⇒ nous PAYONS : flux négatif, montant exact.

    L'assiette est validée ici aussi (``contrats × v × prix de référence``) : se tromper d'assiette
    produirait un flux du bon signe mais du mauvais ordre de grandeur, ce que seul un montant exact
    permet d'attraper.
    """
    notional = signed_notional_at_settlement(Decimal("10"), spec(), Decimal("10000"))
    assert notional == Decimal("100")  # 10 contrats × 0,001 unité × 10 000 USDT
    cashflow = funding_cashflow(notional, RATE_REALIZED_PAST, crossed=True)
    assert cashflow == -notional * RATE_REALIZED_PAST == Decimal("-0.01")
    assert cashflow < 0


def test_T25_un_reglement_non_traverse_ne_produit_aucun_flux() -> None:
    """Même règlement, même taux : sans traversée, le flux est nul — pas « petit », nul.

    C'est la moitié refusée du cas T25 ; sa contre-épreuve est le test précédent, avec exactement les
    mêmes entrées, pour qu'un moteur qui rendrait toujours zéro ne puisse pas passer les deux.
    """
    notional = signed_notional_at_settlement(Decimal("10"), spec(), Decimal("10000"))
    assert funding_cashflow(notional, RATE_REALIZED_PAST, crossed=False) == Decimal("0")


def test_T25_un_short_recoit_le_funding_quand_les_longs_paient() -> None:
    """Le signe suit la position, pas le taux : short + taux positif ⇒ flux POSITIF (encaissé).

    Sans ce cas, une erreur de signe sur la position resterait invisible sur un portefeuille
    uniquement long.
    """
    short = signed_notional_at_settlement(Decimal("-10"), spec(), Decimal("10000"))
    assert short == Decimal("-100")
    assert funding_cashflow(short, RATE_REALIZED_PAST, crossed=True) == Decimal("0.01") > 0


def test_T25_la_fenetre_de_detention_est_ouverte_a_gauche_et_fermee_a_droite() -> None:
    """« Réellement traversé » se décide à la borne : ouvrir À l'instant du règlement ne le traverse pas.

    Les deux bornes sont testées avec le même règlement : c'est la seule façon de distinguer une
    convention explicite d'un ``<=`` posé au hasard des deux côtés.
    """
    # Position ouverte pile à l'instant du règlement : pas traversé.
    assert settlements_crossed([T0], held_from=T0, held_until=T0 + timedelta(hours=1)) == []
    # Position clôturée pile à l'instant du règlement : traversé, la position y était.
    assert settlements_crossed([T0], held_from=T0 - timedelta(hours=1), held_until=T0) == [T0]


def test_T25_un_reglement_est_compte_une_seule_fois_meme_recu_en_double() -> None:
    """Le même instant de règlement listé deux fois reste UN règlement : pas de double comptage.

    La livraison des messages est « au moins une fois » (§52) : un règlement répété doit être
    dédupliqué par son instant, sinon un doublon de flux privé doublerait le coût.
    """
    crossed = settlements_crossed(
        [SETTLEMENT_PAST, SETTLEMENT_PAST], held_from=T0 - timedelta(hours=8), held_until=T0
    )
    assert crossed == [SETTLEMENT_PAST]


def test_T25_un_intervalle_de_detention_inverse_est_une_anomalie_declaree() -> None:
    """Détenir « de demain à hier » n'est pas un cas métier : c'est une incohérence, et elle se voit.

    Le refus est ici, sur l'intervalle lui-même. Le test suivant vérifie qu'une position ouverte APRÈS
    un règlement — cas licite — n'emprunte pas ce chemin d'erreur.
    """
    with pytest.raises(CausalityError):
        settlements_crossed([T0], held_from=T0 + timedelta(hours=1), held_until=T0)


def test_T25_une_position_ouverte_apres_le_reglement_ne_paie_rien_et_nest_pas_une_erreur() -> None:
    """Règlement réel, position ouverte APRÈS lui : aucun flux, et aucune exception.

    Ce cas arrive normalement, y compris quand un événement ancien est reçu tard (§52.2). Le rendre
    par une erreur de causalité obligerait l'appelant à attraper ``CausalityError`` pour une situation
    licite — et cette pêche large masquerait ensuite les VRAIES violations de causalité.
    """
    settled = observation(
        settled=True,
        funding_time=SETTLEMENT_PAST,
        available_at=SETTLEMENT_PAST,
        funding_rate=RATE_REALIZED_PAST,
        realized_rate=RATE_REALIZED_PAST,
    )
    assert (
        settle_if_crossed(
            settled,
            signed_contracts=Decimal("10"),
            spec=spec(),
            reference_price=Decimal("10000"),
            held_from=T0,  # ouverte 4 heures après le règlement
        )
        is None
    )


def test_T25_settle_if_crossed_ne_regle_que_ce_qui_est_traverse_et_detenu() -> None:
    """Le règlement complet : traversé et détenu ⇒ flux et clé d'idempotence ; sinon ``None``.

    Les trois refus (observation qui n'est qu'une estimation, position nulle, règlement non traversé)
    ont leur contre-épreuve dans la première assertion, sinon un moteur rendant toujours ``None``
    passerait le test.
    """
    common = {
        "signed_contracts": Decimal("10"),
        "spec": spec(),
        "reference_price": Decimal("10000"),
        "held_from": T0 - timedelta(hours=8),
    }
    settled = observation(
        settled=True,
        funding_time=SETTLEMENT_PAST,
        available_at=SETTLEMENT_PAST,
        funding_rate=RATE_REALIZED_PAST,
        realized_rate=RATE_REALIZED_PAST,
    )
    result = settle_if_crossed(settled, **common)  # type: ignore[arg-type]
    assert result is not None
    assert result.settlement_at == SETTLEMENT_PAST
    assert result.realized_rate == RATE_REALIZED_PAST
    assert result.cashflow == Decimal("-0.01")
    # La clé porte l'instant du règlement : rejouer l'événement ne crée pas un second flux.
    assert result.idempotency_key == settle_if_crossed(settled, **common).idempotency_key  # type: ignore[arg-type, union-attr]

    # Une ESTIMATION n'est pas un règlement : aucun flux, même si l'horizon la traverse.
    estimate = observation(settled=False, funding_time=SETTLEMENT_PAST, available_at=SETTLEMENT_PAST)
    assert settle_if_crossed(estimate, **common) is None  # type: ignore[arg-type]
    # Sans position, il n'y a pas d'assiette : aucun flux.
    assert (
        settle_if_crossed(
            settled,
            signed_contracts=Decimal("0"),
            spec=spec(),
            reference_price=Decimal("10000"),
            held_from=T0 - timedelta(hours=8),
        )
        is None
    )


def test_T25_un_paiement_discret_nest_jamais_proratise_sur_lhorizon() -> None:
    """Le coût attendu compte des RÈGLEMENTS, pas des minutes : allonger l'horizon sans en traverser
    un de plus ne change rien, et n'en traverser aucun rend zéro.

    Trois horizons sur le même calendrier de règlements : aucun traversé, un traversé, deux traversés.
    Un moteur qui proratiserait rendrait trois valeurs croissantes différentes et échouerait ici.
    """
    calendrier = [SETTLEMENT_PAST, SETTLEMENT_NEXT, SETTLEMENT_AFTER]
    notional = signed_notional_at_settlement(Decimal("10"), spec(), Decimal("10000"))

    aucun = expected_funding_cost(
        notional, RATE_ESTIMATE, calendrier, from_at=T0, until=T0 + timedelta(hours=3)
    )
    un = expected_funding_cost(notional, RATE_ESTIMATE, calendrier, from_at=T0, until=T0 + timedelta(hours=5))
    un_plus_long = expected_funding_cost(
        notional, RATE_ESTIMATE, calendrier, from_at=T0, until=T0 + timedelta(hours=11)
    )
    deux = expected_funding_cost(
        notional, RATE_ESTIMATE, calendrier, from_at=T0, until=T0 + timedelta(hours=13)
    )

    assert aucun == Decimal("0")
    assert un == -notional * RATE_ESTIMATE
    assert un_plus_long == un  # horizon deux fois plus long, même nombre de règlements
    assert deux == un * 2


def test_T25_le_modele_de_couts_ne_compte_le_funding_quaux_reglements_de_lhorizon() -> None:
    """Même règle dans l'estimation ex ante : ``settlements_in_horizon`` multiplie, il ne proratise pas.

    Un horizon d'une minute ne traverse normalement aucun règlement ; supposer le contraire inventerait
    un coût et ferait échouer des idées correctes.
    """
    model = CostModel(
        cost_basis=CostBasis.MID,
        fee_schedule=FeeSchedule(maker_rate=Decimal("0.0002"), taker_rate=Decimal("0.0005")),
    )
    common = {
        "side": Side.BUY,
        "contracts": Decimal("10"),
        "best_bid": Decimal("99.95"),
        "best_ask": Decimal("100.05"),
        "liquidity": Liquidity.TAKER,
        "funding_rate_estimate": RATE_ESTIMATE,
        "deduct": (CostComponent.FUNDING_EXPECTED,),
    }
    aucun = model.estimate(**common, settlements_in_horizon=0, position_sign=1)  # type: ignore[arg-type]
    deux = model.estimate(**common, settlements_in_horizon=2, position_sign=1)  # type: ignore[arg-type]
    short = model.estimate(**common, settlements_in_horizon=2, position_sign=-1)  # type: ignore[arg-type]

    assert aucun.components[CostComponent.FUNDING_EXPECTED] == Decimal("0")
    assert deux.components[CostComponent.FUNDING_EXPECTED] == -RATE_ESTIMATE * 2
    # Un short encaisse le même funding : gain positif, et le stress ne l'amplifie pas.
    assert short.components[CostComponent.FUNDING_EXPECTED] == RATE_ESTIMATE * 2 > 0


def test_T25_un_reglement_annonce_sans_taux_realise_est_refuse() -> None:
    """``settled: true`` sans ``realized_rate`` est une donnée incomplète : refus, pas de taux supposé.

    Reprendre l'estimation à la place du taux réglé inventerait un montant comptable. La contre-épreuve
    (événement complet) suit immédiatement.
    """

    def envelope(payload: dict[str, object]) -> EventEnvelope:
        return EventEnvelope(
            event_id="ev_funding",
            event_type="funding",
            schema_version=1,
            source="fixture",
            exchange_ts=SETTLEMENT_PAST,
            receive_ts=SETTLEMENT_PAST,
            available_at=SETTLEMENT_PAST,
            ingest_seq=1,
            payload_hash="h" * 8,
            payload=payload,
        )

    base: dict[str, object] = {
        "inst_id": INST,
        "funding_rate": "0.0001",
        "funding_time_ms": int(SETTLEMENT_PAST.timestamp() * 1000),
        "next_funding_time_ms": int(SETTLEMENT_NEXT.timestamp() * 1000),
        "settled": True,
    }
    with pytest.raises(DataQualityError):
        parse_funding_event(envelope(base))
    complet = parse_funding_event(envelope({**base, "realized_rate": "0.0001"}))
    assert complet.realized_rate == RATE_REALIZED_PAST
    assert complet.is_estimate is False


# --- T26 : le taux final ne peut pas servir de feature antérieure -------------------------------------


def test_T26_le_taux_final_employe_comme_feature_anterieure_est_detecte_comme_fuite() -> None:
    """Le taux réglé à T0+4h employé à T0 : fuite détectée.

    Ce test échoue si quelqu'un réintroduit la fuite en rendant « connaissable » un taux publié après
    la coupure : ``detect_funding_lookahead`` doit rester ``True`` sur ce cas précis.
    """
    connue_avant = observation(
        settled=False,
        funding_time=SETTLEMENT_NEXT,
        available_at=T0 - timedelta(minutes=5),
        funding_rate=RATE_ESTIMATE,
        ingest_seq=1,
    )
    finale_apres = observation(
        settled=True,
        funding_time=SETTLEMENT_NEXT,
        available_at=SETTLEMENT_NEXT,
        funding_rate=RATE_REALIZED_NEXT,
        realized_rate=RATE_REALIZED_NEXT,
        ingest_seq=2,
    )
    observations = [connue_avant, finale_apres]
    assert detect_funding_lookahead(observations, INST, T0, RATE_REALIZED_NEXT) is True


def test_T26_un_taux_connaissable_a_la_coupure_nest_pas_une_fuite() -> None:
    """CONTRE-ÉPREUVE : l'estimation disponible avant la coupure est un usage LÉGITIME.

    Sans ce cas, un détecteur qui crierait « fuite » sur tout taux passerait le test précédent tout en
    interdisant la seule feature de funding utilisable à la décision.
    """
    connue_avant = observation(
        settled=False,
        funding_time=SETTLEMENT_NEXT,
        available_at=T0 - timedelta(minutes=5),
        funding_rate=RATE_ESTIMATE,
        ingest_seq=1,
    )
    finale_apres = observation(
        settled=True,
        funding_time=SETTLEMENT_NEXT,
        available_at=SETTLEMENT_NEXT,
        funding_rate=RATE_REALIZED_NEXT,
        realized_rate=RATE_REALIZED_NEXT,
        ingest_seq=2,
    )
    assert detect_funding_lookahead([connue_avant, finale_apres], INST, T0, RATE_ESTIMATE) is False
    # Un taux déjà RÉGLÉ dans le passé est également connaissable : ce n'est pas une fuite.
    reglee_avant = observation(
        settled=True,
        funding_time=SETTLEMENT_PAST,
        available_at=SETTLEMENT_PAST,
        funding_rate=RATE_REALIZED_PAST,
        realized_rate=RATE_REALIZED_PAST,
    )
    assert detect_funding_lookahead([reglee_avant], INST, T0, RATE_REALIZED_PAST) is False


def test_T26_la_decision_emploie_le_taux_connu_a_son_instant_et_le_signale_sinon() -> None:
    """Bout en bout : le coût attendu se calcule avec le taux LU au cutoff, et l'autre choix est flagué.

    On construit le coût de deux façons : avec le taux connu à la décision (légitime, non flagué) et
    avec le taux final futur (flagué comme fuite). Les deux montants diffèrent — c'est précisément
    l'écart qu'une fuite ferait passer pour de la performance.
    """
    connue_avant = observation(
        settled=False,
        funding_time=SETTLEMENT_NEXT,
        available_at=T0 - timedelta(minutes=5),
        funding_rate=RATE_ESTIMATE,
        ingest_seq=1,
    )
    finale_apres = observation(
        settled=True,
        funding_time=SETTLEMENT_NEXT,
        available_at=SETTLEMENT_NEXT,
        funding_rate=RATE_REALIZED_NEXT,
        realized_rate=RATE_REALIZED_NEXT,
        ingest_seq=2,
    )
    observations = [connue_avant, finale_apres]

    lue = funding_rate_known_at(observations, INST, T0)
    assert lue is not None
    assert lue.funding_rate == RATE_ESTIMATE
    assert lue.is_estimate is True

    notional = signed_notional_at_settlement(Decimal("10"), spec(), Decimal("10000"))
    honnete = expected_funding_cost(
        notional, lue.funding_rate, [SETTLEMENT_NEXT], from_at=T0, until=T0 + timedelta(hours=5)
    )
    fuite = expected_funding_cost(
        notional, RATE_REALIZED_NEXT, [SETTLEMENT_NEXT], from_at=T0, until=T0 + timedelta(hours=5)
    )
    assert honnete == -notional * RATE_ESTIMATE
    assert fuite != honnete
    assert detect_funding_lookahead(observations, INST, T0, lue.funding_rate) is False
    assert detect_funding_lookahead(observations, INST, T0, RATE_REALIZED_NEXT) is True


def test_T26_aucune_observation_disponible_a_la_coupure_rend_none_et_jamais_un_taux() -> None:
    """Absence de mesure ⇒ ``None``. Rendre 0 ferait croire à un funding nul, ce qui est une mesure.

    La contre-épreuve est dans le test précédent : quand une observation est disponible, elle est bien
    rendue.
    """
    finale_apres = observation(
        settled=True,
        funding_time=SETTLEMENT_NEXT,
        available_at=SETTLEMENT_NEXT,
        funding_rate=RATE_REALIZED_NEXT,
        realized_rate=RATE_REALIZED_NEXT,
    )
    assert funding_rate_known_at([finale_apres], INST, T0) is None
    assert funding_rate_known_at([], INST, T0) is None
    # Un autre instrument ne répond jamais pour celui-ci.
    autre = observation(settled=False, funding_time=SETTLEMENT_NEXT, available_at=T0 - timedelta(minutes=1))
    assert funding_rate_known_at([autre], "AUTRE-USDT-SWAP", T0) is None


def test_T26_une_feature_de_funding_reglee_apres_la_coupure_est_refusee() -> None:
    """Le moteur de features refuse un taux RÉGLÉ dont le règlement est postérieur à la coupure.

    C'est la fuite sous sa forme la plus tentante : l'événement « réglé » existe dans l'historique, il
    porte le bon instrument, et rien n'empêche de le lire — sauf que son règlement n'a pas encore eu
    lieu à la coupure.

    La fuite a DEUX formes, et chacune est vérifiée séparément pour que le test échoue si l'un ou
    l'autre des garde-fous est retiré :

    (a) le règlement est publié à son heure, postérieure à la coupure — seul le contrôle
        « ``funding_time`` > coupure » l'attrape ;
    (b) le règlement est ANTIDATÉ — un règlement PASSÉ, donc admissible à la coupure, mais déclaré
        disponible AVANT d'avoir eu lieu, ce qu'un backfill produit facilement — et seul le contrôle
        « disponible avant son règlement » l'attrape.

    Un état point-in-time valide n'admet que la forme (b), puisqu'il refuse déjà toute donnée
    disponible après la coupure ; la forme (a) est donc vérifiée directement sur ``latest_settled``.
    """
    publie_a_son_heure = record(
        settled=True,
        funding_time=SETTLEMENT_NEXT,
        available_at=SETTLEMENT_NEXT,  # publié quand le règlement a lieu : après la coupure
        funding_rate=float(RATE_REALIZED_NEXT),
        realized_rate=float(RATE_REALIZED_NEXT),
    )
    with pytest.raises(CausalityError):
        latest_settled([publie_a_son_heure], T0.timestamp())

    antidate = record(
        settled=True,
        funding_time=SETTLEMENT_PAST,
        available_at=SETTLEMENT_PAST - timedelta(hours=1),  # connu une heure avant son propre règlement
        funding_rate=float(RATE_REALIZED_PAST),
        realized_rate=float(RATE_REALIZED_PAST),
    )
    with pytest.raises(CausalityError):
        latest_settled([antidate], T0.timestamp())
    # Et par le moteur de features complet, sur un état point-in-time par ailleurs valide.
    state = PointInTimeMarketState(instrument=INST, cutoff_at=T0, funding=[antidate])
    with pytest.raises(CausalityError):
        compute_funding(state)


def test_T26_une_observation_de_funding_disponible_apres_la_coupure_est_refusee() -> None:
    """L'autre forme de la fuite : un événement dont la DISPONIBILITÉ est postérieure à la coupure.

    Reconstituer un état point-in-time en y versant des événements arrivés plus tard est la manière la
    plus courante de fabriquer une fuite lors d'un backfill. L'état lui-même refuse.
    """
    arrivee_tardive = record(
        settled=False,
        funding_time=SETTLEMENT_NEXT,
        available_at=T0 + timedelta(minutes=1),
        funding_rate=float(RATE_ESTIMATE),
    )
    with pytest.raises(CausalityError):
        PointInTimeMarketState(instrument=INST, cutoff_at=T0, funding=[arrivee_tardive])


def test_T26_le_dernier_taux_regle_est_celui_du_passe_et_non_une_estimation_a_venir() -> None:
    """CONTRE-ÉPREUVE des deux refus : l'état légitime produit bien les features, avec les bonnes valeurs.

    Le règlement passé (0,0001) et l'estimation courante du prochain règlement (0,0002) coexistent.
    ``funding_rate_settled_last`` doit valoir le taux RÉGLÉ du passé : si quelqu'un le faisait pointer
    vers le taux courant — ou, pire, vers le taux final du règlement à venir — cette assertion tombe.
    """
    reglee_passee = record(
        settled=True,
        funding_time=SETTLEMENT_PAST,
        available_at=SETTLEMENT_PAST,
        funding_rate=float(RATE_REALIZED_PAST),
        realized_rate=float(RATE_REALIZED_PAST),
    )
    estimation = record(
        settled=False,
        funding_time=SETTLEMENT_NEXT,
        available_at=T0 - timedelta(minutes=1),
        funding_rate=float(RATE_ESTIMATE),
    )
    state = PointInTimeMarketState(instrument=INST, cutoff_at=T0, funding=[reglee_passee, estimation])
    out = compute_funding(state)
    assert out["funding_rate_estimate"].value == pytest.approx(float(RATE_ESTIMATE))
    assert out["funding_rate_settled_last"].value == pytest.approx(float(RATE_REALIZED_PAST))
    assert out["funding_rate_settled_last"].value != pytest.approx(float(RATE_REALIZED_NEXT))
    assert out["funding_rate_change"].value == pytest.approx(float(RATE_ESTIMATE - RATE_REALIZED_PAST))
    assert all(r.flag is QualityFlag.OK for r in out.values())


def test_T26_sans_reglement_connu_la_feature_est_manquante_et_jamais_nulle() -> None:
    """Aucun règlement connu à la coupure ⇒ MISSING motivé, valeur ``None``.

    Imputer 0 (ou l'estimation) ferait croire à un funding réglé nul, et l'écart
    ``estimation − dernier réglé`` deviendrait une mesure inventée.
    """
    estimation = record(
        settled=False,
        funding_time=SETTLEMENT_NEXT,
        available_at=T0 - timedelta(minutes=1),
        funding_rate=float(RATE_ESTIMATE),
    )
    out = compute_funding(PointInTimeMarketState(instrument=INST, cutoff_at=T0, funding=[estimation]))
    assert out["funding_rate_settled_last"].flag is QualityFlag.MISSING
    assert out["funding_rate_settled_last"].value is None
    assert out["funding_rate_settled_last"].reason == "no_settled_funding"
    assert out["funding_rate_change"].value is None
    # Contre-épreuve : l'estimation, elle, est bien disponible.
    assert out["funding_rate_estimate"].flag is QualityFlag.OK

    # Aucun événement du tout : toutes les features de funding sont MISSING, aucune n'est zéro.
    vide = compute_funding(PointInTimeMarketState(instrument=INST, cutoff_at=T0, funding=[]))
    assert {r.flag for r in vide.values()} == {QualityFlag.MISSING}
    assert all(r.value is None for r in vide.values())
