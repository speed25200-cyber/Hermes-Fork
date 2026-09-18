"""Dépôt et retrait externes : T45 (§38).

POURQUOI c'est le test le plus important de cette série. Un apport de capital augmente l'equity. Si le
système le compte comme un gain, alors TOUTE mesure de performance devient fausse dans le sens le plus
dangereux : la courbe monte, le drawdown s'efface, le high-water mark grimpe, la limite de perte
journalière se détend. Le système croit gagner de l'argent au moment où il n'a fait qu'en recevoir —
et il continue de prendre du risque sur cette croyance. Symétriquement, un retrait ne doit pas être
compté comme une perte, sinon chaque virement déclencherait un halt.

La convention du dépôt est DÉCLARÉE : le flux entre en début de période et crée (ou détruit) des parts
au dernier prix de part connu. La valeur de part ne bouge donc qu'avec le PnL de stratégie. C'est cette
propriété qui est vérifiée ici, dans les deux sens : un flux externe ne change rien, un vrai gain change
tout.

Tout est local : SQLite en mémoire et horloge simulée.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from okxq.accounting.ledger import (
    ACCOUNT_CASH,
    ACCOUNT_EXTERNAL,
    FeeSource,
    Ledger,
    TxnKind,
    account_name,
)
from okxq.accounting.pnl import (
    EquityPoint,
    HighWaterMark,
    daily_pnl,
    drawdowns,
    max_drawdown,
    strategy_pnl,
    unitize,
    update_high_water_mark,
)
from okxq.config.schema import RiskCfg
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import LedgerError
from okxq.domain.events import Fill, Liquidity
from okxq.domain.instruments import InstrumentSpec, InstrumentState
from okxq.domain.money import ZERO, Side
from okxq.persistence.db import make_session_factory, memory_engine
from okxq.risk.kill_switch import (
    HaltLevel,
    HealthSignals,
    InMemoryRiskStateStore,
    KillSwitch,
)

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SCOPE = "scope-test"
INST = "AAA-USDT-SWAP"
DAY = timedelta(days=1)


def spec() -> InstrumentSpec:
    return InstrumentSpec(
        inst_id=INST,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        observed_at=T0,
        settle_ccy="USDT",
        base_ccy="AAA",
        quote_ccy="USDT",
        contract_type="linear",
        base_units_per_contract=Decimal("1"),
        tick_size=Decimal("0.1"),
        lot_size=Decimal("1"),
        min_size=Decimal("1"),
        state=InstrumentState.LIVE,
        provenance="fixture:test_accounting_external_cashflow",
        max_leverage=Decimal("10"),
    )


def ledger() -> Ledger:
    """Ledger neuf sur une base en mémoire : aucune donnée héritée d'un autre test."""
    return Ledger(
        make_session_factory(memory_engine()),
        account_scope=SCOPE,
        clock=SimulatedClock(T0),
        settle_ccy="USDT",
        fee_source=FeeSource.PER_FILL,
    )


def fill(*, side: Side, contracts: str, price: str, at: datetime, trade_id: str) -> Fill:
    return Fill(
        execution_key=f"{SCOPE}:{INST}:ex-1:{trade_id}",
        account_scope=SCOPE,
        inst_id=INST,
        client_order_id=f"cid-{trade_id}",
        exchange_order_id="ex-1",
        trade_id=trade_id,
        side=side,
        contracts=Decimal(contracts),
        fill_price=Decimal(price),
        fee_cashflow=ZERO,
        fee_ccy="USDT",
        fill_at=at,
        receive_ts=at,
        liquidity=Liquidity.TAKER,
    )


def point(at: datetime, equity: str, external_cum: str) -> EquityPoint:
    return EquityPoint(at=at, equity=Decimal(equity), external_cashflow_cum=Decimal(external_cum))


