"""Frais, rebates et absence de double comptage (T23, T24, §37).

Pourquoi ces cas comptent : un modèle de coûts qui se trompe de SIGNE ne se voit pas. Il produit des
nombres plausibles, la suite reste verte, et l'erreur n'apparaît qu'en argent réel. Deux fautes
classiques sont attrapées ici.

1. ``abs()`` sur une commission (T23). Un ``abs()`` transforme un rebate maker — de l'argent
   ENCAISSÉ — en coût, et rend un aller-retour maker artificiellement cher. Une liquidité INCONNUE,
   symétriquement, ne doit jamais être supposée favorable : c'est l'hypothèse la plus coûteuse qui
   s'applique, sinon on décide sur un coût qu'on n'a pas mesuré.
2. Le spread déduit deux fois (T24). En convention EXECUTABLE, le prix d'entrée/sortie CONTIENT déjà
   le spread et la consommation de profondeur ; le redéduire fabrique un coût qui n'existe pas,
   rejette des idées rentables, et cette perte-là est invisible dans un backtest.

Conventions vérifiées ici, telles que §37 les fixe :

- ``fee_cashflow`` est un FLUX : négatif = débit (nous payons), positif = crédit (nous sommes payés) ;
- ``fee_cost = -fee_cashflow`` est le coût de DÉCISION : positif = cher, négatif = gain ;
- ``CostModel`` rend des composantes SIGNÉES en fraction du notionnel : négatif = coût, positif = gain ;
- ``EdgeBuilder`` (§37, D4) attend des MAGNITUDES de coût positives, et ne déduit jamais une
  composante déclarée ``included_in_price``.

Tout montant monétaire est un ``Decimal``. Les seuls flottants présents sont ``gross_mu`` et
``uncertainty``, qui sont des sorties de modèle (des statistiques), pas des montants.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from okxq.backtest.market_state import BookView
from okxq.domain.errors import CostModelError, DoubleCountingError, UnitError
from okxq.domain.events import CostBasis, Forecast, Liquidity, PositionSide
from okxq.domain.instruments import InstrumentSpec, InstrumentState
from okxq.domain.money import Money, Side
from okxq.portfolio.costs import (
    ALL_COMPONENTS,
    CostComponent,
    CostModel,
    FeeSchedule,
    StressMultipliers,
    fee_cashflow,
    walk_levels,
)
from okxq.portfolio.forecasts import EdgeBuilder, check_cost_basis_consistency
from okxq.runtime.composition import BookCostComponents

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INST = "TEST-USDT-SWAP"

# Carnet choisi pour que les fractions soient EXACTES en Decimal : mid = 100, demi-spread = 0,05,
# soit un spread de 0,0005 du notionnel. Un carnet « joli » n'affaiblit rien ici : ce qui est testé
# est le signe et l'unicité de la déduction, pas la précision d'une division.
BEST_BID = Decimal("99.95")
BEST_ASK = Decimal("100.05")
MID = Decimal("100.00")
HALF_SPREAD_FRACTION = Decimal("0.0005")

NOTIONAL = Money(Decimal("10000"), "USDT")
MAKER_RATE = Decimal("0.0002")
TAKER_RATE = Decimal("0.0005")
REBATE_RATE = Decimal("-0.00005")  # taux maker NÉGATIF : le maker est payé


def schedule(maker: Decimal = MAKER_RATE, taker: Decimal = TAKER_RATE) -> FeeSchedule:
    return FeeSchedule(maker_rate=maker, taker_rate=taker)


def spec() -> InstrumentSpec:
    return InstrumentSpec(
        inst_id=INST,
        valid_from=T0 - timedelta(days=1),
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
        provenance="fixture:T23-T24",
    )


def book(*, deep: bool = True) -> BookView:
    """Carnet à deux côtés. ``deep`` : L1 assez épais pour que la marche ne coûte rien."""
    asks = (
        ((BEST_ASK, Decimal("1000")),)
        if deep
        else ((BEST_ASK, Decimal("4")), (Decimal("100.15"), Decimal("10")))
    )
    return BookView(INST, T0, 1, True, ((BEST_BID, Decimal("1000")),), asks)


def model(
    basis: CostBasis, *, fees: FeeSchedule | None = None, stress: StressMultipliers | None = None
) -> CostModel:
    return CostModel(
        cost_basis=basis,
        fee_schedule=fees or schedule(),
        stress=stress or StressMultipliers(),
    )


def forecast(basis: CostBasis, *, gross_mu: float = 0.001) -> Forecast:
    return Forecast(
        forecast_id="fc_T24",
        model_id="mdl_T24",
        snapshot_id="snap_T24",
        instrument=INST,
        horizon_s=60,
        gross_mu=gross_mu,
        uncertainty=0.0,  # l'incertitude n'est pas le sujet : on isole l'arithmétique des coûts
        execution_policy_id="pol_T24",
        cost_basis=basis,
        available_at=T0,
    )


def production_components(basis: CostBasis) -> BookCostComponents:
    """L'adaptateur RÉELLEMENT câblé dans la boucle : c'est lui qui doit tenir la promesse T24."""
    view = book()
    return BookCostComponents(
        model=model(basis),
        books=lambda: {INST: view},
        specs=lambda: {INST: spec()},
        horizon_s=60,
    )


# --- T23 : frais maker/taker et rebate, signes et montants exacts -------------------------------------


def test_T23_les_frais_maker_et_taker_sont_des_debits_du_montant_exact() -> None:
    """Payer des frais est un DÉBIT : flux négatif, montant exact, devise portée par le flux."""
    fees = schedule()
    taker = fee_cashflow(NOTIONAL, Liquidity.TAKER, fees)
    maker = fee_cashflow(NOTIONAL, Liquidity.MAKER, fees)
    assert taker == Money(Decimal("-5"), "USDT")
    assert maker == Money(Decimal("-2"), "USDT")
    # Un taker paie strictement plus qu'un maker : l'ordre des deux taux ne doit jamais s'inverser.
    assert taker.amount < maker.amount < Decimal(0)
    assert taker.ccy == maker.ccy == "USDT"


def test_T23_un_taux_maker_negatif_est_un_rebate_encaisse_jamais_efface_par_abs() -> None:
    """Un rebate est de l'argent REÇU : le flux est positif (crédit) et ``abs()`` le détruirait.

    C'est le cas que §37 interdit explicitement de « normaliser » avec ``abs()``. Le test compare au
    montant qu'un ``abs(taux)`` aurait produit : si quelqu'un réintroduit ce raccourci, l'égalité
    attendue devient celle du débit et le test échoue.
    """
    fees = schedule(maker=REBATE_RATE)
    flow = fee_cashflow(NOTIONAL, Liquidity.MAKER, fees)
    assert flow == Money(Decimal("0.5"), "USDT")
    assert flow.amount > 0
    # Ce qu'un abs() aurait rendu — et qui doit rester faux.
    montant_si_abs = -abs(NOTIONAL.amount) * abs(REBATE_RATE)
    assert flow.amount != montant_si_abs


def test_T23_le_cout_de_decision_est_loppose_du_flux_de_commission() -> None:
    """§37 : ``fee_cost = -fee_cashflow``. Un rebate devient donc un coût NÉGATIF, c'est-à-dire un gain.

    L'intérêt du cas : un moteur qui plafonnerait le coût à zéro « par prudence » ferait disparaître
    le rebate de la décision, et on ne saurait plus pourquoi une politique maker semble non rentable.
    """
    debit = fee_cashflow(NOTIONAL, Liquidity.TAKER, schedule())
    credit = fee_cashflow(NOTIONAL, Liquidity.MAKER, schedule(maker=REBATE_RATE))
    assert -debit.amount == Decimal("5")  # coût positif : cher
    assert -credit.amount == Decimal("-0.5")  # coût négatif : gain


def test_T23_une_liquidite_inconnue_prend_lhypothese_la_plus_couteuse() -> None:
    """Liquidité non mesurée ⇒ on suppose le taux le plus cher, dans les DEUX sens de l'asymétrie.

    Une absence de mesure ne se remplace pas par le cas favorable. Le second barème (maker plus cher
    que taker) évite que le test passe seulement parce que « inconnu » serait codé en dur sur taker.
    """
    taker_plus_cher = schedule()
    assert taker_plus_cher.rate(Liquidity.UNKNOWN) == TAKER_RATE
    maker_plus_cher = schedule(maker=Decimal("0.0008"))
    assert maker_plus_cher.rate(Liquidity.UNKNOWN) == Decimal("0.0008")


def test_T23_une_liquidite_inconnue_ne_suppose_jamais_un_rebate() -> None:
    """Contre-épreuve du rebate : il n'est encaissé QUE si l'on sait avoir été maker.

    Supposer le rebate sur une liquidité inconnue inventerait un revenu ; ici le flux reste un débit
    au taux taker, et le rebate n'apparaît que pour ``Liquidity.MAKER``.
    """
    fees = schedule(maker=REBATE_RATE)
    assert fee_cashflow(NOTIONAL, Liquidity.UNKNOWN, fees) == Money(Decimal("-5"), "USDT")
    assert fee_cashflow(NOTIONAL, Liquidity.MAKER, fees).amount > 0


def test_T23_la_devise_des_frais_doit_etre_celle_du_notionnel() -> None:
    """« Montants exacts » n'a aucun sens sans la devise : un notionnel USD ne paie pas des frais USDT."""
    fees = schedule()
    with pytest.raises(CostModelError):
        fee_cashflow(Money(Decimal("10000"), "USD"), Liquidity.TAKER, fees)
    # Contre-épreuve : la même somme dans la devise des frais passe et garde cette devise.
    assert fee_cashflow(NOTIONAL, Liquidity.TAKER, fees).ccy == fees.fee_ccy


