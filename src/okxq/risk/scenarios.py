"""Scénarios de perte (§26, §54) sur un portefeuille cible + ordres en attente.

Chaque scénario produit une PERTE ESTIMÉE (Money ≥ 0) et ses hypothèses. L'exposition retenue est la
plus défavorable entre « les achats en attente s'exécutent » et « les ventes en attente s'exécutent »
(cohérent avec ``okxq.risk.budgets``). « Risqué jusqu'au stop » est une ESTIMATION avec slippage de
crise : ce n'est jamais une perte maximale garantie (gaps, liquidation, ADL, déconnexion).

Scénarios : choc BTC / ETH (via bêtas), divergence d'une jambe, effondrement de liquidité (slippage ×k),
spread ×k, hausse de funding, dégradation du collatéral USDT, saut de prix, déconnexion (impossibilité
d'agir pendant N minutes), liquidation / ADL observable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from okxq.domain.money import ONE, ZERO, Money, dec

Z_CRISIS = Decimal("3")  # multiplicateur de volatilité en crise (hypothèse documentée)


@dataclass(frozen=True, slots=True)
class ScenarioParams:
    """Chocs paramétrés (fractions). Valeurs par défaut prudentes, à revalider par le propriétaire."""

    btc_shock: Decimal = Decimal("0.10")
    eth_shock: Decimal = Decimal("0.10")
    leg_divergence: Decimal = Decimal("0.05")
    liquidity_collapse_multiplier: Decimal = Decimal("5")
    spread_multiplier: Decimal = Decimal("3")
    funding_spike_per_period: Decimal = Decimal("0.003")
    funding_periods: int = 3
    collateral_haircut: Decimal = Decimal("0.05")
    price_jump: Decimal = Decimal("0.15")
    disconnection_minutes: int = 30
    adl_fraction: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        for name in (
            "btc_shock",
            "eth_shock",
            "leg_divergence",
            "liquidity_collapse_multiplier",
            "spread_multiplier",
            "funding_spike_per_period",
            "collateral_haircut",
            "price_jump",
            "adl_fraction",
        ):
            v = dec(getattr(self, name), field=name)
            if v < 0:
                raise ValueError(f"{name} négatif")
            object.__setattr__(self, name, v)
        if self.funding_periods < 0 or self.disconnection_minutes < 0:
            raise ValueError("périodes/minutes négatives")


@dataclass(frozen=True, slots=True)
class ScenarioPosition:
    """Exposition d'un instrument en notionnel USDT signé (+ ordres en attente) et ses paramètres de marché."""

    inst_id: str
    signed_notional: Decimal
    pending_buy_notional: Decimal = ZERO
    pending_sell_notional: Decimal = ZERO
    beta_btc: Decimal = ZERO
    beta_eth: Decimal = ZERO
    vol_per_minute: Decimal = ZERO  # écart-type du rendement par minute (fraction)
    spread_fraction: Decimal = ZERO
    slippage_fraction: Decimal = ZERO
    liquidation_distance: Decimal | None = None
    stop_distance: Decimal | None = None
    is_isolated_margin: bool = True

    def __post_init__(self) -> None:
        for name in (
            "signed_notional",
            "pending_buy_notional",
            "pending_sell_notional",
            "beta_btc",
            "beta_eth",
            "vol_per_minute",
            "spread_fraction",
            "slippage_fraction",
        ):
            object.__setattr__(self, name, dec(getattr(self, name), field=name))
        if self.pending_buy_notional < 0 or self.pending_sell_notional < 0:
            raise ValueError("notionnel en attente négatif")

    @property
    def if_buys_fill(self) -> Decimal:
        return self.signed_notional + self.pending_buy_notional

    @property
    def if_sells_fill(self) -> Decimal:
        return self.signed_notional - self.pending_sell_notional

    @property
    def worst_abs(self) -> Decimal:
        return max(abs(self.if_buys_fill), abs(self.if_sells_fill))

    def worst_directional(self, per_unit_move: Decimal) -> Decimal:
        """Perte pour un mouvement ±per_unit_move : pire des deux sens et des deux états d'exécution."""
        return max(
            ZERO,
            -self.if_buys_fill * per_unit_move,
            self.if_buys_fill * per_unit_move,
            -self.if_sells_fill * per_unit_move,
            self.if_sells_fill * per_unit_move,
        )


@dataclass(frozen=True, slots=True)
class ScenarioLoss:
    name: str
    loss: Money
    fraction_of_equity: Decimal
    assumptions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    equity: Money
    losses: list[ScenarioLoss]
    worst: ScenarioLoss
    risked_to_stop: Money
    risked_to_stop_is_estimate: bool = True  # jamais « perte maximale garantie »

    def loss(self, name: str) -> ScenarioLoss:
        for item in self.losses:
            if item.name == name:
                return item
        raise KeyError(name)

    def breaches(self, fraction_limit: Decimal) -> list[str]:
        return [item.name for item in self.losses if item.fraction_of_equity > fraction_limit]