# Série de référence, choisie pour que toutes les divisions soient EXACTES en Decimal : une valeur de
# part de 1,25 et un apport de 50 000 donnent 40 000 parts pile. Les nombres ronds sont ici une
# précaution de test, pas une hypothèse sur le monde.
P0 = point(T0, "100000", "100000")  # financement initial : 100 000 apportés, valeur de part 1
P1 = point(T0 + DAY, "125000", "100000")  # +25 000 de PnL de stratégie : valeur de part 1,25
P2 = point(T0 + 2 * DAY, "175000", "150000")  # DÉPÔT de 50 000, aucun PnL
P3 = point(T0 + 3 * DAY, "125000", "100000")  # RETRAIT de 50 000, aucun PnL
P4 = point(T0 + 4 * DAY, "100000", "100000")  # vraie perte de 25 000


# --- T45 : le ledger distingue un flux externe d'un gain ----------------------------------------------


def test_T45_a_deposit_moves_cash_and_external_cumulative_but_no_pnl_account() -> None:
    """T45, comptabilité : un dépôt crédite la trésorerie ET le compte ``external`` en sens inverse.

    Le PnL réalisé, les frais et le funding ne bougent pas. S'ils bougeaient — ne serait-ce que par
    commodité de câblage — le rapport de performance additionnerait du capital apporté à des gains,
    et personne ne pourrait plus distinguer les deux après coup.
    """
    book = ledger()
    book.register_spec(spec())

    deposit = book.record_external_cashflow(
        amount=Decimal("100000"), occurred_at=T0, idempotency_key="bill-dep-1", description="dépôt initial"
    )
    assert deposit.applied
    assert deposit.kind is TxnKind.EXTERNAL

    view = book.equity({})
    assert view.cash_collateral == Decimal("100000")
    assert view.equity == Decimal("100000")
    assert view.external_cashflow_cum == Decimal("100000")
    assert view.realized_pnl_cum == ZERO
    assert view.fees_cum == ZERO
    assert view.funding_cum == ZERO
    assert view.adjustments_cum == ZERO
    # Écritures équilibrées par devise : le compte de contrepartie porte l'opposé exact du cash.
    assert book.balance(account_name(ACCOUNT_CASH, "USDT")) == Decimal("100000")
    assert book.balance(account_name(ACCOUNT_EXTERNAL, "USDT")) == Decimal("-100000")
    assert book.audit().ok


def test_T45_strategy_pnl_separates_a_real_gain_from_a_capital_contribution() -> None:
    """T45 : deux mouvements d'equity de +50 000, l'un gagné, l'autre apporté.

    Le premier doit apparaître dans le PnL de stratégie, le second pas du tout. C'est la seule
    distinction qui permette de dire si la stratégie fonctionne — et elle se fait sur l'equity ET le
    cumul des flux externes, jamais sur l'equity seule.
    """
    book = ledger()
    book.register_spec(spec())
    book.record_external_cashflow(amount=Decimal("100000"), occurred_at=T0, idempotency_key="dep-1")
    before = book.equity({}, as_of=T0)

    # Un vrai gain : achat à 1 000, revente à 1 500 sur 100 contrats → +50 000 réalisés.
    book.record_fill(fill(side=Side.BUY, contracts="100", price="1000", at=T0, trade_id="t1"), spec())
    book.record_fill(
        fill(side=Side.SELL, contracts="100", price="1500", at=T0 + timedelta(minutes=1), trade_id="t2"),
        spec(),
    )
    after_gain = book.equity({}, as_of=T0 + timedelta(minutes=2))

    assert after_gain.equity - before.equity == Decimal("50000")
    assert after_gain.realized_pnl_cum == Decimal("50000")
    assert strategy_pnl(
        EquityPoint(before.as_of, before.equity, before.external_cashflow_cum),
        EquityPoint(after_gain.as_of, after_gain.equity, after_gain.external_cashflow_cum),
    ) == Decimal("50000")

    # Le même mouvement d'equity, mais apporté : PnL de stratégie NUL.
    book.record_external_cashflow(
        amount=Decimal("50000"), occurred_at=T0 + timedelta(hours=1), idempotency_key="dep-2"
    )
    after_deposit = book.equity({}, as_of=T0 + timedelta(hours=2))

    assert after_deposit.equity - after_gain.equity == Decimal("50000")
    assert after_deposit.realized_pnl_cum == Decimal("50000"), "le dépôt a été compté comme un gain"
    assert after_deposit.external_cashflow_cum == Decimal("150000")
    assert (
        strategy_pnl(
            EquityPoint(after_gain.as_of, after_gain.equity, after_gain.external_cashflow_cum),
            EquityPoint(after_deposit.as_of, after_deposit.equity, after_deposit.external_cashflow_cum),
        )
        == ZERO
    )