def test_T23_un_taux_taker_negatif_nest_pas_un_cas_supporte() -> None:
    """Être payé pour traverser le carnet n'est pas un barème que nous savons modéliser : refus explicite.

    Contre-épreuve juste après : un taux taker nul (barème promotionnel) reste acceptable.
    """
    with pytest.raises(CostModelError):
        FeeSchedule(maker_rate=MAKER_RATE, taker_rate=Decimal("-0.0001"))
    assert FeeSchedule(maker_rate=REBATE_RATE, taker_rate=Decimal("0")).taker_rate == Decimal("0")


def test_T23_le_composant_de_frais_du_modele_garde_le_signe_du_bareme() -> None:
    """Dans l'estimation, un coût est négatif et un rebate positif — sans exception pour les frais."""
    paid = model(CostBasis.MID).estimate(
        side=Side.BUY,
        contracts=Decimal("10"),
        best_bid=BEST_BID,
        best_ask=BEST_ASK,
        liquidity=Liquidity.TAKER,
        deduct=(CostComponent.FEES,),
    )
    assert paid.components[CostComponent.FEES] == -TAKER_RATE
    earned = model(CostBasis.MID, fees=schedule(maker=REBATE_RATE)).estimate(
        side=Side.BUY,
        contracts=Decimal("10"),
        best_bid=BEST_BID,
        best_ask=BEST_ASK,
        liquidity=Liquidity.MAKER,
        deduct=(CostComponent.FEES,),
    )
    assert earned.components[CostComponent.FEES] == -REBATE_RATE == Decimal("0.00005")
    # Converti en argent, le rebate est un crédit sur le notionnel.
    assert earned.money(NOTIONAL) == Money(Decimal("0.5"), "USDT")