def _beta_shock(positions: Sequence[ScenarioPosition], shock: Decimal, *, use_eth: bool) -> Decimal:
    """Perte sous un choc de l'indice ± shock : pire des deux sens, et pire état d'exécution GLOBAL."""
    buys = sum(((p.beta_eth if use_eth else p.beta_btc) * p.if_buys_fill for p in positions), ZERO)
    sells = sum(((p.beta_eth if use_eth else p.beta_btc) * p.if_sells_fill for p in positions), ZERO)
    return max(abs(buys), abs(sells)) * shock


def evaluate_scenarios(
    positions: Iterable[ScenarioPosition], equity: Money, params: ScenarioParams | None = None
) -> ScenarioReport:
    prm = params or ScenarioParams()
    pos = list(positions)
    if equity.amount <= 0:
        raise ValueError("equity non positive")
    ccy = equity.ccy
    losses: list[ScenarioLoss] = []

    def add(name: str, amount: Decimal, **assumptions: str) -> None:
        amt = max(amount, ZERO)
        losses.append(ScenarioLoss(name, Money(amt, ccy), amt / equity.amount, dict(assumptions)))

    add("btc_shock", _beta_shock(pos, prm.btc_shock, use_eth=False), shock=str(prm.btc_shock))
    add("eth_shock", _beta_shock(pos, prm.eth_shock, use_eth=True), shock=str(prm.eth_shock))
    add(
        "leg_divergence",
        max((p.worst_directional(prm.leg_divergence) for p in pos), default=ZERO),
        divergence=str(prm.leg_divergence),
        note="une seule jambe bouge contre nous",
    )
    add(
        "liquidity_collapse",
        sum((p.worst_abs * p.slippage_fraction * prm.liquidity_collapse_multiplier for p in pos), ZERO),
        multiplier=str(prm.liquidity_collapse_multiplier),
        note="coût de sortie intégrale avec slippage ×k",
    )
    add(
        "spread_widening",
        sum((p.worst_abs * (p.spread_fraction / 2) * prm.spread_multiplier for p in pos), ZERO),
        multiplier=str(prm.spread_multiplier),
    )
    add(
        "funding_spike",
        sum((p.worst_abs * prm.funding_spike_per_period * prm.funding_periods for p in pos), ZERO),
        per_period=str(prm.funding_spike_per_period),
        periods=str(prm.funding_periods),
        note="on suppose payer dans les deux sens",
    )
    add("collateral_haircut", equity.amount * prm.collateral_haircut, haircut=str(prm.collateral_haircut))
    jump = max((p.worst_directional(prm.price_jump) for p in pos), default=ZERO)
    add("price_jump", jump, jump=str(prm.price_jump), note="saut sans exécution possible du stop")
    minutes = Decimal(prm.disconnection_minutes)
    disconnection = sum(
        (p.worst_abs * p.vol_per_minute * minutes.sqrt() * Z_CRISIS for p in pos), ZERO
    )
    add(
        "disconnection",
        disconnection,
        minutes=str(prm.disconnection_minutes),
        z=str(Z_CRISIS),
        note="aucune action possible pendant la coupure",
    )
    liq = ZERO
    for p in pos:
        d = p.liquidation_distance
        if d is not None and d <= prm.price_jump:
            # Liquidation atteignable par le saut : la marge de la position est perdue (approximée par
            # notionnel × distance) plus le coût de liquidation forcée ; ADL : fraction fermée au pire prix.
            forced = p.worst_abs * (dec(d) + p.slippage_fraction * prm.liquidity_collapse_multiplier)
            adl = p.worst_abs * prm.adl_fraction * p.slippage_fraction * prm.liquidity_collapse_multiplier
            liq += forced + adl
    add("liquidation_adl", liq, jump=str(prm.price_jump), adl_fraction=str(prm.adl_fraction))

    risked = ZERO
    for p in pos:
        distance = p.stop_distance if p.stop_distance is not None else p.liquidation_distance
        if distance is None:
            distance = prm.price_jump  # sans stop ni liquidation connus : hypothèse de saut
        risked += p.worst_abs * (
            dec(distance) + p.slippage_fraction * prm.liquidity_collapse_multiplier + (p.spread_fraction / 2) * prm.spread_multiplier
        )
    worst = max(losses, key=lambda item: (item.loss.amount, item.name))
    return ScenarioReport(
        equity=equity,
        losses=losses,
        worst=worst,
        risked_to_stop=Money(max(risked, ZERO), ccy),
        risked_to_stop_is_estimate=True,
    )


def scenario_fraction_limit(daily_loss_halt_fraction: Decimal, multiplier: Decimal = ONE) -> Decimal:
    """Seuil de tolérance d'un scénario : par défaut la perte journalière de halt (×1)."""
    return dec(daily_loss_halt_fraction) * dec(multiplier)
