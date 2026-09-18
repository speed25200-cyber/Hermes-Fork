"""Budgets de risque à plusieurs niveaux (§25, §53).

Niveaux : compte (marge, distance de liquidation, perte journalière, drawdown), portefeuille (brut, net,
bêtas), instrument, cluster, direction (long / short), ordre (taille, participation) et minute (cadence).
Les limites sont dérivées de la configuration et portent une ``limits_version`` stable (hash des
valeurs) : toute approbation y est liée.

Exposition PESSIMISTE (T43) : les positions, les ordres ouverts ET les ordres à l'état UNKNOWN (ainsi que
les réservations actives) comptent comme potentiellement actifs. Deux ordres opposés non exécutés ne se
compensent JAMAIS : pour chaque instrument on retient le pire des deux mondes (« tous les achats
s'exécutent » vs « toutes les ventes s'exécutent »). Un ordre ``reduce_only`` ne peut pas ouvrir : sa
contribution est plafonnée par la position qu'il réduit.

Marge et liquidation : l'absence de prix de liquidation n'est PAS une distance infinie ; on estime
prudemment depuis le levier, et sans levier connu la distance vaut zéro (position considérée à risque).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal
from enum import StrEnum

from okxq.config.schema import AppConfig, ExecutionCfg, MarketDataCfg, RiskCfg, RuntimeCfg, UniverseCfg
from okxq.domain.ids import payload_hash
from okxq.domain.instruments import InstrumentSpec
from okxq.domain.money import ONE, ZERO, Money, Side, dec, dec_from_float
from okxq.domain.orders import OrderState, is_terminal
from okxq.domain.reasons import ReasonCode

FRACTION_PLACES = 12

# Estimation prudente de la distance de liquidation depuis le levier quand le prix de liquidation manque :
# distance ≈ (1/levier) × (1 − part de marge de maintenance). La part réelle dépend du palier OKX ; 0,8 est
# une hypothèse conservatrice documentée, jamais une garantie.
LIQUIDATION_MAINTENANCE_FACTOR = Decimal("0.8")


class LimitLevel(StrEnum):
    ACCOUNT = "account"
    PORTFOLIO = "portfolio"
    INSTRUMENT = "instrument"
    CLUSTER = "cluster"
    DIRECTION = "direction"
    ORDER = "order"
    MINUTE = "minute"


@dataclass(frozen=True, slots=True)
class LimitSet:
    """Limites en multiples d'equity (Decimal) ; ``limits_version`` = hash stable des valeurs."""

    max_gross_equity_multiple: Decimal
    max_abs_net_equity_multiple: Decimal
    max_asset_equity_multiple: Decimal
    max_cluster_gross_equity_multiple: Decimal
    max_directional_equity_multiple: Decimal
    max_abs_btc_beta_exposure: Decimal
    max_abs_eth_beta_exposure: Decimal
    max_margin_utilization: Decimal
    margin_buffer_fraction: Decimal
    liquidation_distance_min_fraction: Decimal
    daily_loss_halt_fraction: Decimal
    drawdown_review_fraction: Decimal
    max_order_equity_multiple: Decimal
    max_order_participation_fraction: Decimal
    max_depth_consumption_fraction: Decimal
    max_turnover_equity_fraction_per_decision: Decimal
    max_orders_per_minute: int
    approval_ttl_ms: int
    max_order_intent_age_ms: int
    max_quote_age_ms: int
    max_private_state_age_ms: int
    max_clock_offset_ms: int
    max_relative_spread: Decimal
    resume_stability_seconds: int
    require_reconciliation_before_entries: bool
    limits_version: str = field(default="")

    def __post_init__(self) -> None:
        if not self.limits_version:
            object.__setattr__(self, "limits_version", compute_limits_version(self))

    @property
    def effective_margin_capacity(self) -> Decimal:
        """Capacité de marge utilisable après buffer : ``max_util × (1 − buffer)``."""
        return self.max_margin_utilization * (ONE - self.margin_buffer_fraction)

    @classmethod
    def from_config(cls, cfg: AppConfig) -> LimitSet:
        return cls.from_sections(cfg.risk, cfg.execution, cfg.market_data, cfg.runtime, cfg.universe)

    @classmethod
    def from_sections(
        cls, risk: RiskCfg, execution: ExecutionCfg, market_data: MarketDataCfg, runtime: RuntimeCfg, universe: UniverseCfg
    ) -> LimitSet:
        def f(x: float) -> Decimal:
            return dec_from_float(float(x), FRACTION_PLACES)

        gross = f(risk.max_gross_equity_multiple)
        net = f(risk.max_abs_net_equity_multiple)
        return cls(
            max_gross_equity_multiple=gross,
            max_abs_net_equity_multiple=net,
            max_asset_equity_multiple=f(risk.max_asset_equity_multiple),
            max_cluster_gross_equity_multiple=f(risk.max_cluster_gross_equity_multiple),
            max_directional_equity_multiple=(gross + net) / 2,
            max_abs_btc_beta_exposure=f(risk.max_abs_btc_beta_exposure),
            max_abs_eth_beta_exposure=f(risk.max_abs_eth_beta_exposure),
            max_margin_utilization=f(risk.max_margin_utilization),
            margin_buffer_fraction=f(risk.margin_buffer_fraction),
            liquidation_distance_min_fraction=f(risk.liquidation_distance_min_fraction),
            daily_loss_halt_fraction=f(risk.daily_loss_halt_fraction),
            drawdown_review_fraction=f(risk.drawdown_review_fraction),
            max_order_equity_multiple=f(risk.max_asset_equity_multiple),
            max_order_participation_fraction=f(execution.max_order_participation_fraction),
            max_depth_consumption_fraction=f(execution.max_depth_consumption_fraction),
            max_turnover_equity_fraction_per_decision=f(execution.max_turnover_equity_fraction_per_decision),
            max_orders_per_minute=risk.max_orders_per_minute,
            approval_ttl_ms=risk.approval_ttl_ms,
            max_order_intent_age_ms=execution.max_order_intent_age_ms,
            max_quote_age_ms=market_data.max_quote_age_ms,
            max_private_state_age_ms=market_data.max_private_state_age_ms,
            max_clock_offset_ms=runtime.max_clock_offset_ms,
            max_relative_spread=f(universe.max_relative_spread),
            resume_stability_seconds=risk.resume_stability_seconds,
            require_reconciliation_before_entries=execution.require_reconciliation_before_entries,
        )