def test_T45_a_withdrawal_is_not_a_loss() -> None:
    """Contre-épreuve symétrique : un retrait réduit l'equity sans être une perte. Le compter comme
    une perte déclencherait un halt à chaque virement — une protection qui se déclenche pour de
    mauvaises raisons finit par être désactivée, et c'est ainsi qu'on perd la protection.
    """
    book = ledger()
    book.record_external_cashflow(amount=Decimal("100000"), occurred_at=T0, idempotency_key="dep-1")
    before = book.equity({}, as_of=T0)

    book.record_external_cashflow(
        amount=Decimal("-30000"), occurred_at=T0 + DAY, idempotency_key="wdr-1", description="retrait"
    )
    after = book.equity({}, as_of=T0 + DAY)

    assert after.equity == Decimal("70000")
    assert after.external_cashflow_cum == Decimal("70000")
    assert (
        strategy_pnl(
            EquityPoint(before.as_of, before.equity, before.external_cashflow_cum),
            EquityPoint(after.as_of, after.equity, after.external_cashflow_cum),
        )
        == ZERO
    )
    assert book.audit().ok


def test_T45_a_replayed_external_movement_has_no_second_effect() -> None:
    """T45 et T34 : les bills externes sont relus après un redémarrage ou une reconnexion. Un dépôt
    compté deux fois créerait du capital qui n'existe pas — et toutes les fractions d'equity (limites,
    tailles d'ordre, marge) seraient calculées sur ce capital fantôme.
    """
    book = ledger()
    first = book.record_external_cashflow(amount=Decimal("100000"), occurred_at=T0, idempotency_key="bill-42")
    assert first.applied

    replay = book.record_external_cashflow(
        amount=Decimal("100000"), occurred_at=T0, idempotency_key="bill-42"
    )
    assert replay.applied is False
    assert replay.entries == (), "un rejeu ne produit aucune écriture"
    assert book.equity({}).cash_collateral == Decimal("100000")
    assert book.equity({}).external_cashflow_cum == Decimal("100000")

    # Et la reconstruction depuis la base donne le même état : l'idempotence est persistée.
    book.load()
    assert book.equity({}).external_cashflow_cum == Decimal("100000")
    audit = book.audit()
    assert audit.duplicate_keys == ()
    assert audit.ok


def test_T45_a_zero_external_movement_is_refused_rather_than_recorded() -> None:
    """Un flux nul n'est pas un événement : l'enregistrer consommerait une clé d'idempotence et
    masquerait le vrai mouvement qui portera la même référence.
    """
    book = ledger()
    with pytest.raises(LedgerError):
        book.record_external_cashflow(amount=ZERO, occurred_at=T0, idempotency_key="bill-vide")
    assert book.equity({}).external_cashflow_cum == ZERO


# --- T45 : equity unitisée, high-water mark, drawdown -------------------------------------------------


