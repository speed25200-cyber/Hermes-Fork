"""Des prévisions aux edges (§21, §22, §37, §51).

Une ``Forecast`` donne un rendement attendu BRUT (``gross_mu``) et une incertitude sur un horizon.
Un ``EdgeEstimate`` y ajoute les composantes de coûts (fractions du notionnel, en ``Decimal``) fournies
par un modèle de coûts EXTERNE, et en déduit :

    net_edge   = expected_gross_return − Σ composantes non déjà incluses dans le prix
    edge_score = net_edge − coefficient × uncertainty

Le coefficient vient de la configuration (``strategy.uncertainty_penalty_coefficient``) : il est choisi
sur validation, jamais ajusté sur la période finale (§22). Anti-double comptage (D4) : les composantes
listées dans ``included_in_price`` sont celles que la convention de prix contient déjà (EXECUTABLE
contient le spread) ; elles ne sont pas soustraites une seconde fois. Une prévision MID ne peut rien
déclarer comme inclus.

Pour l'optimiseur, ``mu`` reste BRUT de coûts : les coûts d'entrée ne s'appliquent qu'à la VARIATION de
position (§51), pas à la position tenue. Les horizons sont vérifiés communs avant toute optimisation :
pas de mélange 1 min / 15 min, pas de division par la durée.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import HorizonMismatchError, UnitError
from okxq.domain.events import CostBasis, EdgeEstimate, Forecast, PortfolioInputs, PositionSide
from okxq.domain.ids import new_id
from okxq.domain.money import ZERO, dec, dec_from_float
from okxq.portfolio.covariance import CovarianceEstimate, assert_covariance_horizon

ENTRY_COMPONENTS: tuple[str, ...] = ("fees", "spread", "slippage", "impact")
KNOWN_COMPONENTS: tuple[str, ...] = (*ENTRY_COMPONENTS, "funding", "exit")
SIGNED_COMPONENTS: frozenset[str] = frozenset({"funding"})  # peut être négatif (funding reçu)

RETURN_PLACES = 10  # quantification des rendements et coûts (fractions du notionnel)

CostComponents = Callable[[str, PositionSide, Decimal, int], dict[str, Decimal]]


@runtime_checkable
class CostModel(Protocol):
    """Modèle de coûts externe : composantes en fraction du notionnel pour une taille et un horizon."""

    @property
    def included_in_price(self) -> Sequence[str]: ...

    def cost_components(
        self, instrument: str, side: PositionSide, contracts: Decimal, horizon_s: int
    ) -> dict[str, Decimal]: ...


def side_from_mu(gross_mu: float) -> PositionSide:
    if gross_mu > 0:
        return PositionSide.LONG
    if gross_mu < 0:
        return PositionSide.SHORT
    return PositionSide.FLAT


def check_cost_basis_consistency(cost_basis: CostBasis, included_in_price: Sequence[str]) -> None:
    """D4 / §37 : MID n'inclut rien ; EXECUTABLE inclut au moins le spread. Sinon double comptage."""
    included = set(included_in_price)
    unknown = included - set(KNOWN_COMPONENTS)
    if unknown:
        raise UnitError("composante inconnue déclarée incluse dans le prix", components=sorted(unknown))
    if cost_basis is CostBasis.MID and included:
        raise UnitError(
            "convention MID : aucune composante ne peut être déclarée incluse dans le prix",
            included=sorted(included),
        )
    if cost_basis is CostBasis.EXECUTABLE and "spread" not in included:
        raise UnitError("convention EXECUTABLE : le spread est dans le prix et doit être déclaré inclus")