def test_T23_le_stress_alourdit_les_couts_et_najoute_jamais_au_rebate() -> None:
    """Un scénario dégradé rend les coûts pires ; il ne doit pas rendre les GAINS meilleurs.

    Amplifier un rebate par un multiplicateur de stress construirait un scénario « dégradé » plus
    rentable que la réalité — exactement l'inverse de ce qu'un stress est censé démontrer.
    """
    stress = StressMultipliers(fees=Decimal("3"))
    paid = model(CostBasis.MID, stress=stress).estimate(
        side=Side.BUY,
        contracts=Decimal("10"),
        best_bid=BEST_BID,
        best_ask=BEST_ASK,
        liquidity=Liquidity.TAKER,
        deduct=(CostComponent.FEES,),
    )
    assert paid.components[CostComponent.FEES] == -TAKER_RATE * 3
    earned = model(CostBasis.MID, fees=schedule(maker=REBATE_RATE), stress=stress).estimate(
        side=Side.BUY,
        contracts=Decimal("10"),
        best_bid=BEST_BID,
        best_ask=BEST_ASK,
        liquidity=Liquidity.MAKER,
        deduct=(CostComponent.FEES,),
    )
    assert earned.components[CostComponent.FEES] == -REBATE_RATE  # inchangé


def test_T23_un_aller_retour_a_taille_nulle_sur_marche_plat_coute_de_largent() -> None:
    """§37 : sans rebate ni funding favorable, faire un aller-retour DOIT coûter de l'argent.

    Marché plat et taille nulle : plus de spread, plus de profondeur, plus d'impact. Il ne reste que
    les commissions des deux jambes. Si le total sortait nul ou positif, un signe aurait été perdu
    quelque part et le backtest rendrait le churn gratuit.
    """
    flat = Decimal("100")
    rt = model(CostBasis.MID).round_trip(
        side=Side.BUY,
        contracts=Decimal("0"),
        best_bid=flat,
        best_ask=flat,
        entry_liquidity=Liquidity.TAKER,
        exit_liquidity=Liquidity.TAKER,
    )
    assert rt.components[CostComponent.FEES] == -TAKER_RATE * 2
    assert rt.components[CostComponent.SPREAD] == Decimal("0")
    assert rt.components[CostComponent.FUNDING_EXPECTED] == Decimal("0")
    assert rt.total == -TAKER_RATE * 2 < Decimal(0)


