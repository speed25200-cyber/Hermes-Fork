"""Risk Engine indépendant (§25, §53) : ``evaluate(intent, context) -> RiskDecision``.

Le modèle propose (``OrderIntent``), le Risk Engine décide (ALLOW / REDUCE / REJECT / FLATTEN) avec des
codes de raison stables (``okxq.domain.reasons``). La décision est liée au hash EXACT du payload
normalisé (``intent.payload_hash()``), à ``limits_version`` et ``position_version``, et expire à
``now + approval_ttl_ms``. Une décision ALLOW/REDUCE crée des réservations d'exposition pessimistes
(``ReservationStore``) libérées seulement sur état final observé (voir ``approvals``).

Contrôles (dans l'ordre) : leadership, horloge, halt courant, instrument négociable / sortie d'univers,
âge de l'intention, statut et âge de la réconciliation, equity réconciliée, fraîcheur des données
(cotation, état privé), carnet valide, connexions, spread, volatilité anormale, santé du modèle, cadence
d'ordres, perte journalière, drawdown, distance de liquidation, taille d'ordre et participation, puis
budgets multi-niveaux (brut, net, actif, cluster, direction, bêtas) et marge sur l'exposition
PESSIMISTE (positions + ordres ouverts + UNKNOWN + réservations + l'intention elle-même, T43).

Règle générale : une RÉDUCTION (``reduce_only``) n'est bloquée que par les contrôles « durs »
(leadership, horloge, cadence, instrument, expiration, hash) ; tous les autres ne bloquent que les
augmentations d'exposition. Une augmentation trop grande est RÉDUITE au plus grand nombre de contrats
admissible sur la grille de lot (bissection déterministe), ou refusée si ce nombre est nul.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from okxq.domain.clocks import Clock, ensure_utc
from okxq.domain.events import OrderIntent, RiskAction, RiskDecision, RiskEvent, Severity
from okxq.domain.ids import new_id
from okxq.domain.instruments import InstrumentSpec
from okxq.domain.money import ZERO, Money, Side, dec, round_down_to_step
from okxq.domain.orders import OrderState
from okxq.domain.reasons import ReasonCode
from okxq.risk.approvals import ReservationStore, reservation_for
from okxq.risk.budgets import (
    LimitSet,
    OpenOrderState,
    PositionState,
    ReservationView,
    build_exposure,
    evaluate_budgets,
    initial_margin_estimate,
    liquidation_distance,
)
from okxq.risk.kill_switch import HaltLevel

EventSink = Callable[[RiskEvent], None]


@dataclass(frozen=True, slots=True)
class MarketQuality:
    """Qualité de marché observée pour un instrument à l'instant de la décision."""

    quote_age_ms: int | None = None  # None = inconnu → traité comme périmé
    book_valid: bool = True
    relative_spread: Decimal | None = None
    volatility_ratio: Decimal | None = None  # vol récente / vol de référence
    max_contracts_by_participation: Decimal | None = None  # plafond calculé sur volume OBSERVÉ


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Tout ce que le Risk Engine regarde. Construit par le runtime, jamais par la stratégie."""

    now: datetime
    account_scope: str
    equity: Money
    equity_version: str
    position_version: str
    positions: Mapping[str, PositionState]
    open_orders: Sequence[OpenOrderState]
    specs: Mapping[str, InstrumentSpec]
    reference_prices: Mapping[str, Decimal]
    market_quality: Mapping[str, MarketQuality] = field(default_factory=dict)
    equity_reconciled: bool = True
    used_margin: Decimal | None = None
    leverages: Mapping[str, Decimal] = field(default_factory=dict)
    clusters: Mapping[str, Sequence[str]] = field(default_factory=dict)
    betas_btc: Mapping[str, Decimal] = field(default_factory=dict)
    betas_eth: Mapping[str, Decimal] = field(default_factory=dict)
    reduce_only_instruments: frozenset[str] = frozenset()
    private_state_age_ms: int | None = None
    public_stream_connected: bool = True
    private_stream_connected: bool = True
    model_healthy: bool = True
    model_health_reason: str | None = None
    clock_offset_ms: int = 0
    clock_reliable: bool = True
    reconciliation_ok: bool = True
    reconciliation_age_ms: int | None = None
    is_leader: bool = True
    orders_last_minute: int = 0
    daily_loss_fraction: Decimal | None = None
    drawdown_fraction: Decimal | None = None
    halt_level: HaltLevel = HaltLevel.NONE
    max_volatility_ratio: Decimal = Decimal("3")
    max_reconciliation_age_ms: int = 60_000


@dataclass(frozen=True, slots=True)
class Finding:
    reason: ReasonCode
    severity: Severity
    blocks_all: bool = False
    blocks_increase: bool = False
    cap_contracts: Decimal | None = None
    evidence: dict[str, str] = field(default_factory=dict)


class RiskEngine:
    """``RiskService`` (domain.protocols) : ``evaluate(intent)`` avec contexte fourni ou injecté."""

    def __init__(
        self,
        *,
        limits: LimitSet,
        clock: Clock,
        reservations: ReservationStore,
        context_provider: Callable[[], RiskContext] | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._reservations = reservations
        self._provider = context_provider
        self._sink = event_sink

    @property
    def limits(self) -> LimitSet:
        return self._limits

    async def evaluate(self, intent: OrderIntent, context: RiskContext | None = None) -> RiskDecision:
        return self.evaluate_sync(intent, context)

    def evaluate_sync(self, intent: OrderIntent, context: RiskContext | None = None) -> RiskDecision:
        ctx = context if context is not None else (self._provider() if self._provider else None)
        if ctx is None:
            raise ValueError("RiskEngine sans contexte : aucune décision possible")
        now = ensure_utc(ctx.now)
        increases = self._increases_exposure(intent, ctx)
        findings = self._run_checks(intent, ctx, increases)
        self._emit(findings, intent, ctx, now)
        return self._decide(intent, ctx, findings, increases, now)

    # --- décision --------------------------------------------------------------------------------------

    def _decide(
        self, intent: OrderIntent, ctx: RiskContext, findings: list[Finding], increases: bool, now: datetime
    ) -> RiskDecision:
        reasons = [f.reason.value for f in findings]
        intent_hash = intent.payload_hash()
        expires = now + timedelta(milliseconds=self._limits.approval_ttl_ms)
        common = dict(
            decision_id=new_id("dec"),
            intent_id=intent.intent_id,
            intent_hash=intent_hash,
            limits_version=self._limits.limits_version,
            position_version=ctx.position_version,
            created_at=now,
            expires_at=expires,
        )
        if increases and ctx.halt_level is HaltLevel.EMERGENCY_FLATTEN:
            return RiskDecision(
                action=RiskAction.FLATTEN,
                allowed_payload_hash=None,
                reason_codes=_dedupe([ReasonCode.EMERGENCY_FLATTEN.value, *reasons]),
                **common,
            )
        blocked = any(f.blocks_all or (increases and f.blocks_increase) for f in findings)
        if blocked:
            return RiskDecision(
                action=RiskAction.REJECT, allowed_payload_hash=None, reason_codes=_dedupe(reasons), **common
            )
        caps = [f.cap_contracts for f in findings if f.cap_contracts is not None]
        allowed = intent.contracts
        if caps:
            allowed = min(caps)
        spec = ctx.specs[intent.inst_id]
        allowed = round_down_to_step(allowed, spec.lot_size)
        if allowed < spec.min_size or allowed <= 0:
            return RiskDecision(
                action=RiskAction.REJECT,
                allowed_payload_hash=None,
                reason_codes=_dedupe([*reasons, ReasonCode.RISK_LIMIT.value]),
                **common,
            )
        if allowed < intent.contracts:
            reduced = intent.model_copy(update={"contracts": allowed})
            action = RiskAction.REDUCE
            allowed_hash = reduced.payload_hash()
        else:
            action = RiskAction.ALLOW
            allowed_hash = intent_hash
        price = dec(ctx.reference_prices[intent.inst_id])
        notional = allowed * spec.base_units_per_contract * price
        reservation = self._reservations.create(
            reservation_for(intent, contracts=allowed, notional_usdt=notional, now=now)
        )
        codes = _dedupe(reasons) or [ReasonCode.OK.value]
        return RiskDecision(
            action=action,
            allowed_payload_hash=allowed_hash,
            allowed_contracts=allowed,
            reservations={intent.inst_id: reservation.reservation_id},
            reason_codes=codes,
            **common,
        )

    # --- contrôles -------------------------------------------------------------------------------------

    @staticmethod
    def _increases_exposure(intent: OrderIntent, ctx: RiskContext) -> bool:
        """Une intention reduce_only qui réduit bien une position opposée n'augmente pas l'exposition."""
        if not intent.reduce_only:
            return True
        pos = ctx.positions.get(intent.inst_id)
        if pos is None or pos.signed_contracts == 0:
            return True
        return (intent.side is Side.SELL) != (pos.signed_contracts > 0)

    def _run_checks(self, intent: OrderIntent, ctx: RiskContext, increases: bool) -> list[Finding]:
        lim = self._limits
        out: list[Finding] = []
        now = ensure_utc(ctx.now)

        def hard(reason: ReasonCode, **ev: str) -> None:
            out.append(Finding(reason, Severity.CRITICAL, blocks_all=True, evidence=ev))

        def entry(reason: ReasonCode, severity: Severity = Severity.WARN, **ev: str) -> None:
            out.append(Finding(reason, severity, blocks_increase=True, evidence=ev))

        # 1. leadership, horloge
        if not ctx.is_leader:
            hard(ReasonCode.NOT_LEADER)
        if not ctx.clock_reliable or abs(ctx.clock_offset_ms) > lim.max_clock_offset_ms:
            hard(ReasonCode.CLOCK_DRIFT, offset_ms=str(ctx.clock_offset_ms))
        # 2. halt courant
        if ctx.halt_level.blocks_increases:
            if increases:
                entry(ctx.halt_level.reason_code, Severity.CRITICAL)
                out.append(Finding(ReasonCode.HALTED, Severity.CRITICAL, blocks_increase=True))
            else:
                out.append(Finding(ReasonCode.REDUCTION_ALLOWED_UNDER_HALT, Severity.INFO))
        # 3. instrument
        spec = ctx.specs.get(intent.inst_id)
        if spec is None or not spec.is_tradable or intent.inst_id not in ctx.reference_prices:
            hard(ReasonCode.INSTRUMENT_NOT_TRADABLE, inst_id=intent.inst_id)
            return out  # sans métadonnées, rien d'autre n'est calculable
        if intent.inst_id in ctx.reduce_only_instruments:
            entry(ReasonCode.UNIVERSE_EXIT_REDUCE_ONLY)
        if intent.reduce_only and increases:
            hard(ReasonCode.RISK_LIMIT, detail="reduce_only sans position opposée")
        # 4. âge de l'intention
        age_ms = (now - ensure_utc(intent.created_at)).total_seconds() * 1000
        if now >= intent.expires_at or age_ms > lim.max_order_intent_age_ms:
            hard(ReasonCode.INTENT_EXPIRED, age_ms=str(int(age_ms)))
        # 5. réconciliation et equity
        if lim.require_reconciliation_before_entries and (
            not ctx.reconciliation_ok
            or ctx.reconciliation_age_ms is None
            or ctx.reconciliation_age_ms > ctx.max_reconciliation_age_ms
        ):
            entry(ReasonCode.RECONCILIATION_PENDING)
        if not ctx.equity_reconciled:
            entry(ReasonCode.EQUITY_NOT_RECONCILED)
        # 6. fraîcheur et qualité des données
        mq = ctx.market_quality.get(intent.inst_id, MarketQuality())
        if mq.quote_age_ms is None or mq.quote_age_ms > lim.max_quote_age_ms:
            entry(ReasonCode.DATA_STALE, quote_age_ms=str(mq.quote_age_ms))
        if ctx.private_state_age_ms is None or ctx.private_state_age_ms > lim.max_private_state_age_ms:
            entry(ReasonCode.DATA_STALE, private_state_age_ms=str(ctx.private_state_age_ms))
        if not mq.book_valid:
            entry(ReasonCode.BOOK_INVALID)
        if not ctx.public_stream_connected or not ctx.private_stream_connected:
            entry(ReasonCode.CONNECTION_LOST)
        if mq.relative_spread is None or mq.relative_spread > lim.max_relative_spread:
            entry(ReasonCode.SPREAD_TOO_WIDE, spread=str(mq.relative_spread))
        if mq.volatility_ratio is not None and mq.volatility_ratio > ctx.max_volatility_ratio:
            entry(ReasonCode.VOLATILITY_ABNORMAL, ratio=str(mq.volatility_ratio))
        if not ctx.model_healthy:
            entry(ReasonCode.MODEL_UNHEALTHY, detail=ctx.model_health_reason or "")
        # 7. cadence
        if ctx.orders_last_minute >= lim.max_orders_per_minute:
            hard(ReasonCode.ORDER_RATE_LIMIT, count=str(ctx.orders_last_minute))
        # 8. pertes
        if ctx.daily_loss_fraction is not None and ctx.daily_loss_fraction >= lim.daily_loss_halt_fraction:
            entry(ReasonCode.DAILY_LOSS_LIMIT, Severity.CRITICAL, fraction=str(ctx.daily_loss_fraction))
        if ctx.drawdown_fraction is not None and ctx.drawdown_fraction >= lim.drawdown_review_fraction:
            entry(ReasonCode.DRAWDOWN_REVIEW, fraction=str(ctx.drawdown_fraction))
        # 9. distance de liquidation
        pos = ctx.positions.get(intent.inst_id)
        if pos is not None and pos.signed_contracts != 0:
            distance = liquidation_distance(pos)
            if distance < lim.liquidation_distance_min_fraction:
                entry(ReasonCode.LIQUIDATION_DISTANCE, Severity.CRITICAL, distance=format(distance, "f"))
        if not increases:
            return out
        # 10. taille d'ordre et participation
        price = dec(ctx.reference_prices[intent.inst_id])
        unit_notional = spec.base_units_per_contract * price
        max_order_contracts = (lim.max_order_equity_multiple * ctx.equity.amount) / unit_notional
        if intent.contracts > max_order_contracts:
            out.append(Finding(ReasonCode.ORDER_TOO_LARGE, Severity.WARN, cap_contracts=max_order_contracts))
        if mq.max_contracts_by_participation is not None and intent.contracts > mq.max_contracts_by_participation:
            out.append(
                Finding(ReasonCode.PARTICIPATION_LIMIT, Severity.WARN, cap_contracts=mq.max_contracts_by_participation)
            )
        # 11. budgets et marge sur l'exposition pessimiste
        cap = self._budget_cap(intent, ctx, spec)
        if cap is not None:
            if cap <= 0:
                entry(ReasonCode.RISK_LIMIT, Severity.WARN, detail="aucune augmentation admissible")
            elif cap < intent.contracts:
                out.append(Finding(ReasonCode.RISK_LIMIT, Severity.WARN, cap_contracts=cap))
        margin_cap = self._margin_cap(intent, ctx, spec)
        if margin_cap is not None:
            if margin_cap <= 0:
                entry(ReasonCode.MARGIN_INSUFFICIENT, Severity.WARN)
            elif margin_cap < intent.contracts:
                out.append(Finding(ReasonCode.MARGIN_INSUFFICIENT, Severity.WARN, cap_contracts=margin_cap))
        return out

    # --- plafonds --------------------------------------------------------------------------------------

    def _budgets_ok(self, intent: OrderIntent, ctx: RiskContext, contracts: Decimal) -> bool:
        extra = (
            [
                OpenOrderState(
                    client_order_id=intent.client_order_id,
                    inst_id=intent.inst_id,
                    side=intent.side,
                    remaining_contracts=contracts,
                    observed_state=OrderState.INTENT_CREATED,
                    reduce_only=intent.reduce_only,
                )
            ]
            if contracts > 0
            else []
        )
        snapshot = build_exposure(
            equity=ctx.equity,
            positions=ctx.positions,
            open_orders=ctx.open_orders,
            reservations=[
                ReservationView(r.inst_id, r.signed_contracts, r.reduce_only)
                for r in self._reservations.active(ctx.account_scope)
            ],
            specs=ctx.specs,
            reference_prices=ctx.reference_prices,
            extra_orders=extra,
        )
        breaches = evaluate_budgets(
            snapshot,
            self._limits,
            betas_btc=ctx.betas_btc or None,
            betas_eth=ctx.betas_eth or None,
            clusters=ctx.clusters or None,
        )
        return not breaches

    def _budget_cap(self, intent: OrderIntent, ctx: RiskContext, spec: InstrumentSpec) -> Decimal | None:
        """Plus grand nombre de contrats (grille lot) tel que tous les budgets tiennent ; None si tout passe."""
        if self._budgets_ok(intent, ctx, intent.contracts):
            return None
        return _bisect_lots(intent.contracts, spec.lot_size, lambda c: self._budgets_ok(intent, ctx, c))

    def _margin_cap(self, intent: OrderIntent, ctx: RiskContext, spec: InstrumentSpec) -> Decimal | None:
        price = dec(ctx.reference_prices[intent.inst_id])
        unit_notional = spec.base_units_per_contract * price
        leverage = ctx.leverages.get(intent.inst_id)
        pos = ctx.positions.get(intent.inst_id)
        if leverage is None and pos is not None:
            leverage = pos.leverage
        used = ctx.used_margin
        if used is None:
            used = sum(
                (
                    initial_margin_estimate(
                        abs(p.signed_contracts) * ctx.specs[k].base_units_per_contract * dec(ctx.reference_prices[k]),
                        ctx.leverages.get(k, p.leverage),
                    )
                    for k, p in ctx.positions.items()
                    if k in ctx.specs and k in ctx.reference_prices
                ),
                ZERO,
            )
        capacity = self._limits.effective_margin_capacity * ctx.equity.amount
        per_contract = initial_margin_estimate(unit_notional, leverage)
        needed = per_contract * intent.contracts
        if used + needed <= capacity:
            return None
        room = capacity - used
        if room <= 0 or per_contract <= 0:
            return ZERO
        return round_down_to_step(room / per_contract, spec.lot_size)

    # --- événements ------------------------------------------------------------------------------------

    def _emit(self, findings: list[Finding], intent: OrderIntent, ctx: RiskContext, now: datetime) -> None:
        if self._sink is None:
            return
        for f in findings:
            if f.severity is Severity.INFO:
                continue
            self._sink(
                RiskEvent(
                    event_id=new_id("rsk"),
                    severity=f.severity,
                    reason_code=f.reason.value,
                    affected_scope=f"{ctx.account_scope}:{intent.inst_id}",
                    evidence={"intent_id": intent.intent_id, **f.evidence},
                    requested_action="REJECT" if (f.blocks_all or f.blocks_increase) else "REDUCE",
                    created_at=now,
                )
            )


def _bisect_lots(max_contracts: Decimal, lot: Decimal, ok: Callable[[Decimal], bool]) -> Decimal:
    """Plus grand multiple de ``lot`` ≤ max_contracts tel que ``ok`` (prédicat monotone) ; 0 si aucun."""
    hi = int(round_down_to_step(max_contracts, lot) / lot)
    lo = 0
    if hi <= 0:
        return ZERO
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(Decimal(mid) * lot):
            lo = mid
        else:
            hi = mid - 1
    return Decimal(lo) * lot


def _dedupe(codes: list[str]) -> list[str]:
    out: list[str] = []
    for c in codes:
        if c not in out:
            out.append(c)
    return out


def reduced_intent(intent: OrderIntent, decision: RiskDecision) -> OrderIntent:
    """Reconstruit l'intention réduite dont le hash correspond à ``allowed_payload_hash`` (REDUCE)."""
    if decision.action is not RiskAction.REDUCE or decision.allowed_contracts is None:
        raise ValueError("reduced_intent ne s'applique qu'à une décision REDUCE")
    candidate = intent.model_copy(update={"contracts": decision.allowed_contracts})
    if candidate.payload_hash() != decision.allowed_payload_hash:
        raise ValueError("l'intention réduite ne correspond pas au hash autorisé")
    return candidate