def validate_components(components: Mapping[str, Decimal]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for name, value in components.items():
        if name not in KNOWN_COMPONENTS:
            raise UnitError("composante de coût inconnue", component=name)
        d = dec(value, field=f"components.{name}")
        if name not in SIGNED_COMPONENTS and d < 0:
            raise UnitError("composante de coût négative", component=name, value=str(d))
        out[name] = d
    return out


@dataclass(frozen=True, slots=True)
class EdgeBuilder:
    """Construit des ``EdgeEstimate`` à partir de ``Forecast`` et d'un modèle de coûts externe."""

    cost_components: CostComponents
    included_in_price: tuple[str, ...]
    uncertainty_penalty_coefficient: Decimal
    validity_s: int | None = None  # None → l'edge expire à la fin de son horizon

    def __post_init__(self) -> None:
        coef = dec(self.uncertainty_penalty_coefficient, field="uncertainty_penalty_coefficient")
        if coef < 0:
            raise UnitError("coefficient de pénalité d'incertitude négatif")
        object.__setattr__(self, "uncertainty_penalty_coefficient", coef)
        object.__setattr__(self, "included_in_price", tuple(self.included_in_price))
        if self.validity_s is not None and self.validity_s <= 0:
            raise UnitError("validity_s doit être strictement positif")

    @classmethod
    def from_cost_model(
        cls, model: CostModel, *, uncertainty_penalty_coefficient: Decimal, validity_s: int | None = None
    ) -> EdgeBuilder:
        return cls(
            cost_components=model.cost_components,
            included_in_price=tuple(model.included_in_price),
            uncertainty_penalty_coefficient=uncertainty_penalty_coefficient,
            validity_s=validity_s,
        )

    def build(self, forecast: Forecast, *, contracts: Decimal, edge_id: str | None = None) -> EdgeEstimate:
        check_cost_basis_consistency(forecast.cost_basis, self.included_in_price)
        side = side_from_mu(forecast.gross_mu)
        size = dec(contracts, field="contracts")
        if size < 0:
            raise UnitError("contracts doit être une taille absolue")
        raw = self.cost_components(forecast.instrument, side, size, forecast.horizon_s)
        components = validate_components(raw)
        gross = abs(dec_from_float(forecast.gross_mu, RETURN_PLACES, field="gross_mu"))
        if side is PositionSide.FLAT:
            gross = ZERO
        deducted = sum((v for k, v in components.items() if k not in self.included_in_price), ZERO)
        net = gross - deducted
        uncertainty = dec_from_float(forecast.uncertainty, RETURN_PLACES, field="uncertainty")
        penalty = self.uncertainty_penalty_coefficient * uncertainty
        validity = self.validity_s if self.validity_s is not None else forecast.horizon_s
        expires_at = ensure_utc(forecast.available_at) + timedelta(seconds=validity)
        return EdgeEstimate(
            edge_id=edge_id or new_id("edg"),
            forecast_id=forecast.forecast_id,
            instrument=forecast.instrument,
            side=side,
            horizon_s=forecast.horizon_s,
            cost_basis=forecast.cost_basis,
            expected_gross_return=gross,
            components=components,
            included_in_price=list(self.included_in_price),
            net_edge=net,
            uncertainty=uncertainty,
            uncertainty_penalty=penalty,
            edge_score=net - penalty,
            expires_at=expires_at,
        )

    def build_many(
        self, forecasts: Iterable[Forecast], *, contracts: Mapping[str, Decimal]
    ) -> list[EdgeEstimate]:
        return [self.build(f, contracts=contracts.get(f.instrument, ZERO)) for f in forecasts]


def is_expired(edge: EdgeEstimate, now: datetime) -> bool:
    return ensure_utc(now) >= edge.expires_at


def assert_common_horizon(edges: Sequence[EdgeEstimate], *, expected_horizon_s: int | None = None) -> int:
    """Refuse tout mélange d'horizons (§51). Retourne l'horizon commun."""
    horizons = sorted({e.horizon_s for e in edges})
    if not horizons:
        if expected_horizon_s is None:
            raise HorizonMismatchError("aucun edge : horizon indéterminé")
        return expected_horizon_s
    if len(horizons) > 1:
        raise HorizonMismatchError(
            "horizons mélangés dans une même optimisation : refusé, jamais converti", horizons=horizons
        )
    if expected_horizon_s is not None and horizons[0] != expected_horizon_s:
        raise HorizonMismatchError(
            "horizon des edges différent de l'horizon de l'optimiseur",
            edges_horizon_s=horizons[0],
            optimizer_horizon_s=expected_horizon_s,
        )
    return horizons[0]


def signed_gross_mu(edge: EdgeEstimate) -> float:
    """``mu`` BRUT signé pour l'optimiseur : jamais le net_edge (les coûts sont modélisés à part)."""
    return float(edge.expected_gross_return) * edge.side.sign


def entry_cost(edge: EdgeEstimate) -> Decimal:
    """Coût d'entrée par unité de VARIATION : composantes d'entrée non incluses dans le prix."""
    return sum(
        (edge.components.get(k, ZERO) for k in ENTRY_COMPONENTS if k not in edge.included_in_price), ZERO
    )


def funding_cost_long_unit(edge: EdgeEstimate) -> Decimal:
    """Coût de funding attendu pour une unité LONGUE (signé) : converti depuis le côté de l'edge."""
    f = edge.components.get("funding", ZERO)
    if edge.side is PositionSide.SHORT:
        return -f
    return f


def future_exit_cost(edge: EdgeEstimate) -> Decimal:
    """Coût futur de sortie par unité |w| : composante ``exit`` si fournie, sinon estimation conservatrice
    égale aux composantes d'entrée (un aller-retour coûte au moins deux fois l'entrée)."""
    if "exit" in edge.components:
        return edge.components["exit"]
    return sum((edge.components.get(k, ZERO) for k in ("fees", "spread", "slippage")), ZERO)


@dataclass(frozen=True, slots=True)
class PortfolioLimits:
    """Limites de §51 exprimées en multiples d'equity (issues de ``RiskCfg``/``ExecutionCfg``)."""

    gross_limit: float
    net_limit: float
    asset_limit: float
    cluster_limit: float
    btc_beta_limit: float
    eth_beta_limit: float
    turnover_limit: float
    margin_capacity: float
    constraints_version: str


def assemble_portfolio_inputs(
    edges: Sequence[EdgeEstimate],
    *,
    instruments: Sequence[str],
    w0: Mapping[str, Decimal | float],
    covariance: CovarianceEstimate,
    limits: PortfolioLimits,
    horizon_s: int,
    risk_aversion: float,
    beta_btc: Mapping[str, float],
    beta_eth: Mapping[str, float],
    clusters: Mapping[str, Sequence[str]],
    margin_requirement_per_unit: Mapping[str, float],
    liquidity_capacity: Mapping[str, float],
    equity_version: str,
    snapshot_id: str,
    now: datetime,
    asset_limit_overrides: Mapping[str, float] | None = None,
) -> PortfolioInputs:
    """Assemble ``PortfolioInputs`` sur un horizon COMMUN vérifié.

    Un instrument sans edge (détenu mais hors univers, ou edge expiré et retiré) reçoit ``mu = 0`` :
    l'optimiseur ne peut alors que le réduire (coûts) ou le garder (turnover), jamais l'augmenter
    utilement. Les edges expirés à ``now`` sont refusés.
    """
    assert_common_horizon(edges, expected_horizon_s=horizon_s)
    assert_covariance_horizon(covariance, horizon_s)
    by_inst: dict[str, EdgeEstimate] = {}
    for e in edges:
        if e.instrument in by_inst:
            raise UnitError("plusieurs edges pour un même instrument", instrument=e.instrument)
        if is_expired(e, now):
            raise UnitError("edge expiré", instrument=e.instrument, expires_at=e.expires_at.isoformat())
        if e.instrument not in instruments:
            raise UnitError("edge pour un instrument hors de la liste", instrument=e.instrument)
        by_inst[e.instrument] = e
    cov = covariance.reordered(instruments)
    mu: list[float] = []
    cost_buy: list[float] = []
    cost_sell: list[float] = []
    penalty: list[float] = []
    funding: list[float] = []
    exit_cost: list[float] = []
    for inst in instruments:
        edge = by_inst.get(inst)
        if edge is None:
            mu.append(0.0)
            cost_buy.append(0.0)
            cost_sell.append(0.0)
            penalty.append(0.0)
            funding.append(0.0)
            exit_cost.append(0.0)
            continue
        mu.append(signed_gross_mu(edge))
        c = float(entry_cost(edge))
        cost_buy.append(c)
        cost_sell.append(c)
        penalty.append(float(edge.uncertainty_penalty))
        funding.append(float(funding_cost_long_unit(edge)))
        exit_cost.append(float(future_exit_cost(edge)))
    overrides = asset_limit_overrides or {}
    return PortfolioInputs(
        instruments=list(instruments),
        mu=mu,
        sigma=cov.as_lists(),
        w0=[float(w0.get(inst, 0.0)) for inst in instruments],
        cost_buy=cost_buy,
        cost_sell=cost_sell,
        uncertainty_penalty=penalty,
        expected_funding_cost=funding,
        future_exit_cost=exit_cost,
        asset_limit=[
            min(limits.asset_limit, float(overrides.get(inst, limits.asset_limit))) for inst in instruments
        ],
        liquidity_capacity=[float(liquidity_capacity[inst]) for inst in instruments],
        beta_btc=[float(beta_btc.get(inst, 0.0)) for inst in instruments],
        beta_eth=[float(beta_eth.get(inst, 0.0)) for inst in instruments],
        clusters={k: list(v) for k, v in clusters.items()},
        horizon_s=horizon_s,
        risk_aversion=risk_aversion,
        gross_limit=limits.gross_limit,
        net_limit=limits.net_limit,
        btc_beta_limit=limits.btc_beta_limit,
        eth_beta_limit=limits.eth_beta_limit,
        cluster_limit=limits.cluster_limit,
        turnover_limit=limits.turnover_limit,
        margin_capacity=limits.margin_capacity,
        margin_requirement_per_unit=[float(margin_requirement_per_unit[inst]) for inst in instruments],
        equity_version=equity_version,
        snapshot_id=snapshot_id,
        constraints_version=limits.constraints_version,
    )