# --- T24 : prix exécutables et spread déduit une seconde fois ------------------------------------------


def test_T24_le_spread_contenu_dans_le_prix_ne_peut_etre_deduit_une_seconde_fois() -> None:
    """Le cas à attraper : prix EXECUTABLE (spread déjà dedans) ET spread redéduit.

    Ce n'est pas une simple imprécision : le coût fabriqué est du même ordre que l'edge cherché, donc
    l'erreur ne se voit que par le refus d'idées qui étaient rentables. On exige un refus TYPÉ et
    nommant la composante fautive, pas un total silencieusement plus mauvais.
    """
    executable = model(CostBasis.EXECUTABLE)
    with pytest.raises(DoubleCountingError) as err:
        executable.estimate(
            side=Side.BUY,
            contracts=Decimal("10"),
            best_bid=BEST_BID,
            best_ask=BEST_ASK,
            included_in_price=("spread",),
            deduct=(CostComponent.FEES, CostComponent.SPREAD),
        )
    assert err.value.context["components"] == ["spread"]
    assert err.value.context["cost_basis"] == CostBasis.EXECUTABLE.value
    # Le défaut est aussi attrapé quand `deduct` n'est pas précisé : le défaut déduit TOUT.
    with pytest.raises(DoubleCountingError):
        executable.estimate(side=Side.BUY, contracts=Decimal("10"), best_bid=BEST_BID, best_ask=BEST_ASK)


def test_T24_la_convention_executable_deduit_les_couts_non_contenus_dans_le_prix() -> None:
    """CONTRE-ÉPREUVE : le cas admissible doit passer, sinon le refus ci-dessus ne prouve rien.

    Un moteur qui refuserait TOUTE estimation EXECUTABLE passerait le test de refus tout en étant
    inutilisable. Ici les coûts non inclus (frais, profondeur, funding, impact) sont bien déduits,
    le spread est déclaré inclus et absent des composantes, et le prix de référence est le prix
    TOUCHABLE et non le milieu.
    """
    estimate = model(CostBasis.EXECUTABLE).estimate(
        side=Side.BUY,
        contracts=Decimal("10"),
        best_bid=BEST_BID,
        best_ask=BEST_ASK,
        opposite_levels=book().asks,
        liquidity=Liquidity.TAKER,
        deduct=tuple(c for c in ALL_COMPONENTS if c is not CostComponent.SPREAD),
    )
    assert estimate.included_in_price == ("spread",)
    assert CostComponent.SPREAD not in estimate.components
    assert estimate.components[CostComponent.FEES] == -TAKER_RATE
    assert estimate.reference_price == BEST_ASK
    # La profondeur du L1 suffit : aucune marche, donc aucun coût de profondeur inventé.
    assert estimate.components[CostComponent.SLIPPAGE] == Decimal("0")
    assert estimate.depth_shortfall_contracts == Decimal("0")
    assert estimate.total == -TAKER_RATE