def test_T45_a_deposit_leaves_the_unit_value_and_the_high_water_mark_untouched() -> None:
    """T45, cœur du sujet : l'equity passe de 125 000 à 175 000 par un DÉPÔT de 50 000.

    Un high-water mark tenu sur l'equity brute grimperait à 175 000 et le drawdown repartirait de
    zéro. Sur la valeur de PART, rien ne bouge : 1,25 avant, 1,25 après. C'est exactement ce que
    T45 exige — ni la performance ni le high-water mark ne sont artificiellement améliorés.
    """
    units = unitize([P0, P1, P2])

    assert [u.unit_value for u in units] == [Decimal("1"), Decimal("1.25"), Decimal("1.25")]
    assert units[2].units == Decimal("140000"), "le dépôt crée des parts au dernier prix connu"
    assert units[2].external_flow == Decimal("50000")
    # Le dépôt ne crée aucun nouveau plus-haut : le high-water mark reste celui du vrai gain.
    marks = drawdowns(units)
    assert [d.high_water_mark for d in marks] == [Decimal("1"), Decimal("1.25"), Decimal("1.25")]
    assert max_drawdown(units) == ZERO
    # Et l'equity brute, elle, a bien monté : c'est précisément le piège qu'on évite.
    assert units[2].equity > units[1].equity


def test_T45_a_real_gain_does_raise_the_unit_value_and_the_high_water_mark() -> None:
    """Contre-épreuve indispensable : sans elle, un système qui figerait la valeur de part passerait
    le test précédent tout en n'enregistrant JAMAIS de performance — l'erreur inverse, aussi grave.
    """
    gain = point(T0 + 3 * DAY, "189000", "150000")  # 140 000 parts × 1,35
    units = unitize([P0, P1, P2, gain])

    assert units[-1].unit_value == Decimal("1.35")
    assert units[-1].external_flow == ZERO
    assert drawdowns(units)[-1].high_water_mark == Decimal("1.35")


def test_T45_a_withdrawal_neither_lowers_the_unit_value_nor_invents_a_drawdown() -> None:
    """Un retrait détruit des parts au dernier prix connu. La valeur de part reste 1,25 : aucun
    drawdown n'apparaît. Sans cette neutralisation, chaque retrait ressemblerait à une perte et
    déclencherait la revue de drawdown — puis on finirait par relever le seuil, c'est-à-dire par
    désactiver la protection.
    """
    units = unitize([P0, P1, P2, P3])

    assert units[-1].unit_value == Decimal("1.25")
    assert units[-1].units == Decimal("100000")
    assert units[-1].external_flow == Decimal("-50000")
    assert max_drawdown(units) == ZERO

    # Contre-épreuve : une VRAIE perte, elle, produit bien un drawdown mesuré.
    with_loss = unitize([P0, P1, P2, P3, P4])
    assert with_loss[-1].unit_value == Decimal("1")
    assert max_drawdown(with_loss) == Decimal("0.2")


def test_T45_the_high_water_mark_is_kept_on_the_unit_value_not_on_gross_equity() -> None:
    """``HighWaterMark`` est explicitement défini sur la valeur de part. On le vérifie sur la séquence
    complète : le plus-haut reste celui atteint par le PnL, alors que l'equity brute a été plus haute
    après le dépôt. Un high-water mark sur l'equity brute serait trivialement battu par un virement.
    """
    hwm = HighWaterMark()
    for u in unitize([P0, P1, P2, P3, P4]):
        hwm = update_high_water_mark(hwm, unit_value=u.unit_value, equity=u.equity, at=u.at)

    assert hwm.unit_value == Decimal("1.25")
    assert hwm.equity == Decimal("125000"), "le plus-haut retenu n'est pas celui gonflé par le dépôt"
    assert hwm.at == P1.at