def compute_limits_version(limits: LimitSet) -> str:
    values: dict[str, object] = {}
    for f in fields(limits):
        if f.name == "limits_version":
            continue
        v = getattr(limits, f.name)
        values[f.name] = format(v, "f") if isinstance(v, Decimal) else v
    return "limits-" + payload_hash(values)[:16]


# --- état observé ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionState:
    inst_id: str
    signed_contracts: Decimal
    mark_price: Decimal | None = None
    liquidation_price: Decimal | None = None
    leverage: Decimal | None = None
    margin: Decimal | None = None
    version: int = 0


@dataclass(frozen=True, slots=True)
class OpenOrderState:
    client_order_id: str
    inst_id: str
    side: Side
    remaining_contracts: Decimal
    observed_state: OrderState
    reduce_only: bool = False

    @property
    def potentially_active(self) -> bool:
        """Tout état non terminal compte, UNKNOWN et CANCEL_REQUESTED compris (§53, T43)."""
        return not is_terminal(self.observed_state)


@dataclass(frozen=True, slots=True)
class ReservationView:
    inst_id: str
    signed_contracts: Decimal
    reduce_only: bool = False


@dataclass(frozen=True, slots=True)
class InstrumentExposure:
    inst_id: str
    position_contracts: Decimal
    pending_buy_contracts: Decimal
    pending_sell_contracts: Decimal
    if_buys_fill: Decimal  # contrats signés si tous les achats s'exécutent
    if_sells_fill: Decimal  # contrats signés si toutes les ventes s'exécutent
    reference_price: Decimal
    base_units_per_contract: Decimal

    def _notional(self, contracts: Decimal) -> Decimal:
        return contracts * self.base_units_per_contract * self.reference_price

    @property
    def pessimistic_abs_notional(self) -> Decimal:
        return max(abs(self._notional(self.if_buys_fill)), abs(self._notional(self.if_sells_fill)))

    @property
    def pessimistic_long_notional(self) -> Decimal:
        return max(self._notional(self.if_buys_fill), ZERO)

    @property
    def pessimistic_short_notional(self) -> Decimal:
        return max(-self._notional(self.if_sells_fill), ZERO)

    @property
    def signed_if_buys(self) -> Decimal:
        return self._notional(self.if_buys_fill)

    @property
    def signed_if_sells(self) -> Decimal:
        return self._notional(self.if_sells_fill)


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    equity: Money
    per_instrument: dict[str, InstrumentExposure]

    def _frac(self, amount: Decimal) -> Decimal:
        return amount / self.equity.amount

    @property
    def gross(self) -> Decimal:
        return self._frac(sum((e.pessimistic_abs_notional for e in self.per_instrument.values()), ZERO))

    @property
    def net_abs(self) -> Decimal:
        buys = sum((e.signed_if_buys for e in self.per_instrument.values()), ZERO)
        sells = sum((e.signed_if_sells for e in self.per_instrument.values()), ZERO)
        return self._frac(max(abs(buys), abs(sells)))

    @property
    def long(self) -> Decimal:
        return self._frac(sum((e.pessimistic_long_notional for e in self.per_instrument.values()), ZERO))

    @property
    def short(self) -> Decimal:
        return self._frac(sum((e.pessimistic_short_notional for e in self.per_instrument.values()), ZERO))

    def asset(self, inst_id: str) -> Decimal:
        e = self.per_instrument.get(inst_id)
        return ZERO if e is None else self._frac(e.pessimistic_abs_notional)

    def cluster(self, members: Iterable[str]) -> Decimal:
        return sum((self.asset(m) for m in members), ZERO)

    def beta(self, betas: Mapping[str, Decimal]) -> Decimal:
        """Exposition bêta pessimiste : pire des deux mondes achats/ventes."""
        buys = sum((betas.get(k, ZERO) * e.signed_if_buys for k, e in self.per_instrument.items()), ZERO)
        sells = sum((betas.get(k, ZERO) * e.signed_if_sells for k, e in self.per_instrument.items()), ZERO)
        return self._frac(max(abs(buys), abs(sells)))