def test_T24_la_convention_executable_doit_declarer_le_spread_comme_inclus() -> None:
    """Ne rien déclarer inclus alors que le prix contient le spread est l'autre face du double comptage.

    Le refus est ici un ``CostModelError`` de configuration (et non un ``DoubleCountingError``) :
    l'incohérence est dans la DÉCLARATION, avant tout calcul.
    """
    with pytest.raises(CostModelError) as err:
        model(CostBasis.EXECUTABLE).estimate(
            side=Side.BUY,
            contracts=Decimal("10"),
            best_bid=BEST_BID,
            best_ask=BEST_ASK,
            included_in_price=(),
            deduct=(CostComponent.FEES,),
        )
    assert not isinstance(err.value, DoubleCountingError)


def test_T24_la_convention_mid_deduit_le_demi_spread_exactement_une_fois() -> None:
    """CONTRE-ÉPREUVE côté MID : là, le spread N'EST PAS dans le prix, donc il se déduit — une fois.

    Le montant est exact et la référence est le milieu. Comparé au cas EXECUTABLE ci-dessus, l'écart
    de total vaut exactement le demi-spread : c'est la preuve chiffrée qu'une seule des deux
    conventions le porte.
    """
    mid = model(CostBasis.MID).estimate(
        side=Side.BUY,
        contracts=Decimal("10"),
        best_bid=BEST_BID,
        best_ask=BEST_ASK,
        opposite_levels=book().asks,
        liquidity=Liquidity.TAKER,
    )
    assert mid.included_in_price == ()
    assert mid.reference_price == MID
    assert mid.components[CostComponent.SPREAD] == -HALF_SPREAD_FRACTION
    assert mid.total == -(TAKER_RATE + HALF_SPREAD_FRACTION)
    executable_total = -TAKER_RATE
    assert executable_total - mid.total == HALF_SPREAD_FRACTION


def test_T24_un_maker_ne_paie_pas_le_demi_spread_et_ne_lencaisse_pas_ex_ante() -> None:
    """Un maker ne traverse pas le spread : ex ante, sa composante spread est nulle, jamais un gain.

    Compter le demi-spread comme un revenu maker supposerait le fill, ce qu'aucun ordre passif ne
    garantit (T27, T28) ; le compter comme un coût le ferait payer deux fois — une fois dans le prix
    passif, une fois dans la déduction.
    """
    passive = model(CostBasis.MID).estimate(
        side=Side.BUY,
        contracts=Decimal("10"),
        best_bid=BEST_BID,
        best_ask=BEST_ASK,
        opposite_levels=book().asks,
        liquidity=Liquidity.MAKER,
    )
    assert passive.components[CostComponent.SPREAD] == Decimal("0")
    assert passive.components[CostComponent.SLIPPAGE] == Decimal("0")


def test_T24_la_profondeur_consommee_reste_un_poste_distinct_du_spread() -> None:
    """Le spread s'arrête au prix touchable ; la marche dans la profondeur commence là. Aucun recouvrement.

    Sans cette frontière, la profondeur serait comptée dans le spread (ou l'inverse) et un carnet
    mince paierait deux fois le même écart de prix.
    """
    shallow = book(deep=False)
    walk = walk_levels(shallow.asks, Decimal("10"))
    assert walk.vwap == Decimal("100.11")
    estimate = model(CostBasis.MID).estimate(
        side=Side.BUY,
        contracts=Decimal("10"),
        best_bid=BEST_BID,
        best_ask=BEST_ASK,
        opposite_levels=shallow.asks,
        liquidity=Liquidity.TAKER,
    )
    # Le spread couvre mid → touchable ; la profondeur couvre touchable → VWAP. Segments disjoints.
    assert estimate.components[CostComponent.SPREAD] == -HALF_SPREAD_FRACTION
    assert estimate.components[CostComponent.SLIPPAGE] == -(walk.vwap - BEST_ASK) / MID


def test_T24_une_quantite_que_la_profondeur_ne_couvre_pas_est_rapportee_et_non_imputee() -> None:
    """Une absence de mesure est signalée, jamais remplacée par zéro : le reliquat non couvert est rendu.

    Sans cette remontée, un carnet trop mince rendrait un coût « complet » et laisserait croire que
    la taille est exécutable.
    """
    walk = walk_levels(book(deep=False).asks, Decimal("20"))
    assert walk.shortfall_contracts == Decimal("6")
    assert walk_levels((), Decimal("5")).vwap is None  # aucun prix mesuré ⇒ None, pas 0