def test_T45_daily_pnl_reports_external_flows_separately_from_strategy_pnl() -> None:
    """Le rapport journalier doit montrer les DEUX nombres. Un rapport qui ne publierait que la
    variation d'equity laisserait croire qu'un jour de dépôt a été un bon jour.
    """
    days = daily_pnl([P0, P1, P2, P3, P4])
    by_day = {d.day.date().isoformat(): d for d in days}

    gain_day = by_day[(T0 + DAY).date().isoformat()]
    assert gain_day.external_flows == ZERO
    assert gain_day.strategy_pnl == Decimal("25000")

    deposit_day = by_day[(T0 + 2 * DAY).date().isoformat()]
    assert deposit_day.external_flows == Decimal("50000")
    assert deposit_day.strategy_pnl == ZERO, "le jour du dépôt n'est pas un jour de gain"
    assert deposit_day.end_equity - deposit_day.start_equity == Decimal("50000")

    withdrawal_day = by_day[(T0 + 3 * DAY).date().isoformat()]
    assert withdrawal_day.external_flows == Decimal("-50000")
    assert withdrawal_day.strategy_pnl == ZERO

    loss_day = by_day[(T0 + 4 * DAY).date().isoformat()]
    assert loss_day.external_flows == ZERO
    assert loss_day.strategy_pnl == Decimal("-25000")


def test_T45_unitization_refuses_to_start_or_continue_on_an_impossible_series() -> None:
    """Sans equity initiale positive, il n'y a pas de valeur de part : on refuse au lieu d'inventer
    une base de 1. Et un retrait qui détruirait toutes les parts est signalé, pas divisé par zéro.
    """
    assert unitize([]) == []
    with pytest.raises(LedgerError):
        unitize([point(T0, "0", "0")])
    with pytest.raises(LedgerError):
        unitize([P0, point(T0 + DAY, "0", "0")])  # retrait de la totalité des apports
    with pytest.raises(LedgerError):
        unitize([P1, P0])  # série non ordonnée


# --- T45 : ce que le kill switch voit -----------------------------------------------------------------


def kill_switch(cfg: RiskCfg | None = None) -> tuple[KillSwitch, SimulatedClock]:
    clock = SimulatedClock(T0)
    switch = KillSwitch(
        account_scope=SCOPE,
        store=InMemoryRiskStateStore(),
        cfg=cfg or RiskCfg(),
        clock=clock,
        limits_version="limits-test",
    )
    return switch, clock


def test_T45_a_deposit_does_not_improve_the_drawdown_seen_by_the_kill_switch() -> None:
    """T45 côté protection : le drawdown est calculé sur la valeur de part dès qu'elle est fournie.

    Le compte tombe de 1,25 à 1,00 de valeur de part (−20 %), puis reçoit 50 000. L'equity remonte de
    100 000 à 150 000, bien au-dessus de son plus-haut précédent. Si le drawdown était lu sur
    l'equity, il repasserait à zéro et la revue de drawdown serait levée par un simple virement.
    """
    switch, clock = kill_switch(RiskCfg(drawdown_review_fraction=0.1, daily_loss_halt_fraction=0.01))

    switch.observe(HealthSignals(now=clock.now_utc(), equity=Decimal("125000"), unit_value=Decimal("1.25")))
    assert switch.state.high_water_mark_unit == Decimal("1.25")

    clock.advance(DAY)
    fallen = switch.observe(
        HealthSignals(now=clock.now_utc(), equity=Decimal("100000"), unit_value=Decimal("1"))
    )
    assert fallen.drawdown_fraction == Decimal("0.2")
    assert fallen.level is HaltLevel.SOFT_HALT  # politique : drawdown → SOFT_HALT

    # Apport de 50 000 : l'equity bondit, la valeur de part ne bouge pas.
    clock.advance(DAY)
    after_deposit = switch.observe(
        HealthSignals(now=clock.now_utc(), equity=Decimal("150000"), unit_value=Decimal("1"))
    )
    assert after_deposit.drawdown_fraction == Decimal("0.2"), "le dépôt a effacé le drawdown"
    assert switch.state.high_water_mark_unit == Decimal("1.25"), "le dépôt a relevé le high-water mark"
    assert [t.name for t in after_deposit.triggers] == ["drawdown"]

    # Contre-épreuve : un vrai retour au plus-haut, lui, efface bien le drawdown.
    clock.advance(DAY)
    recovered = switch.observe(
        HealthSignals(now=clock.now_utc(), equity=Decimal("187500"), unit_value=Decimal("1.25"))
    )
    assert recovered.drawdown_fraction == ZERO
    assert recovered.triggers == []


