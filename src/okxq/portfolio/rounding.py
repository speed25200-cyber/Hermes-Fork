"""Poids → contrats → intentions d'ordre (§24, §45, T39, T40).

Règles :
- une AUGMENTATION de risque s'arrondit vers zéro sur la grille ``lot_size`` ; sous ``min_size`` elle
  devient zéro (pas d'ordre), jamais ``min_size`` ;
- une RÉDUCTION dont la taille tombe sous ``min_size`` est portée à ``min_size`` si la position le
  permet (on réduit un peu plus, jamais au-delà de la position) ; sinon le résidu est du DUST : il est
  laissé en place et rapporté, on n'ouvre JAMAIS une position opposée plus grosse pour le nettoyer ;
- après arrondi, TOUTES les contraintes (brute, nette, actif, bêtas, clusters, turnover, marge —
  exacte si un vérificateur est fourni —, liquidité) sont revérifiées ; si l'arrondi casse une limite
  (T40), le candidat est corrigé de façon déterministe SOUS contraintes (on rapproche la jambe la plus
  contributive de sa position actuelle, jamais au-delà de zéro pour un retournement) ou rejeté ;
- seuls les deltas utiles deviennent des ``OrderIntent`` (TTL, ``client_order_id`` durable via
  ``new_id``) : réduction (``reduce_only=True``) ou augmentation ; un passage long→short est SCINDÉ en
  une clôture reduce-only puis une ouverture séparée qui dépend de la première (T39) — chaque jambe
  est ensuite revérifiée séparément par le Risk Engine ;
- un target expiré est rejeté avant tout calcul.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from okxq.domain.clocks import Clock
from okxq.domain.errors import RoundingError, UnitError
from okxq.domain.events import OrderIntent, PortfolioInputs, PortfolioTarget
from okxq.domain.ids import new_id
from okxq.domain.instruments import (
    InstrumentSpec,
    raw_target_contracts,
    round_contracts_risk_reducing,
    validate_order_size,
)
from okxq.domain.money import (
    ONE,
    ZERO,
    Contracts,
    Money,
    Side,
    dec,
    dec_from_float,
    round_down_to_step,
    round_price_aggressive_within_limit,
    round_price_passive,
)
from okxq.domain.orders import OrderKind
from okxq.domain.reasons import ReasonCode
from okxq.portfolio.constraints import (
    FEASIBILITY_TOL,
    ConstraintReport,
    MarginChecker,
    verify_weights,
)
from okxq.portfolio.optimizer import assert_target_fresh

MAX_REPAIR_STEPS = 500

LegKind = Literal["reduce", "increase", "close", "open"]


@dataclass(frozen=True, slots=True)
class RoundingContext:
    equity: Money
    specs: Mapping[str, InstrumentSpec]
    reference_prices: Mapping[str, Decimal]
    current_contracts: Mapping[str, Decimal]  # signés, par instrument
    inputs: PortfolioInputs
    margin_checker: MarginChecker | None = None


@dataclass(frozen=True, slots=True)
class IntentPolicy:
    """Paramètres d'émission des intentions (issus d'``ExecutionCfg`` et de la décision courante)."""

    account_scope: str
    decision_id: str
    ttl_ms: int
    entry_order_type: OrderKind = OrderKind.POST_ONLY
    reduce_order_type: OrderKind = OrderKind.IOC
    reduce_limit_slippage: Decimal = Decimal("0.001")  # limite agressive tolérée pour une réduction

    def __post_init__(self) -> None:
        if self.ttl_ms <= 0:
            raise UnitError("ttl_ms doit être strictement positif")
        if self.entry_order_type is OrderKind.MARKET:
            raise UnitError("une entrée ne peut pas être un ordre market (§45)")
        slip = dec(self.reduce_limit_slippage, field="reduce_limit_slippage")
        if slip < 0 or slip >= 1:
            raise UnitError("reduce_limit_slippage hors [0, 1)")
        object.__setattr__(self, "reduce_limit_slippage", slip)


@dataclass(frozen=True, slots=True)
class Leg:
    inst_id: str
    kind: LegKind
    side: Side
    contracts: Decimal  # taille absolue
    reduce_only: bool
    from_contracts: Decimal
    to_contracts: Decimal


@dataclass(frozen=True, slots=True)
class RoundedPlan:
    target_id: str
    status: Literal["ok", "repaired", "rejected"]
    rounded_contracts: dict[str, Decimal]
    rounded_weights: dict[str, Decimal]
    report: ConstraintReport
    legs: list[Leg] = field(default_factory=list)
    intents: list[OrderIntent] = field(default_factory=list)
    sequencing: dict[str, list[str]] = field(default_factory=dict)  # intent_id → prérequis
    dust: dict[str, Decimal] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)
    repair_steps: int = 0

    @property
    def accepted(self) -> bool:
        return self.status != "rejected"


# --- conversions --------------------------------------------------------------------------------------


def contracts_to_weight(contracts: Decimal, spec: InstrumentSpec, price: Decimal, equity: Money) -> Decimal:
    return contracts * spec.base_units_per_contract * dec(price, field="price") / equity.amount


def weight_per_lot(spec: InstrumentSpec, price: Decimal, equity: Money) -> Decimal:
    return contracts_to_weight(spec.lot_size, spec, price, equity)


def _same_sign(a: Decimal, b: Decimal) -> bool:
    return a == 0 or b == 0 or (a > 0) == (b > 0)


def round_target_contracts(
    target_weight: Decimal, current: Decimal, spec: InstrumentSpec, price: Decimal, equity: Money
) -> tuple[Decimal, Decimal | None]:
    """Arrondit un poids cible en contrats signés. Retourne ``(cible arrondie, dust)``.

    - augmentation ou retournement : vers zéro (grille lot, min_size → 0) ;
    - réduction : la cible est arrondie vers zéro ; si le delta résultant est sous ``min_size`` mais
      que la position permet une réduction de ``min_size``, on réduit de ``min_size`` ; sinon la
      réduction n'est pas envoyable : la cible reste la position actuelle et le résidu est du dust.
    """
    raw = raw_target_contracts(target_weight, equity, spec, price)
    rounded = round_contracts_risk_reducing(raw, spec).value
    cur = dec(current, field="current")
    if cur == 0 or not _same_sign(rounded, cur) or abs(rounded) >= abs(cur):
        if cur != 0 and not _same_sign(rounded, cur):
            # Retournement : la jambe de clôture est |cur| ; l'ouverture est |rounded| (déjà vers zéro).
            return rounded, None
        if cur != 0 and abs(rounded) > abs(cur):
            # Augmentation : la partie ajoutée doit être un multiple du lot et ≥ min_size.
            add = round_down_to_step(abs(rounded) - abs(cur), spec.lot_size)
            if add < spec.min_size:
                return cur, None
            return cur + (add if cur > 0 else -add), None
        return rounded, None
    # Réduction stricte (même signe, |rounded| < |cur|).
    delta = round_down_to_step(abs(cur) - abs(rounded), spec.lot_size)
    if delta == 0:
        return cur, None
    if delta < spec.min_size:
        if abs(cur) >= spec.min_size:
            delta = spec.min_size
        else:
            return cur, abs(cur)  # dust : position sous le minimum, non réductible
    new_abs = abs(cur) - delta
    return (new_abs if cur > 0 else -new_abs), None


# --- réparation T40 -----------------------------------------------------------------------------------


def _weights_from_contracts(contracts: Mapping[str, Decimal], ctx: RoundingContext) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for inst in ctx.inputs.instruments:
        spec = ctx.specs[inst]
        out[inst] = contracts_to_weight(
            contracts.get(inst, ZERO), spec, ctx.reference_prices[inst], ctx.equity
        )
    return out


def _verify(
    contracts: Mapping[str, Decimal], ctx: RoundingContext
) -> tuple[ConstraintReport, dict[str, Decimal]]:
    weights = _weights_from_contracts(contracts, ctx)
    report = verify_weights(
        ctx.inputs,
        [float(weights[i]) for i in ctx.inputs.instruments],
        tol=FEASIBILITY_TOL,
        margin_checker=ctx.margin_checker,
    )
    return report, weights


def _contributors(
    name: str, contracts: Mapping[str, Decimal], weights: Mapping[str, Decimal], ctx: RoundingContext
) -> list[tuple[str, Decimal]]:
    """Jambes dont le rapprochement vers la position actuelle réduit la contrainte ``name``.

    Retourne ``[(inst, poids de la contribution par lot)]`` triés par contribution décroissante puis nom.
    """
    inputs = ctx.inputs
    idx = {inst: i for i, inst in enumerate(inputs.instruments)}
    out: list[tuple[str, Decimal]] = []
    w = weights
    net = sum(w.values(), ZERO)
    for inst in inputs.instruments:
        cur = dec(ctx.current_contracts.get(inst, ZERO))
        tgt = contracts.get(inst, ZERO)
        if tgt == cur:
            continue
        per_lot = weight_per_lot(ctx.specs[inst], ctx.reference_prices[inst], ctx.equity)
        moving_toward_zero = abs(tgt) > abs(cur) or not _same_sign(tgt, cur)
        contribution: Decimal | None = None
        if (
            name in ("gross", "margin", "margin_exact")
            or name.startswith("cluster:")
            or name.startswith("asset:")
        ):
            if name.startswith("asset:") and name.split(":", 1)[1] != inst:
                continue
            if name.startswith("cluster:") and inst not in inputs.clusters.get(name.split(":", 1)[1], []):
                continue
            if moving_toward_zero:
                contribution = per_lot * (
                    dec_from_float(float(inputs.margin_requirement_per_unit[idx[inst]]), 10)
                    if name.startswith("margin")
                    else ONE
                )
        elif name == "net":
            # Rapprocher tgt de cur change net de −sign(tgt − cur)·per_lot : utile si opposé au signe de net.
            direction = 1 if tgt > cur else -1
            if (net > 0 and direction > 0) or (net < 0 and direction < 0):
                contribution = per_lot
        elif name in ("beta_btc", "beta_eth"):
            betas = inputs.beta_btc if name == "beta_btc" else inputs.beta_eth
            beta = dec_from_float(float(betas[idx[inst]]), 10)
            exposure = sum(
                (dec_from_float(float(betas[idx[i]]), 10) * w[i] for i in inputs.instruments), ZERO
            )
            direction = 1 if tgt > cur else -1
            effect = beta * direction  # signe de la variation de l'exposition due à l'ordre
            if (exposure > 0 and effect > 0) or (exposure < 0 and effect < 0):
                contribution = abs(beta) * per_lot
        elif name == "turnover" or name.startswith("liquidity:"):
            if name.startswith("liquidity:") and name.split(":", 1)[1] != inst:
                continue
            contribution = per_lot
        if contribution is not None and contribution > 0:
            out.append((inst, contribution))
    out.sort(key=lambda item: (-item[1], item[0]))
    return out


def _step_toward_current(inst: str, contracts: dict[str, Decimal], ctx: RoundingContext, lots: int) -> bool:
    """Rapproche la cible de ``inst`` de sa position actuelle de ``lots`` lots, sans dépasser zéro lors d'un
    retournement (on n'ouvre pas l'autre côté) ni la position actuelle. Retourne False si rien ne bouge."""
    spec = ctx.specs[inst]
    cur = dec(ctx.current_contracts.get(inst, ZERO))
    tgt = contracts.get(inst, ZERO)
    if tgt == cur:
        return False
    step = spec.lot_size * lots
    if _same_sign(tgt, cur):
        gap = abs(tgt - cur)
        move = min(step, gap)
        new = tgt - move if tgt > cur else tgt + move
    else:
        # Retournement : la cible est de l'autre côté ; on la ramène vers zéro seulement.
        move = min(step, abs(tgt))
        new = tgt - move if tgt > 0 else tgt + move
    if new == tgt:
        return False
    if new != 0 and abs(new) < spec.min_size and new != cur:
        # Une ouverture résiduelle sous le minimum n'est pas envoyable : on la supprime.
        new = ZERO if not _same_sign(new, cur) or cur == 0 else cur
    contracts[inst] = new
    return True


def repair_under_constraints(
    contracts: dict[str, Decimal], ctx: RoundingContext
) -> tuple[dict[str, Decimal], ConstraintReport, dict[str, Decimal], int]:
    """T40 : corrige déterministement un candidat arrondi qui viole une contrainte, ou lève RoundingError."""
    work = dict(contracts)
    report, weights = _verify(work, ctx)
    steps = 0
    while not report.ok and steps < MAX_REPAIR_STEPS:
        if not report.finite:
            raise RoundingError("candidat non fini après arrondi", code=ReasonCode.ROUNDING_REJECTED.value)
        worst = max(report.violations, key=lambda n: (report.residuals[n], n))
        residual = dec_from_float(report.residuals[worst], 12)
        candidates = _contributors(worst, work, weights, ctx)
        moved = False
        for inst, per_lot in candidates:
            lots = int((residual / per_lot).to_integral_value(rounding=ROUND_CEILING)) if per_lot > 0 else 1
            if _step_toward_current(inst, work, ctx, max(1, lots)):
                moved = True
                break
        if not moved:
            raise RoundingError(
                "arrondi irréparable sous contraintes : candidat rejeté",
                code=ReasonCode.ROUNDING_REJECTED.value,
                constraint=worst,
                residual=str(residual),
            )
        steps += 1
        report, weights = _verify(work, ctx)
    if not report.ok:
        raise RoundingError(
            "réparation non convergée : candidat rejeté",
            code=ReasonCode.ROUNDING_REJECTED.value,
            violations=report.violations,
        )
    return work, report, weights, steps


# --- jambes et intentions -----------------------------------------------------------------------------


def legs_from_contracts(
    rounded: Mapping[str, Decimal], ctx: RoundingContext
) -> tuple[list[Leg], dict[str, Decimal]]:
    """Deltas utiles seulement ; un retournement est scindé en clôture puis ouverture (T39)."""
    legs: list[Leg] = []
    dust: dict[str, Decimal] = {}
    for inst in ctx.inputs.instruments:
        spec = ctx.specs[inst]
        cur = dec(ctx.current_contracts.get(inst, ZERO))
        tgt = rounded.get(inst, ZERO)
        if tgt == cur:
            continue
        if cur != 0 and not _same_sign(tgt, cur):
            close_side = Side.SELL if cur > 0 else Side.BUY
            legs.append(Leg(inst, "close", close_side, abs(cur), True, cur, ZERO))
            if tgt != 0:
                open_side = Side.BUY if tgt > 0 else Side.SELL
                legs.append(Leg(inst, "open", open_side, abs(tgt), False, ZERO, tgt))
            continue
        size = abs(tgt - cur)
        if abs(tgt) < abs(cur):
            side = Side.SELL if cur > 0 else Side.BUY
            if size < spec.min_size and abs(cur) < spec.min_size:
                dust[inst] = abs(cur)
                continue
            legs.append(Leg(inst, "reduce", side, size, True, cur, tgt))
        else:
            side = Side.BUY if tgt > 0 else Side.SELL
            if size < spec.min_size:
                continue
            legs.append(Leg(inst, "increase", side, size, False, cur, tgt))
    # Réductions et clôtures d'abord (T42) ; ordre déterministe par instrument.
    order = {"close": 0, "reduce": 1, "increase": 2, "open": 3}
    legs.sort(key=lambda leg: (order[leg.kind], leg.inst_id))
    return legs, dust


def _price_for(leg: Leg, spec: InstrumentSpec, mid: Decimal, policy: IntentPolicy) -> Decimal:
    if leg.reduce_only:
        limit = (
            mid * (ONE + policy.reduce_limit_slippage)
            if leg.side is Side.BUY
            else mid * (ONE - policy.reduce_limit_slippage)
        )
        return round_price_aggressive_within_limit(mid, spec.tick_size, leg.side, limit)
    return round_price_passive(mid, spec.tick_size, leg.side)


def intents_from_legs(
    legs: list[Leg], ctx: RoundingContext, policy: IntentPolicy, *, target_id: str, clock: Clock
) -> tuple[list[OrderIntent], dict[str, list[str]]]:
    now = clock.now_utc()
    intents: list[OrderIntent] = []
    sequencing: dict[str, list[str]] = {}
    close_intent_by_inst: dict[str, str] = {}
    for leg in legs:
        spec = ctx.specs[leg.inst_id]
        validate_order_size(_signed(leg), spec)
        price = _price_for(leg, spec, ctx.reference_prices[leg.inst_id], policy)
        intent = OrderIntent(
            intent_id=new_id("int"),
            decision_id=policy.decision_id,
            target_id=target_id,
            account_scope=policy.account_scope,
            inst_id=leg.inst_id,
            side=leg.side,
            contracts=leg.contracts,
            price_limit=price,
            order_type=policy.reduce_order_type if leg.reduce_only else policy.entry_order_type,
            reduce_only=leg.reduce_only,
            ttl_ms=policy.ttl_ms,
            reason=f"{leg.kind}:{format(leg.from_contracts, 'f')}->{format(leg.to_contracts, 'f')}",
            client_order_id=new_id("ord"),
            created_at=now,
            expires_at=now + timedelta(milliseconds=policy.ttl_ms),
        )
        intents.append(intent)
        if leg.kind == "close":
            close_intent_by_inst[leg.inst_id] = intent.intent_id
        elif leg.kind == "open":
            sequencing[intent.intent_id] = [close_intent_by_inst[leg.inst_id]]
    return intents, sequencing


def _signed(leg: Leg) -> Contracts:
    return Contracts(leg.contracts if leg.side is Side.BUY else -leg.contracts)


# --- point d'entrée -----------------------------------------------------------------------------------


def round_target(
    target: PortfolioTarget, ctx: RoundingContext, policy: IntentPolicy, *, clock: Clock
) -> RoundedPlan:
    """Pipeline complet : fraîcheur → contrats → revérification/réparation (T40) → jambes → intentions."""
    now = clock.now_utc()
    assert_target_fresh(target, now)
    if ctx.equity.amount <= 0:
        raise UnitError("equity non positive")
    reasons: list[str] = []
    rounded: dict[str, Decimal] = {}
    dust: dict[str, Decimal] = {}
    for inst in ctx.inputs.instruments:
        if inst not in ctx.specs or inst not in ctx.reference_prices:
            raise UnitError("instrument sans métadonnées ou sans prix de référence", inst_id=inst)
        weight = target.signed_weights.get(inst, ZERO)
        cur = dec(ctx.current_contracts.get(inst, ZERO))
        value, residual = round_target_contracts(
            weight, cur, ctx.specs[inst], ctx.reference_prices[inst], ctx.equity
        )
        rounded[inst] = value
        if residual is not None:
            dust[inst] = residual
    for inst in target.signed_weights:
        if inst not in ctx.inputs.instruments and target.signed_weights[inst] != 0:
            raise UnitError("poids cible pour un instrument hors des entrées", inst_id=inst)

    status: Literal["ok", "repaired", "rejected"] = "ok"
    steps = 0
    try:
        report, weights = _verify(rounded, ctx)
        if not report.ok:
            rounded, report, weights, steps = repair_under_constraints(rounded, ctx)
            status = "repaired"
            reasons.append(ReasonCode.ROUNDING_REPAIRED.value)
    except RoundingError as exc:
        report, weights = _verify(rounded, ctx)
        return RoundedPlan(
            target_id=target.target_id,
            status="rejected",
            rounded_contracts=rounded,
            rounded_weights=weights,
            report=report,
            dust=dust,
            reason_codes=[ReasonCode.ROUNDING_REJECTED.value, str(exc.context.get("constraint", ""))],
        )
    legs, leg_dust = legs_from_contracts(rounded, ctx)
    dust.update(leg_dust)
    if dust:
        reasons.append(ReasonCode.DUST_RESIDUAL.value)
    intents, sequencing = intents_from_legs(legs, ctx, policy, target_id=target.target_id, clock=clock)
    return RoundedPlan(
        target_id=target.target_id,
        status=status,
        rounded_contracts=rounded,
        rounded_weights=weights,
        report=report,
        legs=legs,
        intents=intents,
        sequencing=sequencing,
        dust=dust,
        reason_codes=reasons,
        repair_steps=steps,
    )


def weight_residual_norm(plan: RoundedPlan, target: PortfolioTarget) -> float:
    """Écart L1 entre les poids cibles et les poids arrondis (diagnostic)."""
    return float(sum(abs(plan.rounded_weights.get(k, ZERO) - v) for k, v in target.signed_weights.items()))