def test_T24_une_prevision_mid_ne_peut_rien_declarer_comme_inclus_dans_le_prix() -> None:
    """``check_cost_basis_consistency`` : MID n'inclut rien, EXECUTABLE inclut au moins le spread.

    Les deux refus ET leurs deux contre-épreuves sont ici : un moteur qui refuserait les quatre
    combinaisons échouerait sur les deux dernières assertions.
    """
    with pytest.raises(UnitError):
        check_cost_basis_consistency(CostBasis.MID, ["spread"])
    with pytest.raises(UnitError):
        check_cost_basis_consistency(CostBasis.EXECUTABLE, [])
    with pytest.raises(UnitError):
        check_cost_basis_consistency(CostBasis.EXECUTABLE, ["spread", "demi_spread_inconnu"])
    assert check_cost_basis_consistency(CostBasis.MID, []) is None
    assert check_cost_basis_consistency(CostBasis.EXECUTABLE, ["spread"]) is None


def test_T24_le_calcul_dedge_reellement_cable_ne_deduit_pas_le_spread_deja_paye() -> None:
    """Bout en bout, par l'adaptateur RÉEL de la boucle : l'edge EXECUTABLE ne redéduit pas le spread.

    Ce test passe par ``BookCostComponents``, c'est-à-dire le chemin qu'emprunte une vraie décision,
    et non par un dictionnaire de coûts fabriqué pour le test — sinon il ne prouverait rien sur le
    système livré. L'écart entre les deux conventions doit valoir EXACTEMENT le spread : plus grand,
    le spread serait compté deux fois côté EXECUTABLE ; nul, la convention MID l'aurait oublié.
    """
    mid_builder = EdgeBuilder.from_cost_model(
        production_components(CostBasis.MID), uncertainty_penalty_coefficient=Decimal("0")
    )
    exec_builder = EdgeBuilder.from_cost_model(
        production_components(CostBasis.EXECUTABLE), uncertainty_penalty_coefficient=Decimal("0")
    )
    mid_edge = mid_builder.build(forecast(CostBasis.MID), contracts=Decimal("10"))
    exec_edge = exec_builder.build(forecast(CostBasis.EXECUTABLE), contracts=Decimal("10"))

    assert mid_edge.included_in_price == []
    assert exec_edge.included_in_price == ["spread"]
    # Les composantes du constructeur d'edges sont des MAGNITUDES de coût : positives.
    assert mid_edge.components["fees"] == TAKER_RATE
    assert mid_edge.components["spread"] == HALF_SPREAD_FRACTION
    assert exec_edge.components["fees"] == TAKER_RATE
    assert "spread" not in exec_edge.components or exec_edge.components["spread"] == Decimal("0")
    # Le spread est déduit une fois en MID, zéro fois en EXECUTABLE : l'écart vaut le demi-spread.
    assert exec_edge.net_edge - mid_edge.net_edge == HALF_SPREAD_FRACTION
    assert mid_edge.net_edge == exec_edge.net_edge - mid_edge.components["spread"]
    assert exec_edge.side is PositionSide.LONG


def test_T24_une_prevision_executable_dont_le_modele_noublie_pas_le_spread_est_refusee() -> None:
    """Le refus au niveau du constructeur d'edges : prix EXECUTABLE mais spread non déclaré inclus.

    C'est la même faute que plus haut, vue depuis la prévision : le modèle s'apprête à déduire un
    spread que le prix contient déjà. On refuse avant de produire un ``net_edge`` faux.
    """
    builder = EdgeBuilder(
        cost_components=lambda instrument, side, contracts, horizon_s: {"fees": TAKER_RATE},
        included_in_price=(),  # rien de déclaré : incompatible avec une prévision EXECUTABLE
        uncertainty_penalty_coefficient=Decimal("0"),
    )
    with pytest.raises(UnitError):
        builder.build(forecast(CostBasis.EXECUTABLE), contracts=Decimal("10"))
    # Contre-épreuve : la même prévision avec un modèle qui déclare le spread inclus passe.
    coherent = EdgeBuilder(
        cost_components=lambda instrument, side, contracts, horizon_s: {
            "fees": TAKER_RATE,
            "spread": HALF_SPREAD_FRACTION,
        },
        included_in_price=("spread",),
        uncertainty_penalty_coefficient=Decimal("0"),
    )
    edge = coherent.build(forecast(CostBasis.EXECUTABLE), contracts=Decimal("10"))
    assert edge.net_edge == edge.expected_gross_return - TAKER_RATE