def test_T45_a_deposit_does_not_erase_a_daily_loss_that_was_already_reached() -> None:
    """T45 et §38 : la perte du jour est mesurée depuis l'equity de début de journée UTC.

    Le compte perd 7 % dans la journée — au-delà du seuil journalier, donc le halt tombe. Puis un
    apport de 10 000 fait remonter l'equity AU-DESSUS de son point de départ. Si la perte du jour est
    recalculée sur l'equity courante, elle repasse à zéro : le déclencheur disparaît, et un opérateur
    peut lever le halt le jour même. Autrement dit, il suffirait d'un virement pour annuler une
    limite de perte journalière atteinte.

    La perte du jour doit donc être un PLAFOND : ni un apport externe ni une remontée d'equity ne
    l'effacent avant la frontière UTC suivante.
    """
    switch, clock = kill_switch(RiskCfg(daily_loss_halt_fraction=0.05, drawdown_review_fraction=0.10))

    switch.observe(HealthSignals(now=clock.now_utc(), equity=Decimal("100000")))
    assert switch.state.day_start_equity == Decimal("100000")

    clock.advance(timedelta(hours=1))
    breached = switch.observe(HealthSignals(now=clock.now_utc(), equity=Decimal("93000")))
    assert "daily_loss" in {t.name for t in breached.triggers}
    assert breached.level is HaltLevel.HARD_HALT
    assert switch.state.day_realized_loss == Decimal("7000")

    # Apport de 10 000 : l'equity dépasse son point de départ.
    clock.advance(timedelta(hours=1))
    after_deposit = switch.observe(HealthSignals(now=clock.now_utc(), equity=Decimal("103000")))

    assert switch.state.day_realized_loss == Decimal("7000"), "l'apport a effacé la perte du jour"
    assert "daily_loss" in {t.name for t in after_deposit.triggers}
    assert switch.daily_loss_fraction(Decimal("103000")) == Decimal("0.07")
    # Conséquence directe : la reprise reste impossible le jour même.
    assert "declencheurs_actifs:daily_loss" in switch.resume_preconditions(
        HealthSignals(now=T0 + timedelta(hours=5), equity=Decimal("103000"))
    )


def test_T45_a_new_utc_day_resets_the_daily_loss_but_not_the_halt_level() -> None:
    """Contre-épreuve du test précédent : la perte du jour DOIT repartir de zéro à la frontière UTC
    déclarée, sinon le halt journalier serait définitif. Le NIVEAU de halt, lui, ne redescend pas
    tout seul : il attend une action opérateur (§53). Les deux propriétés sont indissociables.
    """
    switch, clock = kill_switch(RiskCfg(daily_loss_halt_fraction=0.05, drawdown_review_fraction=0.10))
    switch.observe(HealthSignals(now=clock.now_utc(), equity=Decimal("100000")))
    clock.advance(timedelta(hours=1))
    switch.observe(HealthSignals(now=clock.now_utc(), equity=Decimal("93000")))
    assert switch.level is HaltLevel.HARD_HALT

    # Jour UTC suivant : nouvelle equity de départ, perte du jour remise à zéro.
    clock.advance(DAY)
    next_day = switch.observe(HealthSignals(now=clock.now_utc(), equity=Decimal("93000")))

    assert switch.state.day_realized_loss == ZERO
    assert switch.state.day_start_equity == Decimal("93000")
    assert "daily_loss" not in {t.name for t in next_day.triggers}
    assert switch.level is HaltLevel.HARD_HALT, "le niveau de halt ne redescend jamais tout seul"
