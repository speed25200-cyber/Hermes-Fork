"""Budgets de risque et exposition PESSIMISTE : T43 (§25, §53).

POURQUOI ces cas comptent. Entre le moment où un ordre est envoyé et celui où son sort est connu,
l'exposition du compte n'est pas connue : elle est un INTERVALLE. Une comptabilité qui compenserait un
achat en attente par une vente en attente afficherait une exposition nulle alors que les deux peuvent
s'exécuter — ou qu'un seul peut s'exécuter. Un ordre à l'état ``UNKNOWN`` est le cas le plus coûteux :
on ne sait pas s'il vit, et le supposer mort est la seule hypothèse qui fasse perdre de l'argent.

La règle du module est donc : pour chaque instrument, on retient le PIRE des deux mondes (« tous les
achats s'exécutent » contre « toutes les ventes s'exécutent »), et un ordre ``reduce_only`` ne peut
jamais ouvrir. Chaque limite testée ici l'est dans les deux sens : le cas qui doit bloquer ET le cas
admissible qui doit passer — sinon les tests passeraient aussi sur un moteur qui refuse tout.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from okxq.domain.instruments import InstrumentSpec, InstrumentState
from okxq.domain.money import ONE, ZERO, Money, Side
from okxq.domain.orders import OrderState, is_terminal
from okxq.domain.reasons import ReasonCode
from okxq.risk.budgets import (
    LimitLevel,
    LimitSet,
    OpenOrderState,
    PositionState,
    ReservationView,
    build_exposure,
    compute_limits_version,
    evaluate_budgets,
    initial_margin_estimate,
    liquidation_distance,
    margin_utilization,
)

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
EQUITY = Money(Decimal("100000"), "USDT")
PRICES: dict[str, Decimal] = {"AAA-USDT-SWAP": Decimal("1000"), "BBB-USDT-SWAP": Decimal("1000")}
# 1 contrat = 1 unité de base à 1 000 USDT ; 10 contrats = 10 000 USDT = 0,1 d'equity.
TEN_CONTRACTS = Decimal("0.1")


def spec(inst_id: str) -> InstrumentSpec:
    return InstrumentSpec(
        inst_id=inst_id,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        observed_at=T0,
        settle_ccy="USDT",
        base_ccy=inst_id.split("-")[0],
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=Decimal("1"),
        tick_size=Decimal("0.1"),
        lot_size=Decimal("1"),
        min_size=Decimal("1"),
        state=InstrumentState.LIVE,
        provenance="fixture:test_risk_budgets",
        max_leverage=Decimal("10"),
    )


SPECS: dict[str, InstrumentSpec] = {inst: spec(inst) for inst in PRICES}


def limits(**overrides: object) -> LimitSet:
    values: dict[str, object] = {
        "max_gross_equity_multiple": Decimal("2"),
        "max_abs_net_equity_multiple": Decimal("1"),
        "max_asset_equity_multiple": Decimal("0.5"),
        "max_cluster_gross_equity_multiple": Decimal("1"),
        "max_directional_equity_multiple": Decimal("1.5"),
        "max_abs_btc_beta_exposure": Decimal("1"),
        "max_abs_eth_beta_exposure": Decimal("1"),
        "max_margin_utilization": Decimal("0.5"),
        "margin_buffer_fraction": Decimal("0.2"),
        "liquidation_distance_min_fraction": Decimal("0.15"),
        "daily_loss_halt_fraction": Decimal("0.05"),
        "drawdown_review_fraction": Decimal("0.1"),
        "max_order_equity_multiple": Decimal("0.5"),
        "max_order_participation_fraction": Decimal("0.1"),
        "max_depth_consumption_fraction": Decimal("0.2"),
        "max_turnover_equity_fraction_per_decision": Decimal("1"),
        "max_orders_per_minute": 30,
        "approval_ttl_ms": 2_000,
        "max_order_intent_age_ms": 3_000,
        "max_quote_age_ms": 2_000,
        "max_private_state_age_ms": 5_000,
        "max_clock_offset_ms": 250,
        "max_relative_spread": Decimal("0.001"),
        "resume_stability_seconds": 120,
        "require_reconciliation_before_entries": True,
    }
    values.update(overrides)
    return LimitSet(**values)  # type: ignore[arg-type]


def order(
    inst_id: str,
    side: Side,
    contracts: str,
    state: OrderState = OrderState.ACKNOWLEDGED,
    *,
    reduce_only: bool = False,
) -> OpenOrderState:
    return OpenOrderState(
        client_order_id=f"cid-{inst_id}-{side.value}-{contracts}",
        inst_id=inst_id,
        side=side,
        remaining_contracts=Decimal(contracts),
        observed_state=state,
        reduce_only=reduce_only,
    )


def exposure(
    *,
    positions: Mapping[str, PositionState] | None = None,
    open_orders: Sequence[OpenOrderState] = (),
    reservations: Iterable[ReservationView] = (),
    extra_orders: Sequence[OpenOrderState] = (),
    equity: Money = EQUITY,
):
    return build_exposure(
        equity=equity,
        positions=positions or {},
        open_orders=open_orders,
        reservations=reservations,
        specs=SPECS,
        reference_prices=PRICES,
        extra_orders=extra_orders,
    )


def position(inst_id: str, contracts: str, **kwargs: object) -> PositionState:
    return PositionState(inst_id=inst_id, signed_contracts=Decimal(contracts), **kwargs)  # type: ignore[arg-type]


# --- T43 : deux ordres opposés ne se compensent jamais -------------------------------------------------


def test_T43_opposite_pending_orders_never_offset_each_other() -> None:
    """T43 : un achat et une vente de 10 contrats en attente sur le MÊME instrument.

    Une comptabilité nette afficherait zéro. Or l'un des deux peut s'exécuter seul : l'exposition
    réelle est comprise entre −10 et +10 contrats. Le budget doit retenir le pire des deux, sinon la
    limite brute serait calculée sur une exposition qui n'existe dans aucun scénario.
    """
    inst = "AAA-USDT-SWAP"
    snap = exposure(open_orders=[order(inst, Side.BUY, "10"), order(inst, Side.SELL, "10")])

    view = snap.per_instrument[inst]
    assert view.pending_buy_contracts == Decimal("10")
    assert view.pending_sell_contracts == Decimal("10")
    assert view.if_buys_fill == Decimal("10")
    assert view.if_sells_fill == Decimal("-10")
    assert snap.gross == TEN_CONTRACTS, "la compensation aurait rendu 0"
    assert snap.asset(inst) == TEN_CONTRACTS
    # Les deux directions sont plafonnées séparément : chacune est possible.
    assert snap.long == TEN_CONTRACTS
    assert snap.short == TEN_CONTRACTS

    # Contre-épreuve 1 : sans aucun ordre ni position, l'exposition est réellement nulle.
    assert exposure().gross == ZERO
    # Contre-épreuve 2 : l'ordre opposé n'a RIEN retiré — un seul achat donne la même exposition brute.
    assert exposure(open_orders=[order(inst, Side.BUY, "10")]).gross == TEN_CONTRACTS


def test_T43_net_exposure_takes_the_worst_of_the_two_worlds_across_instruments() -> None:
    """T43 : un achat en attente sur AAA et une vente en attente sur BBB.

    « Tout s'exécute » donnerait un net nul et rassurant. Mais si seul l'achat passe, le compte est
    long de 0,1 ; si seule la vente passe, il est short de 0,1. Le net retenu doit être le pire des
    deux mondes, pas leur moyenne.
    """
    snap = exposure(
        open_orders=[order("AAA-USDT-SWAP", Side.BUY, "10"), order("BBB-USDT-SWAP", Side.SELL, "10")]
    )

    assert snap.net_abs == TEN_CONTRACTS
    assert snap.gross == Decimal("0.2")

    # Contre-épreuve : deux achats de même sens s'ajoutent vraiment (le net n'est pas plafonné à tort).
    both_long = exposure(
        open_orders=[order("AAA-USDT-SWAP", Side.BUY, "10"), order("BBB-USDT-SWAP", Side.BUY, "10")]
    )
    assert both_long.net_abs == Decimal("0.2")


@pytest.mark.parametrize(
    "state",
    [
        OrderState.INTENT_CREATED,
        OrderState.RISK_APPROVED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_REQUESTED,
        OrderState.UNKNOWN,
    ],
)
def test_T43_every_non_terminal_state_counts_as_potentially_active(state: OrderState) -> None:
    """T43 : ``UNKNOWN`` et ``CANCEL_REQUESTED`` comptent.

    Une demande d'annulation n'est pas une annulation, et un ACK perdu n'est pas un rejet. Traiter
    ces deux états comme inoffensifs libérerait du budget de risque au moment précis où l'on ignore
    ce que l'exchange détient — c'est l'erreur qui transforme une incertitude en position doublée.
    """
    inst = "AAA-USDT-SWAP"
    pending = order(inst, Side.BUY, "10", state)

    assert not is_terminal(state)
    assert pending.potentially_active
    assert exposure(open_orders=[pending]).gross == TEN_CONTRACTS


@pytest.mark.parametrize(
    "state", [OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED]
)
def test_T43_a_terminal_state_stops_consuming_budget(state: OrderState) -> None:
    """Contre-épreuve indispensable : un état FINAL observé ne consomme plus de budget. Sans cela, le
    compte se remplirait d'ordres morts et finirait par ne plus rien pouvoir envoyer — un moteur qui
    compte tout est aussi faux qu'un moteur qui ne compte rien.
    """
    inst = "AAA-USDT-SWAP"
    done = order(inst, Side.BUY, "10", state)

    assert is_terminal(state)
    assert not done.potentially_active
    assert exposure(open_orders=[done]).gross == ZERO


def test_T43_a_reduce_only_order_is_capped_by_the_position_and_never_opens() -> None:
    """T43 : un ordre ``reduce_only`` ne peut pas ouvrir. Sa contribution est plafonnée par la
    position qu'il réduit — sinon une vente reduce-only de 50 contrats contre une position de 10
    compterait comme un short de 40 qui ne peut pas exister, et bloquerait des réductions utiles.
    """
    inst = "AAA-USDT-SWAP"
    held = {inst: position(inst, "10")}

    # Vente reduce-only surdimensionnée : plafonnée à la position, donc au mieux un solde à zéro.
    capped = exposure(positions=held, open_orders=[order(inst, Side.SELL, "50", reduce_only=True)])
    assert capped.per_instrument[inst].if_sells_fill == ZERO
    assert capped.gross == TEN_CONTRACTS  # le pire reste la position tenue

    # Achat reduce-only sur une position LONGUE : il n'y a rien à réduire, contribution nulle.
    wrong_side = exposure(positions=held, open_orders=[order(inst, Side.BUY, "5", reduce_only=True)])
    assert wrong_side.per_instrument[inst].if_buys_fill == Decimal("10")

    # Contre-épreuve : le même ordre SANS reduce_only ouvre réellement un short de 40.
    opening = exposure(positions=held, open_orders=[order(inst, Side.SELL, "50")])
    assert opening.per_instrument[inst].if_sells_fill == Decimal("-40")
    assert opening.gross == Decimal("0.4")


def test_T43_active_reservations_and_the_projected_intent_consume_budget() -> None:
    """T43 : une réservation active est une exposition déjà engagée, même sans ordre encore visible à
    l'exchange ; et l'intention en cours d'évaluation doit être comptée AVANT d'être approuvée, sinon
    chaque ordre serait validé contre un compte qui l'ignore.
    """
    inst = "AAA-USDT-SWAP"
    reserved = exposure(reservations=[ReservationView(inst, Decimal("10"))])
    assert reserved.gross == TEN_CONTRACTS

    projected = exposure(extra_orders=[order(inst, Side.BUY, "10", OrderState.INTENT_CREATED)])
    assert projected.gross == TEN_CONTRACTS

    # Les deux s'additionnent : une réservation ne « couvre » pas une seconde intention.
    both = exposure(
        reservations=[ReservationView(inst, Decimal("10"))],
        extra_orders=[order(inst, Side.BUY, "10", OrderState.INTENT_CREATED)],
    )
    assert both.gross == Decimal("0.2")


def test_T43_beta_exposure_is_pessimistic_even_with_opposite_sign_betas() -> None:
    """T43, bêtas : AAA a un bêta de +1, BBB un bêta de −1 (instrument de couverture).

    Un achat en attente sur AAA et une vente en attente sur BBB pointent dans le MÊME sens en bêta :
    +1 × (+10) et −1 × (−10) s'ajoutent. Ne considérer que « tous les achats » puis « toutes les
    ventes » manque ce cumul et sous-estime l'exposition bêta de moitié. Une limite bêta calculée sur
    une exposition sous-estimée est une limite qui ne protège pas.
    """
    betas = {"AAA-USDT-SWAP": ONE, "BBB-USDT-SWAP": -ONE}
    snap = exposure(
        open_orders=[order("AAA-USDT-SWAP", Side.BUY, "10"), order("BBB-USDT-SWAP", Side.SELL, "10")]
    )

    assert snap.beta(betas) == Decimal("0.2")

    # Contre-épreuve : avec des bêtas de même signe, les deux ordres opposés ne se cumulent PAS —
    # le pire reste 0,1. Un moteur qui additionnerait toujours les valeurs absolues échouerait ici.
    same_sign = {"AAA-USDT-SWAP": ONE, "BBB-USDT-SWAP": ONE}
    assert snap.beta(same_sign) == TEN_CONTRACTS
    # Et un bêta nul n'expose à rien.
    assert snap.beta({}) == ZERO


def test_T43_an_unmeasurable_exposure_is_refused_rather_than_assumed() -> None:
    """Sans equity positive, ou sans métadonnées/prix pour un instrument tenu, aucune fraction
    d'equity n'a de sens. Rendre 0 dans ces cas afficherait « aucun risque » sur un compte dont on ne
    sait rien.
    """
    with pytest.raises(ValueError):
        exposure(equity=Money(ZERO, "USDT"))

    with pytest.raises(ValueError):
        build_exposure(
            equity=EQUITY,
            positions={"ZZZ-USDT-SWAP": position("ZZZ-USDT-SWAP", "1")},
            open_orders=[],
            reservations=[],
            specs=SPECS,
            reference_prices=PRICES,
        )


# --- limites de LimitSet ------------------------------------------------------------------------------


def test_each_limit_of_the_limit_set_blocks_its_own_breach_and_nothing_else() -> None:
    """Chaque niveau de limite (portefeuille, instrument, cluster, direction) doit être exercé
    SÉPARÉMENT : un moteur qui ne vérifierait que la limite brute passerait un test global tant que
    le brut est atteint en premier.
    """
    clusters = {"alt": list(PRICES)}
    # 30 contrats sur AAA = 0,3 d'equity : sous la limite d'actif (0,5), donc rien ne casse.
    admissible = exposure(open_orders=[order("AAA-USDT-SWAP", Side.BUY, "30")])
    assert evaluate_budgets(admissible, limits(), clusters=clusters) == []

    # Limite d'instrument : 60 contrats = 0,6 > 0,5.
    breach_asset = exposure(open_orders=[order("AAA-USDT-SWAP", Side.BUY, "60")])
    names = {b.name: b for b in evaluate_budgets(breach_asset, limits(), clusters=clusters)}
    assert "asset:AAA-USDT-SWAP" in names
    assert names["asset:AAA-USDT-SWAP"].level is LimitLevel.INSTRUMENT
    assert names["asset:AAA-USDT-SWAP"].reason is ReasonCode.RISK_LIMIT
    assert names["asset:AAA-USDT-SWAP"].used == Decimal("0.6")
    assert names["asset:AAA-USDT-SWAP"].excess == Decimal("0.1")

    # Limite nette : 40 + 40 dans le même sens = 0,8 > 0,5... on relâche l'actif pour isoler le net.
    net_only = limits(max_asset_equity_multiple=Decimal("1"), max_abs_net_equity_multiple=Decimal("0.5"))
    breach_net = exposure(
        open_orders=[order("AAA-USDT-SWAP", Side.BUY, "40"), order("BBB-USDT-SWAP", Side.BUY, "40")]
    )
    net_names = {b.name for b in evaluate_budgets(breach_net, net_only, clusters=clusters)}
    assert "net" in net_names
    assert not {"asset:AAA-USDT-SWAP", "asset:BBB-USDT-SWAP"} & net_names

    # Limite brute : le même portefeuille avec un net autorisé mais un brut serré.
    gross_only = limits(
        max_asset_equity_multiple=Decimal("1"),
        max_abs_net_equity_multiple=Decimal("1"),
        max_gross_equity_multiple=Decimal("0.5"),
        max_cluster_gross_equity_multiple=Decimal("5"),
        max_directional_equity_multiple=Decimal("5"),
    )
    gross_names = {b.name for b in evaluate_budgets(breach_net, gross_only)}
    assert gross_names == {"gross"}
    assert breach_net.gross == Decimal("0.8")

    # Limite de cluster : brut et actif admissibles, mais la somme du cluster non.
    cluster_only = limits(
        max_asset_equity_multiple=Decimal("1"),
        max_abs_net_equity_multiple=Decimal("1"),
        max_gross_equity_multiple=Decimal("5"),
        max_directional_equity_multiple=Decimal("5"),
        max_cluster_gross_equity_multiple=Decimal("0.5"),
    )
    cluster_names = {b.name for b in evaluate_budgets(breach_net, cluster_only, clusters=clusters)}
    assert cluster_names == {"cluster:alt"}

    # Limite directionnelle : 80 contrats longs = 0,8 > 0,5 côté long, et 0 côté short.
    direction_only = limits(
        max_asset_equity_multiple=Decimal("1"),
        max_abs_net_equity_multiple=Decimal("1"),
        max_gross_equity_multiple=Decimal("5"),
        max_cluster_gross_equity_multiple=Decimal("5"),
        max_directional_equity_multiple=Decimal("0.5"),
    )
    direction = {b.name for b in evaluate_budgets(breach_net, direction_only)}
    assert direction == {"long"}, "un compte uniquement long ne viole pas la limite short"


def test_the_btc_and_eth_beta_limits_are_checked_independently() -> None:
    """Les deux bêtas sont des limites distinctes : un portefeuille peut être neutre BTC et exposé
    ETH. Les confondre laisserait passer la moitié des expositions de facteur.
    """
    clusters = {"alt": list(PRICES)}
    snap = exposure(open_orders=[order("AAA-USDT-SWAP", Side.BUY, "50")])  # 0,5 d'equity
    betas_btc = {"AAA-USDT-SWAP": Decimal("3")}  # exposition bêta BTC = 1,5
    betas_eth = {"AAA-USDT-SWAP": Decimal("0.5")}  # exposition bêta ETH = 0,25

    breaches = evaluate_budgets(
        snap,
        limits(max_asset_equity_multiple=Decimal("1")),
        betas_btc=betas_btc,
        betas_eth=betas_eth,
        clusters=clusters,
    )
    names = {b.name for b in breaches}
    assert names == {"beta_btc"}

    # Contre-épreuve : sans bêtas fournis, aucune limite bêta n'est inventée.
    assert {b.name for b in evaluate_budgets(snap, limits(max_asset_equity_multiple=Decimal("1")))} == set()


def test_the_effective_margin_capacity_keeps_the_declared_buffer() -> None:
    """La capacité utilisable est ``max_utilisation × (1 − buffer)``. Utiliser la limite brute
    consommerait le tampon prévu pour les variations de marge — c'est-à-dire le supprimer.
    """
    lim = limits(max_margin_utilization=Decimal("0.5"), margin_buffer_fraction=Decimal("0.2"))
    assert lim.effective_margin_capacity == Decimal("0.40")
    assert lim.effective_margin_capacity < lim.max_margin_utilization


def test_margin_utilization_is_none_when_it_has_not_been_measured() -> None:
    """Une marge inconnue est ``None``, jamais 0 : rendre 0 signifierait « aucune marge utilisée »,
    c'est-à-dire l'affirmation la plus fausse et la plus permissive possible.
    """
    assert margin_utilization(None, EQUITY) is None
    assert margin_utilization(Decimal("25000"), EQUITY) == Decimal("0.25")
    # Equity nulle ou négative : l'utilisation est PLEINE, pas indéfinie.
    assert margin_utilization(Decimal("1"), Money(ZERO, "USDT")) == ONE


def test_the_initial_margin_estimate_assumes_no_leverage_when_none_is_known() -> None:
    """Sans levier connu, on suppose 1× : tout le notionnel est immobilisé. Supposer le levier
    maximal ferait croire à une capacité qui n'existe peut-être pas.
    """
    assert initial_margin_estimate(Decimal("10000"), None) == Decimal("10000")
    assert initial_margin_estimate(Decimal("10000"), Decimal("10")) == Decimal("1000")
    # Un levier absurde ne devient pas une division par zéro silencieuse.
    assert initial_margin_estimate(Decimal("10000"), Decimal("0")) == Decimal("10000")


def test_liquidation_distance_is_never_infinite_when_the_price_is_missing() -> None:
    """Un prix de liquidation absent n'est PAS une distance infinie. Sans prix, on estime depuis le
    levier ; sans levier, la distance vaut zéro et la position est traitée comme immédiatement à
    risque. C'est le seul choix qui ne finance pas une position dont on ignore la fragilité.
    """
    inst = "AAA-USDT-SWAP"
    unknown = position(inst, "10")
    assert liquidation_distance(unknown) == ZERO

    from_leverage = position(inst, "10", leverage=Decimal("4"))
    assert liquidation_distance(from_leverage) == Decimal("0.2")  # (1/4) × 0,8

    measured = position(inst, "10", mark_price=Decimal("1000"), liquidation_price=Decimal("900"))
    assert liquidation_distance(measured) == Decimal("0.1")

    # Contre-épreuve : une position plate n'est pas liquidable, sa distance vaut 1.
    assert liquidation_distance(position(inst, "0")) == ONE


def test_the_limits_version_changes_with_the_values_and_only_with_them() -> None:
    """Toute approbation est liée à ``limits_version``. Une version figée validerait un ordre contre
    des limites qui ont changé ; une version instable invaliderait des approbations valides. Les deux
    sont des défauts.
    """
    a = limits()
    b = limits()
    assert a.limits_version == b.limits_version
    assert a.limits_version == compute_limits_version(a)
    assert a.limits_version.startswith("limits-")

    tighter = limits(max_gross_equity_multiple=Decimal("1.9"))
    assert tighter.limits_version != a.limits_version
    # Un champ non monétaire compte aussi : le TTL d'approbation fait partie du contrat de risque.
    assert limits(approval_ttl_ms=2_001).limits_version != a.limits_version