def _capped_reduce_only(position: Decimal, side: Side, contracts: Decimal) -> Decimal:
    """Un ordre reduce-only ne peut réduire que la position opposée : contribution plafonnée, jamais d'ouverture."""
    if side is Side.SELL and position > 0:
        return min(contracts, position)
    if side is Side.BUY and position < 0:
        return min(contracts, -position)
    return ZERO


def build_exposure(
    *,
    equity: Money,
    positions: Mapping[str, PositionState],
    open_orders: Iterable[OpenOrderState],
    reservations: Iterable[ReservationView],
    specs: Mapping[str, InstrumentSpec],
    reference_prices: Mapping[str, Decimal],
    extra_orders: Iterable[OpenOrderState] = (),
) -> ExposureSnapshot:
    """Exposition pessimiste : positions + ordres potentiellement actifs + réservations (+ intention projetée)."""
    if equity.amount <= 0:
        raise ValueError("equity non positive : aucune exposition n'est admissible")
    buys: dict[str, Decimal] = {}
    sells: dict[str, Decimal] = {}
    pos_by_inst = {k: dec(v.signed_contracts) for k, v in positions.items()}
    insts: set[str] = set(pos_by_inst)

    def add(inst: str, side: Side, contracts: Decimal, reduce_only: bool) -> None:
        insts.add(inst)
        c = dec(contracts)
        if reduce_only:
            c = _capped_reduce_only(pos_by_inst.get(inst, ZERO), side, c)
        if side is Side.BUY:
            buys[inst] = buys.get(inst, ZERO) + c
        else:
            sells[inst] = sells.get(inst, ZERO) + c

    for order in open_orders:
        if order.potentially_active:
            add(order.inst_id, order.side, order.remaining_contracts, order.reduce_only)
    for r in reservations:
        side = Side.BUY if r.signed_contracts > 0 else Side.SELL
        add(r.inst_id, side, abs(r.signed_contracts), r.reduce_only)
    for order in extra_orders:
        add(order.inst_id, order.side, order.remaining_contracts, order.reduce_only)

    per: dict[str, InstrumentExposure] = {}
    for inst in sorted(insts):
        spec = specs.get(inst)
        price = reference_prices.get(inst)
        if spec is None or price is None:
            raise ValueError(f"instrument {inst} sans métadonnées ou sans prix de référence")
        p = pos_by_inst.get(inst, ZERO)
        b = buys.get(inst, ZERO)
        s = sells.get(inst, ZERO)
        per[inst] = InstrumentExposure(
            inst_id=inst,
            position_contracts=p,
            pending_buy_contracts=b,
            pending_sell_contracts=s,
            if_buys_fill=p + b,
            if_sells_fill=p - s,
            reference_price=dec(price),
            base_units_per_contract=spec.base_units_per_contract,
        )
    return ExposureSnapshot(equity=equity, per_instrument=per)


# --- marge et liquidation ----------------------------------------------------------------------------


def margin_utilization(used_margin: Decimal | None, equity: Money) -> Decimal | None:
    if used_margin is None:
        return None
    if equity.amount <= 0:
        return ONE
    return dec(used_margin) / equity.amount


def initial_margin_estimate(notional_abs: Decimal, leverage: Decimal | None) -> Decimal:
    """Marge initiale estimée ; sans levier connu, on suppose 1× (tout le notionnel) : prudent."""
    lev = dec(leverage) if leverage is not None else ONE
    if lev <= 0:
        lev = ONE
    return abs(notional_abs) / lev


def liquidation_distance(position: PositionState) -> Decimal:
    """Distance relative au prix de liquidation. Jamais infinie : sans prix, estimation depuis le levier ;
    sans levier, zéro (la position est traitée comme immédiatement à risque)."""
    if position.signed_contracts == 0:
        return ONE
    mark = position.mark_price
    liq = position.liquidation_price
    if mark is not None and mark > 0 and liq is not None and liq > 0:
        return abs(dec(mark) - dec(liq)) / dec(mark)
    if position.leverage is not None and position.leverage > 0:
        return (ONE / dec(position.leverage)) * LIQUIDATION_MAINTENANCE_FACTOR
    return ZERO


# --- évaluation des budgets ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Breach:
    level: LimitLevel
    reason: ReasonCode
    name: str
    used: Decimal
    limit: Decimal

    @property
    def excess(self) -> Decimal:
        return self.used - self.limit


def evaluate_budgets(
    snapshot: ExposureSnapshot,
    limits: LimitSet,
    *,
    betas_btc: Mapping[str, Decimal] | None = None,
    betas_eth: Mapping[str, Decimal] | None = None,
    clusters: Mapping[str, Iterable[str]] | None = None,
) -> list[Breach]:
    """Compare une exposition pessimiste aux limites de portefeuille/instrument/cluster/direction."""
    out: list[Breach] = []

    def check(level: LimitLevel, name: str, used: Decimal, limit: Decimal) -> None:
        if used > limit:
            out.append(Breach(level, ReasonCode.RISK_LIMIT, name, used, limit))

    check(LimitLevel.PORTFOLIO, "gross", snapshot.gross, limits.max_gross_equity_multiple)
    check(LimitLevel.PORTFOLIO, "net", snapshot.net_abs, limits.max_abs_net_equity_multiple)
    check(LimitLevel.DIRECTION, "long", snapshot.long, limits.max_directional_equity_multiple)
    check(LimitLevel.DIRECTION, "short", snapshot.short, limits.max_directional_equity_multiple)
    for inst in snapshot.per_instrument:
        check(LimitLevel.INSTRUMENT, f"asset:{inst}", snapshot.asset(inst), limits.max_asset_equity_multiple)
    for name, members in (clusters or {}).items():
        check(LimitLevel.CLUSTER, f"cluster:{name}", snapshot.cluster(members), limits.max_cluster_gross_equity_multiple)
    if betas_btc:
        check(LimitLevel.PORTFOLIO, "beta_btc", snapshot.beta(betas_btc), limits.max_abs_btc_beta_exposure)
    if betas_eth:
        check(LimitLevel.PORTFOLIO, "beta_eth", snapshot.beta(betas_eth), limits.max_abs_eth_beta_exposure)
    return out
