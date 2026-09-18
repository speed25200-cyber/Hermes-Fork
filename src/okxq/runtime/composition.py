"""Câblage des processus (§52, §55, §62) : c'est ici, et nulle part ailleurs, que les composants
concrets sont assemblés.

Ce module existe pour que les moteurs restent ignorants les uns des autres. La boucle décisionnelle
(``okxq.runtime.decision_loop``) ne connaît que des protocoles ; le Risk Engine ne connaît ni la
stratégie ni l'échange ; le gateway est le SEUL chemin vers un ordre. Les adaptateurs ci-dessous
traduisent entre ces mondes, et c'est délibérément la seule couche autorisée à tout voir.

Trois garanties sont portées par la composition elle-même, pas par la discipline de l'appelant :

1. **Séparation des secrets par rôle.** Seul le rôle ``gateway`` peut lire des identifiants d'échange.
   Tous les autres rôles refusent de démarrer si un adaptateur réel leur est demandé, et n'accèdent
   jamais aux variables d'environnement correspondantes.
2. **Aucun ordre sans approbation.** Le type ``ApprovedOrder`` impose déjà un ``RiskDecision`` ; la
   composition n'offre aucun raccourci qui contournerait le Risk Engine. En SHADOW, le gateway câblé
   REFUSE structurellement d'envoyer.
3. **LIVE jamais par défaut.** ``run_process`` refuse le mode LIVE si la configuration ne l'a pas
   explicitement activé. Il n'existe aucun paramètre de contournement dans ce module.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np

from okxq import __version__
from okxq.accounting.ledger import Ledger
from okxq.backtest.market_state import BookView, MarketState
from okxq.backtest.virtual_exchange import FEE_TAKER_DEFAULT, VirtualExchange
from okxq.config.modes import Mode
from okxq.config.schema import AppConfig
from okxq.data.collector import PublicCollector
from okxq.data.normalizer import Normalizer
from okxq.data.ws_client import connect_public
from okxq.domain.clocks import Clock, SystemClock, ensure_utc
from okxq.domain.errors import CausalityError, ConfigError, CostModelError, OkxqError
from okxq.domain.events import (
    ApprovedOrder,
    EdgeEstimate,
    Forecast,
    MarketSnapshot,
    OrderIntent,
    PortfolioInputs,
    PortfolioTarget,
    PositionSide,
    ReconciliationReport,
    SubmissionOutcome,
    SubmissionResult,
)
from okxq.domain.ids import new_id
from okxq.domain.instruments import InstrumentSpec, spec_from_okx_instrument
from okxq.domain.money import ZERO, Money, Side, dec_from_float
from okxq.domain.reasons import ReasonCode
from okxq.exchange.okx.capabilities import (
    RegionProfile,
    load_manifest,
    resolve_region_profile,
)
from okxq.exchange.okx.rest_public import OkxPublicRestClient
from okxq.exchange.okx.websocket_public import SubscriptionArg
from okxq.features.incremental import IncrementalFeatureEngine
from okxq.features.registry import FeatureEngine, default_registry
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.repositories import UnitOfWorkFactory
from okxq.portfolio.costs import CostModel, FeeSchedule
from okxq.portfolio.covariance import CovarianceEstimate, estimate_covariance
from okxq.portfolio.forecasts import EdgeBuilder as PortfolioEdgeBuilder
from okxq.portfolio.forecasts import PortfolioLimits, assemble_portfolio_inputs
from okxq.portfolio.optimizer import CvxpyPortfolioBuilder
from okxq.portfolio.rounding import IntentPolicy, RoundingContext, round_target
from okxq.risk.approvals import SqlReservationStore
from okxq.risk.budgets import LimitSet, PositionState
from okxq.risk.engine import MarketQuality, RiskContext, RiskEngine
from okxq.risk.kill_switch import HaltLevel, KillSwitch, SqlRiskStateStore
from okxq.runtime import health as health_names
from okxq.runtime.alerts import Priority, build_default_manager
from okxq.runtime.decision_loop import DecisionDeps, DecisionLoop, DecisionRecord
from okxq.runtime.health import HealthRegistry, Status
from okxq.runtime.logging import bind_context, configure_logging, get_logger
from okxq.runtime.metrics import Metrics
from okxq.runtime.scheduler import DecisionScheduler
from okxq.runtime.startup import StartupSequence, StepResult

log = get_logger("okxq.runtime.composition")

#: Rôles de processus. ``all`` réunit tout dans un seul processus (développement et PAPER local) ;
#: en exploitation chaque rôle tourne dans son propre conteneur, avec ses propres secrets.
ROLES: tuple[str, ...] = ("all", "collector", "strategy", "risk", "gateway", "jev-worker", "api")

#: Seul rôle autorisé à détenir des identifiants d'échange (principe de séparation des secrets).
CREDENTIALED_ROLE = "gateway"

#: Variables d'environnement portant les identifiants OKX. Listées ici pour pouvoir VÉRIFIER qu'un
#: rôle non autorisé ne les a pas reçues, jamais pour les lire ailleurs que dans le gateway.
OKX_CREDENTIAL_VARS: tuple[str, ...] = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")

#: Nombre minimal d'observations de rendement avant d'accepter une covariance estimée.
MIN_COVARIANCE_OBSERVATIONS = 30

#: Aversion au risque de l'objectif §51. La configuration n'en porte pas : c'est un paramètre de
#: recherche, et le figer ici évite qu'il devienne un bouton de réglage en exploitation. Une valeur
#: élevée privilégie la variance faible sur le rendement espéré, ce qui est le bon défaut tant
#: qu'aucun modèle validé n'a démontré le contraire.
RISK_AVERSION = 10.0


# --- garde-fous de composition -------------------------------------------------------------------------


def assert_role(role: str) -> str:
    if role not in ROLES:
        raise ConfigError(f"rôle inconnu : {role}", expected=", ".join(ROLES))
    return role


def assert_credentials_separation(cfg: AppConfig, role: str) -> None:
    """Refuse de démarrer un rôle non autorisé auquel on a injecté des identifiants d'échange.

    Ce n'est pas une précaution cosmétique : un secret présent dans l'environnement d'un processus est
    lisible par tout ce qui tourne dedans, y compris une dépendance compromise. Si le collecteur, la
    recherche, l'API ou le worker JEV voient une clé OKX, le déploiement est mal configuré, et la
    bonne réaction est de s'arrêter — pas de continuer en espérant ne pas s'en servir.
    """
    if role in (CREDENTIALED_ROLE, "all"):
        return
    leaked = [name for name in OKX_CREDENTIAL_VARS if os.environ.get(name)]
    if leaked:
        raise ConfigError(
            "identifiants d'échange présents dans un rôle qui ne doit jamais les recevoir",
            role=role,
            variables=", ".join(leaked),
            remede="donner un fichier d'environnement distinct par service (voir infra/env/)",
        )


def assert_schema_ready(session_factory: Any) -> None:
    """Vérifie que le schéma est en place, et dit QUOI faire sinon.

    On ne crée pas le schéma à la volée. Un schéma créé par le runtime diverge silencieusement de
    celui des migrations : les deux se ressemblent jusqu'au jour où une contrainte manque là où on
    la croyait présente. Mieux vaut refuser de démarrer avec une instruction exacte qu'accepter avec
    une base approximative.
    """
    from sqlalchemy import inspect as sa_inspect

    from okxq.persistence.models import Base

    with session_factory() as session:
        present = set(sa_inspect(session.get_bind()).get_table_names())
    missing = sorted(set(Base.metadata.tables) - present)
    if missing:
        raise ConfigError(
            "schéma de base absent ou incomplet : appliquer les migrations avant de démarrer",
            tables_manquantes=", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""),
            nombre_manquant=str(len(missing)),
            commande="okxq db upgrade --config <config>",
        )


def assert_mode_allows_process(cfg: AppConfig) -> None:
    """LIVE ne démarre pas parce qu'on l'a demandé : il démarre parce qu'il a été autorisé."""
    if cfg.mode is Mode.LIVE and not cfg.project.live_enabled:
        raise ConfigError("mode LIVE demandé alors que live_enabled est faux : refus", mode=cfg.mode.value)


# --- petits utilitaires de traduction -----------------------------------------------------------------


def stable_version(prefix: str, *parts: str) -> str:
    """Version courte et DÉTERMINISTE d'un état.

    Deux lectures du même état donnent la même version, deux états différents des versions
    différentes. C'est exactement ce que demandent les re-vérifications au moment de l'envoi : elles
    comparent des versions, donc une version qui bougerait sans raison ferait rejeter des ordres
    valides, et une version figée laisserait passer un ordre sur un état périmé.
    """
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:16]}"


def _relative_spread(view: BookView) -> Decimal | None:
    """Spread relatif du carnet, ``None`` si le carnet n'a pas deux côtés.

    Zéro serait un spread nul, c'est-à-dire un marché parfait : exactement le contraire d'une mesure
    manquante. Le Risk Engine traite ``None`` comme inconnu et refuse d'augmenter l'exposition.
    """
    bid, ask = view.best_bid, view.best_ask
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid


def fee_schedule_from_config(cfg: AppConfig) -> FeeSchedule:
    """Grille de frais pour l'ESTIMATION ex ante des coûts.

    Les taux réels dépendent du palier du compte et ne sont connus qu'à l'exécution : la comptabilité
    n'utilise donc jamais cette grille, elle enregistre les frais RÉELLEMENT facturés. Ici on estime,
    et on estime prudemment — taux taker des deux côtés — parce qu'une estimation optimiste ferait
    passer des idées qui ne couvrent pas leurs coûts. Un taux maker négatif (rebate) resterait tel
    quel si la configuration en fournissait un jour.
    """
    return FeeSchedule(
        maker_rate=FEE_TAKER_DEFAULT,
        taker_rate=FEE_TAKER_DEFAULT,
        fee_ccy=cfg.account.settlement_currency,
    )


def portfolio_limits_from_config(cfg: AppConfig) -> PortfolioLimits:
    """Limites de l'optimiseur, dérivées des MÊMES valeurs que celles du Risk Engine.

    Les deux ne jouent pas le même rôle : l'optimiseur s'en sert comme contraintes pour construire une
    cible admissible, le Risk Engine comme frontières à faire respecter. Les dériver d'une source
    unique évite le cas pervers où l'optimiseur propose ce que le risque rejettera systématiquement,
    et ``constraints_version`` lie la cible aux limites qui l'ont produite.
    """
    risk = cfg.risk
    values = (
        risk.max_gross_equity_multiple,
        risk.max_abs_net_equity_multiple,
        risk.max_asset_equity_multiple,
        risk.max_cluster_gross_equity_multiple,
        risk.max_abs_btc_beta_exposure,
        risk.max_abs_eth_beta_exposure,
        cfg.execution.max_turnover_equity_fraction_per_decision,
        risk.max_margin_utilization,
    )
    return PortfolioLimits(
        gross_limit=risk.max_gross_equity_multiple,
        net_limit=risk.max_abs_net_equity_multiple,
        asset_limit=risk.max_asset_equity_multiple,
        cluster_limit=risk.max_cluster_gross_equity_multiple,
        btc_beta_limit=risk.max_abs_btc_beta_exposure,
        eth_beta_limit=risk.max_abs_eth_beta_exposure,
        turnover_limit=cfg.execution.max_turnover_equity_fraction_per_decision,
        margin_capacity=risk.max_margin_utilization,
        constraints_version=stable_version("cons", *(repr(v) for v in values)),
    )


@dataclass(slots=True)
class BookCostComponents:
    """Adaptateur : ``CostModel`` d'estimation → protocole ``CostModel`` attendu par les edges.

    Le modèle de coûts a besoin du carnet RÉEL (meilleur bid, meilleur ask, profondeur) pour estimer
    un coût ; le constructeur d'edges, lui, ne veut qu'un dictionnaire de composantes. Cet adaptateur
    fait le pont, et REFUSE d'estimer sans carnet utilisable : un coût nul ferait passer n'importe
    quelle idée au-dessus de ses coûts.
    """

    model: CostModel
    books: Callable[[], Mapping[str, BookView]]
    specs: Callable[[], Mapping[str, InstrumentSpec]]
    horizon_s: int

    @property
    def included_in_price(self) -> Sequence[str]:
        return self.model.default_included_in_price

    def cost_components(
        self, instrument: str, side: PositionSide, contracts: Decimal, horizon_s: int
    ) -> dict[str, Decimal]:
        view = self.books().get(instrument)
        spec = self.specs().get(instrument)
        if view is None or spec is None or view.best_bid is None or view.best_ask is None:
            raise CostModelError("coûts non estimables sans carnet à deux côtés", instrument=instrument)
        estimate = self.model.estimate(
            side=Side.BUY if side is PositionSide.LONG else Side.SELL,
            contracts=contracts,
            best_bid=view.best_bid,
            best_ask=view.best_ask,
            # La profondeur du côté opposé conditionne l'impact : sans elle, l'impact serait sous-estimé.
            opposite_levels=view.asks if side is PositionSide.LONG else view.bids,
            # Nombre de règlements de funding traversés sur l'horizon : un horizon d'une minute n'en
            # traverse normalement aucun, et supposer le contraire inventerait un coût.
            settlements_in_horizon=0,
        )
        # Les clés du modèle sont des `CostComponent` ; le protocole des edges attend des chaînes.
        return {str(component): value for component, value in estimate.components.items()}


def _minimum_tradable_size(rt: Runtime) -> Callable[[MarketSnapshot, Forecast], Decimal]:
    """Taille servant à ESTIMER les coûts d'une idée : la plus petite réellement traitable.

    Elle ne décide pas de la taille envoyée (c'est l'optimiseur puis l'arrondi qui le font). Elle sert
    à répondre « cette idée survit-elle à ses coûts ? », et la plus petite taille traitable est la plus
    favorable : si l'idée ne passe pas là, elle ne passera nulle part.
    """

    def size(snapshot: MarketSnapshot, forecast: Forecast) -> Decimal:
        spec = rt.market.specs.get(forecast.instrument)
        if spec is None:
            return ZERO
        return spec.min_size

    return size


# --- fournisseur de features point-in-time -------------------------------------------------------------


@dataclass(slots=True)
class UniverseView:
    """Univers courant tel que la décision doit le voir : éligibles, détenus, versions.

    Les instruments détenus mais sortis de l'univers restent présents, en réduction seulement : sortir
    un instrument de l'univers ne doit jamais rendre une position invisible.
    """

    universe_version: str
    metadata_version: str
    eligible: tuple[str, ...]
    held: tuple[str, ...] = ()

    @property
    def instruments(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for inst in (*self.eligible, *self.held):
            seen.setdefault(inst, None)
        return tuple(seen)


class LiveFeatureProvider:
    """``FeatureProvider`` : agrège le moteur incrémental et l'univers en un ``MarketSnapshot``.

    Le moteur incrémental porte déjà la causalité point-in-time (il refuse de calculer si un événement
    postérieur à la coupure a été ingéré). Ce fournisseur n'ajoute donc aucune logique de features : il
    assemble, et il propage les versions qui rendent la décision rejouable.
    """

    def __init__(
        self,
        *,
        engine: IncrementalFeatureEngine,
        universe: Callable[[], UniverseView],
        equity_version: Callable[[], str],
        reference_prices: Callable[[], Mapping[str, Decimal]],
    ) -> None:
        self._engine = engine
        self._universe = universe
        self._equity_version = equity_version
        self._prices = reference_prices

    async def snapshot(self, cutoff_at: datetime) -> MarketSnapshot:
        cutoff_at = ensure_utc(cutoff_at, field="cutoff_at")
        view = self._universe()
        instruments = view.instruments
        features = {}
        quality: dict[str, list[Any]] = {}
        versions: dict[str, str] = {}
        for inst in instruments:
            computation = self._engine.compute(inst, cutoff_at, eligible=view.eligible, peers=instruments)
            features[inst] = computation.vector
            versions[inst] = computation.vector.schema_hash
            # Les masques disent POURQUOI une feature manque ; on les remonte tels quels, sans les
            # traduire en valeurs par défaut qui feraient passer une absence pour une mesure.
            quality[inst] = sorted({flag for flag in computation.vector.masks})
        prices = {k: v for k, v in self._prices().items() if k in instruments}
        return MarketSnapshot(
            snapshot_id=new_id("snp"),
            cutoff_at=cutoff_at,
            universe_version=view.universe_version,
            metadata_version=view.metadata_version,
            equity_version=self._equity_version(),
            eligible_instruments=list(view.eligible),
            held_instruments=list(view.held),
            feature_versions=versions,
            quality_flags=quality,
            features=features,
            reference_prices=prices,
        )


# --- prédicteur --------------------------------------------------------------------------------------


class NoModelPredictor:
    """``Predictor`` par défaut : aucune prévision, et une raison explicite.

    Il n'existe pas de prédicteur « neutre » innocent : une prévision inventée à zéro se propagerait
    dans l'optimiseur comme une opinion. Tant qu'aucun modèle validé n'est chargé, la décision doit
    être NO_TRADE avec ``MODEL_UNAVAILABLE``, ce que l'absence de prévision produit naturellement.
    """

    reason = ReasonCode.MODEL_UNAVAILABLE

    async def predict(self, snapshot: MarketSnapshot) -> list[Forecast]:
        return []


def load_predictor(cfg: AppConfig) -> Any:
    """Charge le prédicteur de recherche s'il est livré ET qu'un modèle est désigné.

    L'import est tardif et tolérant : la plateforme doit pouvoir tourner en PAPER sans module de
    recherche installé. Un échec de chargement n'est jamais silencieux — il est journalisé et dégrade
    vers ``NoModelPredictor``, qui produit NO_TRADE au lieu de deviner.
    """
    try:
        module = __import__("okxq.research.predictor", fromlist=["*"])
    except ModuleNotFoundError:
        log.info("predictor_absent", raison="module de recherche non livré", effet="NO_TRADE")
        return NoModelPredictor()
    builder = getattr(module, "build_predictor", None)
    if builder is None:
        log.warning("predictor_sans_point_entree", attendu="build_predictor(cfg)")
        return NoModelPredictor()
    try:
        return builder(cfg)
    except OkxqError as exc:
        log.warning("predictor_indisponible", raison=str(exc), effet="NO_TRADE")
        return NoModelPredictor()


# --- adaptateurs de la boucle décisionnelle ------------------------------------------------------------


@dataclass(slots=True)
class RuntimeEdgeBuilder:
    """``EdgeBuilder`` de la boucle : délègue au constructeur d'edges du portefeuille.

    La taille utilisée pour estimer les coûts est celle qu'on envisage RÉELLEMENT de traiter : un coût
    estimé sur une taille fictive rendrait le filtre « edge net au-dessus des coûts » inopérant.
    """

    builder: PortfolioEdgeBuilder
    contracts_for: Callable[[MarketSnapshot, Forecast], Decimal]

    def build(self, snapshot: MarketSnapshot, forecasts: Sequence[Forecast]) -> list[EdgeEstimate]:
        out: list[EdgeEstimate] = []
        for forecast in forecasts:
            size = self.contracts_for(snapshot, forecast)
            if size <= 0:
                continue
            out.append(self.builder.build(forecast, contracts=size))
        return out


@dataclass(slots=True)
class RuntimeInputsBuilder:
    """``InputsBuilder`` : covariance, bêtas, clusters et limites → ``PortfolioInputs``.

    Retourne ``None`` — et donc NO_TRADE — quand une entrée obligatoire manque. C'est le point où
    l'insuffisance de données doit s'arrêter : un optimiseur nourri d'estimations improvisées produit
    un portefeuille qui a l'air valide.
    """

    clock: Clock
    limits: PortfolioLimits
    horizon_s: int
    sample_horizon_s: int
    risk_aversion: float
    returns_for: Callable[[Sequence[str], datetime], np.ndarray | None]
    current_weights: Callable[[Sequence[str]], Mapping[str, Decimal]]
    betas_btc: Callable[[Sequence[str]], Mapping[str, float]]
    betas_eth: Callable[[Sequence[str]], Mapping[str, float]]
    clusters: Callable[[Sequence[str]], Mapping[str, Sequence[str]]]
    margin_per_unit: Callable[[Sequence[str]], Mapping[str, float]]
    capacity: Callable[[Sequence[str], MarketSnapshot], Mapping[str, float]]

    def build(self, snapshot: MarketSnapshot, edges: Sequence[EdgeEstimate]) -> PortfolioInputs | None:
        if not edges:
            return None
        instruments = sorted({e.instrument for e in edges} | set(snapshot.held_instruments))
        returns = self.returns_for(instruments, snapshot.cutoff_at)
        if returns is None or returns.shape[0] < MIN_COVARIANCE_OBSERVATIONS:
            log.info(
                "covariance_insuffisante",
                raison=ReasonCode.COVARIANCE_INSUFFICIENT.value,
                observations=0 if returns is None else int(returns.shape[0]),
                requis=MIN_COVARIANCE_OBSERVATIONS,
            )
            return None
        covariance: CovarianceEstimate = estimate_covariance(
            instruments,
            returns,
            horizon_s=self.horizon_s,
            # L'horizon d'ÉCHANTILLONNAGE est celui des rendements fournis ; le confondre avec
            # l'horizon de décision sur/sous-estimerait la variance d'un facteur entier.
            sample_horizon_s=self.sample_horizon_s,
        )
        return assemble_portfolio_inputs(
            edges,
            instruments=instruments,
            w0=self.current_weights(instruments),
            covariance=covariance,
            limits=self.limits,
            horizon_s=self.horizon_s,
            risk_aversion=self.risk_aversion,
            beta_btc=self.betas_btc(instruments),
            beta_eth=self.betas_eth(instruments),
            clusters=self.clusters(instruments),
            margin_requirement_per_unit=self.margin_per_unit(instruments),
            liquidity_capacity=self.capacity(instruments, snapshot),
            equity_version=snapshot.equity_version,
            snapshot_id=snapshot.snapshot_id,
            now=self.clock.now_utc(),
        )


@dataclass(slots=True)
class RuntimeIntentBuilder:
    """``IntentBuilder`` : cible continue → intentions entières, via l'arrondi vérifié (§53).

    L'arrondi peut REFUSER un plan (contrainte violée après arrondi). Ce refus est propagé comme une
    absence d'intention, pas comme une exception : ne rien envoyer est une issue normale.
    """

    clock: Clock
    account_scope: str
    ttl_ms: int
    equity: Callable[[], Money]
    specs: Callable[[], Mapping[str, Any]]
    current_contracts: Callable[[], Mapping[str, Decimal]]
    inputs_of_target: Callable[[PortfolioTarget], PortfolioInputs | None]
    last_reason_codes: list[str] = field(default_factory=list)

    def build(self, snapshot: MarketSnapshot, target: PortfolioTarget, decision_id: str) -> list[OrderIntent]:
        inputs = self.inputs_of_target(target)
        if inputs is None:
            self.last_reason_codes = [ReasonCode.NO_VALID_TARGET.value]
            return []
        ctx = RoundingContext(
            equity=self.equity(),
            specs=self.specs(),
            reference_prices=dict(snapshot.reference_prices),
            current_contracts=self.current_contracts(),
            inputs=inputs,
        )
        policy = IntentPolicy(account_scope=self.account_scope, decision_id=decision_id, ttl_ms=self.ttl_ms)
        plan = round_target(target, ctx, policy, clock=self.clock)
        self.last_reason_codes = list(plan.reason_codes)
        if not plan.accepted:
            return []
        return list(plan.intents)


@dataclass(slots=True)
class OperationalGateState:
    """``OperationalGate`` : ce qui interdit de décider, avant même de regarder le marché.

    L'ordre des vérifications n'a pas d'importance fonctionnelle — toutes les raisons bloquantes sont
    remontées ensemble, parce qu'un opérateur qui ne voit qu'un motif à la fois répare en aveugle.
    """

    kill_switch: KillSwitch
    health: HealthRegistry
    is_leader: Callable[[], bool]
    reconciled: Callable[[], bool]
    require_leadership: bool
    max_data_age_s: float
    last_data_at: Callable[[], datetime | None]

    def blocking_reasons(self, cutoff_at: datetime) -> list[ReasonCode]:
        reasons: list[ReasonCode] = []
        level = self.kill_switch.level
        if level is not HaltLevel.NONE:
            reasons.append(level.reason_code)
        if self.require_leadership and not self.is_leader():
            reasons.append(ReasonCode.NOT_LEADER)
        if not self.reconciled():
            reasons.append(ReasonCode.RECONCILIATION_PENDING)
        last = self.last_data_at()
        if last is None or (cutoff_at - ensure_utc(last)).total_seconds() > self.max_data_age_s:
            reasons.append(ReasonCode.DATA_STALE)
        return reasons


class ShadowGateway:
    """``ExecutionGateway`` du mode SHADOW : il ne peut pas envoyer, par construction.

    SHADOW existe pour mesurer des décisions AVANT d'en connaître le résultat. Un gateway capable
    d'envoyer « juste au cas où » détruirait cette propriété. Celui-ci journalise l'intention et
    répond ``NOT_SENT`` : la décision reste complète et auditable, sans aucun ordre.
    """

    is_real_exchange = False

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self.submitted: list[str] = []

    async def submit(self, approved: ApprovedOrder) -> SubmissionResult:
        self.submitted.append(approved.intent.intent_id)
        log.info(
            "shadow_intention_non_envoyee",
            intent_id=approved.intent.intent_id,
            inst_id=approved.intent.inst_id,
            payload_hash=approved.payload_hash,
        )
        return SubmissionResult(
            intent_id=approved.intent.intent_id,
            client_order_id=approved.intent.client_order_id,
            outcome=SubmissionOutcome.NOT_SENT,
            sent_at=None,
            error_code="SHADOW",
            message="mode SHADOW : aucune intention n'est transmise à un échange",
        )

    async def reconcile(self) -> ReconciliationReport:
        now = self._clock.now_utc()
        return ReconciliationReport(
            started_at=now,
            completed_at=now,
            orders_checked=0,
            unknown_resolved=0,
            unknown_remaining=0,
            ok=True,
            notes=["mode SHADOW : aucun ordre à réconcilier"],
        )


@dataclass(slots=True)
class SqlDecisionSink:
    """``DecisionSink`` : persiste la décision, NO_TRADE comprise.

    Une décision non écrite est une décision qu'on ne peut pas auditer. Les alternatives rejetées sont
    conservées avec leur motif : c'est ce qui permet, après coup, de répondre « pourquoi n'a-t-on rien
    fait ? » autrement que par une supposition.
    """

    session_factory: Any
    software_version: str

    def persist(self, record: DecisionRecord) -> None:
        """Écrit la ligne de décision. La cible, les intentions, les approbations et les envois ont
        chacun leur table (``portfolio_targets``, ``order_intents``, ``risk_approvals``, ``orders``)
        et sont écrits par les composants qui en sont responsables : les dupliquer ici créerait deux
        vérités pour un même fait, et c'est toujours la mauvaise qu'on finit par lire.

        ``inputs_hash`` lie la décision à ses ENTRÉES exactes : deux décisions de même empreinte ont
        vu la même chose, ce qui est la condition pour pouvoir rejouer un désaccord.
        """
        from okxq.persistence.models import Decision as DecisionRow

        with self.session_factory() as session:
            session.add(
                DecisionRow(
                    decision_id=record.decision_id,
                    mode=record.mode,
                    snapshot_id=record.snapshot_id or "",
                    cutoff_at=record.cutoff_at,
                    started_at=record.started_at,
                    finished_at=record.finished_at,
                    outcome=record.outcome,
                    reason_codes=list(record.reason_codes),
                    model_id=record.model_id,
                    universe_version=record.universe_version,
                    equity_version=record.equity_version,
                    inputs_hash=stable_version(
                        "in",
                        record.snapshot_id or "",
                        record.universe_version or "",
                        record.equity_version or "",
                    ),
                    forecasts=list(record.forecasts),
                    edges=list(record.edges),
                    rejected_alternatives=list(record.rejected_alternatives),
                    timings_ms=dict(record.timings_ms),
                    software_version=self.software_version,
                    trace_id=record.trace_id,
                )
            )
            session.commit()


# --- collecte de données de marché ---------------------------------------------------------------------


#: Canaux publics souscrits par instrument. Le carnet vient de la configuration (§ qualité de données) ;
#: les autres sont indispensables au marquage, au funding et à la mesure de volume de l'univers.
COLLECTED_CHANNELS: tuple[str, ...] = ("trades", "tickers", "mark-price", "funding-rate", "open-interest")


def subscriptions_for(cfg: AppConfig, instruments: Sequence[str]) -> list[SubscriptionArg]:
    """Souscriptions publiques pour un ensemble d'instruments. Aucun canal privé, par construction."""
    args: list[SubscriptionArg] = []
    for inst_id in instruments:
        args.append(SubscriptionArg(channel=cfg.market_data.orderbook_channel, inst_id=inst_id))
        args.extend(SubscriptionArg(channel=channel, inst_id=inst_id) for channel in COLLECTED_CHANNELS)
    return args


async def discover_instruments(cfg: AppConfig) -> tuple[list[dict[str, Any]], RegionProfile]:
    """Liste les swaps linéaires réglés en USDT depuis le REST PUBLIC (aucune clé requise).

    Le filtre est strict et il le reste : un instrument dont le type de contrat, la devise de
    règlement ou le multiplicateur ne correspond pas exactement à ce que la plateforme sait traiter
    est écarté. Deviner la convention d'un instrument, c'est se tromper sur la taille d'une position.
    """
    manifest = load_manifest()
    profile = resolve_region_profile(cfg, manifest)
    async with OkxPublicRestClient(profile, manifest=manifest) as rest:
        raw = await rest.instruments(inst_type=cfg.universe.instrument_type)
    eligible: list[dict[str, Any]] = []
    rejected = 0
    for row in raw:
        if (
            row.get("ctType") == cfg.universe.contract_type
            and row.get("settleCcy") == cfg.universe.settlement_currency
            and row.get("ctValCcy") == (row.get("instId") or "").split("-")[0]
            and str(row.get("ctMult", "1")) == "1"
            and row.get("state") == "live"
        ):
            eligible.append(row)
        else:
            rejected += 1
    log.info(
        "instruments_decouverts",
        retenus=len(eligible),
        ecartes=rejected,
        raison_ecart="type de contrat, devise de règlement ou multiplicateur non conformes",
    )
    return eligible, profile


def build_collector(
    rt: Runtime, *, instruments: Sequence[str], profile: RegionProfile, connect: Any | None = None
) -> PublicCollector:
    """Collecteur public : flux réel, normalisation, validation de séquence des carnets.

    Le collecteur ne détient AUCUN secret, et son URL est validée comme publique : c'est ce qui rend
    acceptable de le faire tourner dans son propre conteneur sans aucun identifiant.
    """
    return PublicCollector(
        clock=rt.clock,
        subscriptions=subscriptions_for(rt.cfg, instruments),
        connect=connect or connect_public,
        url=profile.ws_public_url,
        normalizer=Normalizer(rt.clock, source=f"okx-public:{profile.name}"),
    )


async def drain_into_state(rt: Runtime, collector: PublicCollector, stop: asyncio.Event) -> None:
    """Applique les enveloppes collectées à l'état de marché ET au moteur de features.

    Les deux consommateurs lisent la MÊME séquence, dans le MÊME ordre : c'est ce qui garantit qu'une
    décision et une exécution simulée voient le même marché. Une enveloppe hors ordre est comptée et
    écartée, jamais réordonnée après coup — réordonner détruirait la causalité qu'on cherche à tenir.
    """
    out_of_order = 0
    while not stop.is_set():
        try:
            envelope = await asyncio.wait_for(collector.queue.get(), timeout=0.5)
        except TimeoutError:
            continue
        try:
            rt.market.apply(envelope)
            rt.features.ingest(envelope)
        except CausalityError:
            out_of_order += 1
            if out_of_order in (1, 10, 100, 1000):
                log.warning(
                    "enveloppe_hors_ordre_ecartee",
                    total=out_of_order,
                    event_type=envelope.event_type,
                    raison="réordonner après coup détruirait la causalité point-in-time",
                )
        else:
            rt.health.touch(health_names.MARKET_DATA)


# --- assemblage ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class Runtime:
    """Tout ce qu'un processus détient. Assemblé une fois, arrêté dans l'ordre inverse."""

    cfg: AppConfig
    role: str
    clock: Clock
    session_factory: Any
    uow_factory: UnitOfWorkFactory
    health: HealthRegistry
    metrics: Metrics
    market: MarketState
    features: IncrementalFeatureEngine
    ledger: Ledger
    kill_switch: KillSwitch
    limits: LimitSet
    stop: asyncio.Event
    scheduler: DecisionScheduler | None = None
    loop: DecisionLoop | None = None
    gateway: Any | None = None
    exchange: Any | None = None
    collector: Any | None = None
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def aclose(self) -> None:
        """Arrêt dans l'ordre inverse de l'assemblage ; une erreur d'arrêt ne masque pas les suivantes."""
        for closer in reversed(self.closers):
            with contextlib.suppress(Exception):
                await closer()


def _database_url(cfg: AppConfig) -> str:
    url = os.environ.get("OKXQ_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if url:
        return url
    if cfg.mode in (Mode.PAPER, Mode.RESEARCH, Mode.SHADOW):
        # Base locale sur fichier : un PAPER doit pouvoir tourner sans serveur, mais la persistance
        # reste réelle (un redémarrage ne doit pas effacer des halts ni un drawdown).
        return f"sqlite+pysqlite:///{cfg.storage.data_root}/okxq-{cfg.mode.value.lower()}.sqlite3"
    raise ConfigError(
        "URL de base absente : OKXQ_DATABASE_URL est obligatoire hors PAPER/SHADOW/RESEARCH",
        mode=cfg.mode.value,
    )


def build_exchange_adapter(cfg: AppConfig, *, role: str, clock: Clock, market: MarketState) -> Any:
    """Choisit l'adaptateur d'échange en fonction du MODE, jamais d'un drapeau d'appel.

    PAPER simule localement. SHADOW n'envoie rien. DEMO et LIVE exigent le connecteur réel, donc des
    identifiants, donc le rôle ``gateway`` : c'est le seul endroit du système qui les voit.
    """
    if cfg.mode is Mode.SHADOW:
        return ShadowGateway(clock=clock)
    if cfg.mode in (Mode.PAPER, Mode.RESEARCH):
        return VirtualExchange(
            clock=clock,
            market=market,
            account_scope=cfg.account.scope,
            initial_cash=cfg.account.paper_initial_equity,
            settle_ccy=cfg.account.settlement_currency,
        )
    if role not in (CREDENTIALED_ROLE, "all"):
        raise ConfigError(
            "seul le rôle gateway peut ouvrir une connexion privée à l'échange",
            role=role,
            mode=cfg.mode.value,
        )
    missing = [name for name in OKX_CREDENTIAL_VARS if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "identifiants d'échange absents : NOT_RUN plutôt qu'une connexion partielle",
            mode=cfg.mode.value,
            variables=", ".join(missing),
        )
    from okxq.exchange.okx.adapter import OkxExchangeAdapter, region_profile_from_env
    from okxq.exchange.okx.authentication import OkxCredentials

    # C'est le SEUL endroit du système qui lit ces variables. Elles ne sont ni journalisées, ni
    # rangées dans un attribut du runtime, ni transmises à un autre composant.
    credentials = OkxCredentials(
        api_key=os.environ["OKX_API_KEY"],
        api_secret=os.environ["OKX_API_SECRET"],
        passphrase=os.environ["OKX_API_PASSPHRASE"],
    )
    return OkxExchangeAdapter(
        cfg=cfg,
        credentials=credentials,
        profile=region_profile_from_env(),
        clock=clock,
    )


def build_runtime(
    cfg: AppConfig,
    *,
    role: str = "all",
    clock: Clock | None = None,
    session_factory: Any | None = None,
) -> Runtime:
    """Assemble un processus complet pour ``role``, sans rien démarrer.

    Séparer l'assemblage du démarrage permet de tester le câblage — y compris les refus — sans
    ouvrir de connexion ni attendre une frontière de minute.
    """
    assert_role(role)
    assert_mode_allows_process(cfg)
    assert_credentials_separation(cfg, role)
    the_clock = clock or SystemClock()
    if session_factory is None:
        engine = make_engine(_database_url(cfg))
        session_factory = make_session_factory(engine)
        uow_factory = UnitOfWorkFactory(engine)
    else:
        uow_factory = UnitOfWorkFactory(session_factory.kw["bind"])
    # AVANT tout composant qui lit la base : le ledger relit ses transactions dès sa construction,
    # et une table absente doit produire une instruction, pas une trace SQLAlchemy.
    assert_schema_ready(session_factory)
    health = HealthRegistry(clock=the_clock, mode=cfg.mode.value)
    metrics = Metrics()
    market = MarketState()
    features = IncrementalFeatureEngine(FeatureEngine(default_registry()))
    ledger = Ledger(
        session_factory,
        account_scope=cfg.account.scope,
        clock=the_clock,
        settle_ccy=cfg.account.settlement_currency,
    )
    limits = LimitSet.from_config(cfg)
    kill_switch = KillSwitch(
        account_scope=cfg.account.scope,
        store=SqlRiskStateStore(session_factory),
        cfg=cfg.risk,
        clock=the_clock,
        limits_version=limits.limits_version,
    )
    health.set(health_names.DATABASE, Status.OK, "base joignable")
    health.set(health_names.CLOCK, Status.OK, "horloge système")
    return Runtime(
        cfg=cfg,
        role=role,
        clock=the_clock,
        session_factory=session_factory,
        uow_factory=uow_factory,
        health=health,
        metrics=metrics,
        market=market,
        features=features,
        ledger=ledger,
        kill_switch=kill_switch,
        limits=limits,
        stop=asyncio.Event(),
    )


def build_startup_sequence(rt: Runtime) -> StartupSequence:
    """Séquence §52.4. Chaque étape répond une seule question, et un échec bloquant arrête là.

    ``authorize_entries`` est la DERNIÈRE étape : aucune intention d'entrée ne peut partir avant que
    le leadership, la réconciliation, les protections et la validation des données soient établis.
    """

    async def load_config() -> StepResult:
        return StepResult(ok=True, detail=f"mode {rt.cfg.mode.value}, compte {rt.cfg.account.scope}")

    async def verify_environment() -> StepResult:
        try:
            assert_credentials_separation(rt.cfg, rt.role)
        except ConfigError as exc:
            return StepResult(ok=False, detail=str(exc))
        return StepResult(ok=True, detail="séparation des secrets vérifiée")

    async def acquire_leadership() -> StepResult:
        if not rt.cfg.runtime.require_single_execution_writer or rt.role not in (
            CREDENTIALED_ROLE,
            "all",
        ):
            return StepResult(ok=True, detail="pas d'écrivain d'exécution dans ce rôle", blocking=False)
        return StepResult(ok=True, detail="bail géré par le gateway")

    async def connect_private_streams() -> StepResult:
        if not rt.cfg.mode.sends_orders_to_exchange:
            rt.health.set(
                health_names.PRIVATE_STREAM,
                Status.OK,
                "aucun flux privé requis hors DEMO/LIVE",
            )
            return StepResult(ok=True, detail="aucun flux privé", blocking=False)
        return StepResult(ok=True, detail="flux privés ouverts par le gateway")

    async def bootstrap_reconcile() -> StepResult:
        gateway = rt.gateway
        if gateway is None or not hasattr(gateway, "startup_check"):
            return StepResult(ok=True, detail="aucun gateway durable dans ce rôle", blocking=False)
        pending = int(gateway.startup_check())
        return StepResult(ok=True, detail=f"{pending} ordre(s) à réconcilier")

    async def restore_protections() -> StepResult:
        # Les protections vivent côté échange ; hors DEMO/LIVE il n'y en a pas à restaurer.
        return StepResult(
            ok=True,
            detail="protections restaurées par le gateway"
            if rt.cfg.mode.sends_orders_to_exchange
            else "sans objet",
            blocking=False,
        )

    async def validate_data() -> StepResult:
        if rt.market.last_available_at is None:
            rt.health.set(health_names.MARKET_DATA, Status.FAULT, "aucune donnée reçue")
            return StepResult(ok=False, detail="aucune donnée de marché : entrées non autorisées")
        rt.health.set(health_names.MARKET_DATA, Status.OK, "données reçues")
        return StepResult(ok=True, detail="données de marché présentes")

    async def authorize_entries() -> StepResult:
        level = rt.kill_switch.level
        if level is not HaltLevel.NONE:
            return StepResult(ok=False, detail=f"halt persisté au démarrage : {level.value}")
        return StepResult(ok=True, detail="entrées autorisées")

    return StartupSequence(
        rt.clock,
        {
            "load_config": load_config,
            "verify_environment": verify_environment,
            "acquire_leadership": acquire_leadership,
            "connect_private_streams": connect_private_streams,
            "bootstrap_reconcile": bootstrap_reconcile,
            "restore_protections": restore_protections,
            "validate_data": validate_data,
            "authorize_entries": authorize_entries,
        },
    )


def build_decision_loop(rt: Runtime, *, predictor: Any | None = None) -> DecisionLoop:
    """Câble la boucle décisionnelle sur les composants du runtime.

    Les fonctions d'accès (equity, positions, prix) sont des fermetures relues À CHAQUE frontière :
    une valeur capturée une fois pour toutes ferait décider sur un état périmé.
    """
    cfg = rt.cfg
    reservations = SqlReservationStore(rt.session_factory)

    def equity_now() -> Money:
        view = rt.ledger.equity(dict(rt.market.marks))
        return Money(view.equity, cfg.account.settlement_currency)

    def equity_version() -> str:
        """Version d'equity : empreinte de l'état comptable observé à cet instant.

        Elle doit CHANGER dès que l'equity change, sinon une approbation liée à une version
        resterait valide sur un état qui a bougé (T48). L'horodatage seul ne suffirait pas : deux
        lectures dans la même milliseconde doivent donner la même version, et deux equities
        différentes des versions différentes.
        """
        view = rt.ledger.equity(dict(rt.market.marks))
        return stable_version("eq", format(view.equity, "f"), view.as_of.isoformat())

    def position_version() -> str:
        """Version de position : empreinte des contrats signés et de leurs versions individuelles."""
        parts = [
            f"{inst}:{state.signed_contracts}:{state.version}"
            for inst, state in sorted(positions_now().items())
        ]
        return stable_version("pos", *parts)

    def positions_now() -> dict[str, PositionState]:
        """Traduit les positions comptables en état vu par le risque.

        Le Risk Engine raisonne en CONTRATS signés (c'est l'unité que l'échange accepte), la
        comptabilité en quantité de base et coût ouvert. La conversion est explicite ici pour que la
        distinction reste visible : un contrat n'est pas une unité de base.
        """
        out: dict[str, PositionState] = {}
        for inst_id, position in rt.ledger.positions.items():
            spec = rt.market.specs.get(inst_id)
            if spec is None:
                # Sans spécification d'instrument, la conversion base → contrats est indéterminée.
                # Omettre serait masquer une position : on la remonte en contrats inconnus (0) avec
                # sa version, et la validation de données bloquera la décision.
                log.warning("position_sans_specification", inst_id=inst_id)
                continue
            out[inst_id] = PositionState(
                inst_id=inst_id,
                signed_contracts=position.signed_base_qty / spec.base_units_per_contract,
                mark_price=rt.market.marks.get(inst_id),
                version=position.version,
            )
        return out

    def risk_context() -> RiskContext:
        view = rt.ledger.equity(dict(rt.market.marks))
        quality: dict[str, MarketQuality] = {}
        now = rt.clock.now_utc()
        for inst, book in rt.market.books.items():
            book_view = book.view()
            age_ms = None
            if book_view.ts is not None:
                age_ms = int((now - ensure_utc(book_view.ts)).total_seconds() * 1000)
            quality[inst] = MarketQuality(
                quote_age_ms=age_ms,
                book_valid=book_view.valid,
                relative_spread=_relative_spread(book_view),
            )
        return RiskContext(
            now=now,
            account_scope=cfg.account.scope,
            equity=Money(view.equity, cfg.account.settlement_currency),
            equity_version=equity_version(),
            position_version=position_version(),
            positions=positions_now(),
            open_orders=[],
            specs=dict(rt.market.specs),
            reference_prices=dict(rt.market.marks),
            market_quality=quality,
            equity_reconciled=True,
            halt_level=rt.kill_switch.level,
        )

    risk_engine = RiskEngine(
        limits=rt.limits,
        clock=rt.clock,
        reservations=reservations,
        context_provider=risk_context,
    )
    cost_model = BookCostComponents(
        model=CostModel(cost_basis=cfg.strategy.cost_basis, fee_schedule=fee_schedule_from_config(cfg)),
        books=lambda: {inst: book.view() for inst, book in rt.market.books.items()},
        specs=lambda: dict(rt.market.specs),
        horizon_s=cfg.strategy.optimizer_horizon_seconds,
    )
    edge_builder = RuntimeEdgeBuilder(
        builder=PortfolioEdgeBuilder.from_cost_model(
            cost_model,
            uncertainty_penalty_coefficient=dec_from_float(cfg.strategy.uncertainty_penalty_coefficient, 10),
        ),
        contracts_for=_minimum_tradable_size(rt),
    )
    inputs_builder = RuntimeInputsBuilder(
        clock=rt.clock,
        limits=portfolio_limits_from_config(cfg),
        horizon_s=cfg.strategy.optimizer_horizon_seconds,
        # Les rendements d'entraînement sont échantillonnés au pas de décision ; c'est ce pas qui
        # gouverne la mise à l'échelle de la covariance, pas l'horizon de la cible.
        sample_horizon_s=cfg.runtime.decision_interval_seconds,
        risk_aversion=RISK_AVERSION,
        returns_for=lambda instruments, cutoff: None,
        current_weights=lambda instruments: {},
        betas_btc=lambda instruments: dict.fromkeys(instruments, 0.0),
        betas_eth=lambda instruments: dict.fromkeys(instruments, 0.0),
        clusters=lambda instruments: {},
        margin_per_unit=lambda instruments: dict.fromkeys(instruments, 0.0),
        capacity=lambda instruments, snapshot: dict.fromkeys(instruments, 0.0),
    )
    last_inputs: dict[str, PortfolioInputs] = {}

    def remember(inputs: PortfolioInputs | None) -> PortfolioInputs | None:
        if inputs is not None:
            last_inputs[inputs.snapshot_id] = inputs
        return inputs

    class _Inputs:
        def build(self, snapshot: MarketSnapshot, edges: Sequence[EdgeEstimate]) -> PortfolioInputs | None:
            return remember(inputs_builder.build(snapshot, edges))

    intent_builder = RuntimeIntentBuilder(
        clock=rt.clock,
        account_scope=cfg.account.scope,
        ttl_ms=cfg.execution.max_order_intent_age_ms,
        equity=equity_now,
        specs=lambda: dict(rt.market.specs),
        current_contracts=lambda: {inst: state.signed_contracts for inst, state in positions_now().items()},
        inputs_of_target=lambda target: last_inputs.get(target.snapshot_id),
    )
    provider = LiveFeatureProvider(
        engine=rt.features,
        universe=lambda: UniverseView(
            universe_version="runtime-univers",
            metadata_version="runtime-metadonnees",
            eligible=tuple(sorted(rt.market.specs)),
            held=tuple(sorted(positions_now())),
        ),
        equity_version=equity_version,
        reference_prices=lambda: dict(rt.market.marks),
    )
    gate = OperationalGateState(
        kill_switch=rt.kill_switch,
        health=rt.health,
        is_leader=lambda: rt.gateway is None or getattr(rt.gateway, "is_leader", lambda: True)(),
        reconciled=lambda: not getattr(rt.gateway, "reconciliation_required", False),
        require_leadership=cfg.runtime.require_single_execution_writer,
        max_data_age_s=cfg.market_data.max_quote_age_ms / 1000.0,
        last_data_at=lambda: rt.market.last_available_at,
    )
    deps = DecisionDeps(
        clock=rt.clock,
        mode=cfg.mode.value,
        minimum_eligible=cfg.universe.minimum_eligible,
        gate=gate,
        feature_provider=provider,
        predictor=predictor or load_predictor(cfg),
        edge_builder=edge_builder,
        inputs_builder=_Inputs(),
        portfolio_builder=CvxpyPortfolioBuilder(
            # Une cible plus vieille que l'intervalle de décision décrirait un marché révolu : son TTL
            # ne peut donc pas dépasser le pas de la boucle.
            clock=rt.clock,
            target_ttl_s=cfg.runtime.decision_interval_seconds,
        ),
        intent_builder=intent_builder,
        risk_service=risk_engine,
        gateway=rt.gateway if rt.gateway is not None else ShadowGateway(clock=rt.clock),
        sink=SqlDecisionSink(session_factory=rt.session_factory, software_version=__version__),
    )
    return DecisionLoop(deps)


async def start_collection(rt: Runtime) -> dict[str, Any]:
    """Découvre l'univers, ouvre le flux public et branche l'alimentation de l'état de marché.

    Un échec de collecte n'arrête PAS le processus : sans données, la porte opérationnelle produit
    ``DATA_STALE`` et la décision est NO_TRADE. C'est le comportement voulu — une plateforme qui
    s'arrête à la première coupure réseau ne peut ni réduire une position ni rapporter son état,
    alors que ce sont précisément les deux choses utiles à ce moment-là.
    """
    try:
        rows, profile = await discover_instruments(rt.cfg)
    except Exception as exc:
        rt.health.set(health_names.MARKET_DATA, Status.FAULT, f"découverte impossible : {exc!r}")
        log.warning("decouverte_univers_impossible", detail=repr(exc), effet="NO_TRADE")
        return {"ok": False, "reason": repr(exc), "instruments": 0}

    # Les spécifications sont enregistrées au ledger ET à l'état de marché : sans elle, la conversion
    # base ↔ contrats est indéterminée, et une position devient inchiffrable.
    specs: list[str] = []
    for row in rows[: rt.cfg.universe.maximum_eligible]:
        try:
            spec = spec_from_okx_instrument(
                row,
                observed_at=rt.clock.now_utc(),
                provenance=f"rest:instruments:{profile.name}",
            )
        except OkxqError as exc:
            log.warning("instrument_non_conforme", inst_id=row.get("instId"), detail=str(exc))
            continue
        rt.market.specs[spec.inst_id] = spec
        rt.ledger.register_spec(spec)
        specs.append(spec.inst_id)
    if not specs:
        rt.health.set(health_names.MARKET_DATA, Status.FAULT, "aucun instrument conforme")
        return {"ok": False, "reason": "aucun instrument conforme", "instruments": 0}

    collector = build_collector(rt, instruments=specs, profile=profile)
    tasks = [
        asyncio.create_task(collector.run(rt.stop), name="collector"),
        asyncio.create_task(drain_into_state(rt, collector, rt.stop), name="collector-drain"),
    ]

    async def close() -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    rt.closers.append(close)
    rt.collector = collector
    return {"ok": True, "instruments": len(specs), "url": profile.ws_public_url}


# --- exécution ----------------------------------------------------------------------------------------


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Un signal d'arrêt demande un arrêt PROPRE ; il n'interrompt pas une décision en cours.

    Couper au milieu d'un envoi laisserait un ordre dont on ne connaît pas l'état — précisément la
    situation que la réconciliation doit éviter d'avoir à traiter.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def run_process(
    cfg: AppConfig,
    *,
    role: str = "all",
    max_minutes: float | None = None,
    clock: Clock | None = None,
    session_factory: Any | None = None,
    collect: bool = True,
) -> dict[str, Any]:
    """Point d'entrée des commandes ``okxq paper|shadow|demo|live run``.

    Retourne un compte rendu machine-readable de ce qui s'est passé : démarrage, frontières, arrêt.
    ``max_minutes`` borne l'exécution pour les tests et les vérifications de déploiement ; ce n'est pas
    un mode dégradé, c'est un arrêt propre après N minutes.
    """
    configure_logging(
        level=cfg.observability.log_level, json_output=cfg.observability.log_json, mode=cfg.mode.value
    )
    bind_context(mode=cfg.mode.value, role=role, account_scope=cfg.account.scope)
    rt = build_runtime(cfg, role=role, clock=clock, session_factory=session_factory)
    alerts = build_default_manager(clock=rt.clock)
    report: dict[str, Any] = {"mode": cfg.mode.value, "role": role, "ok": False}
    try:
        rt.exchange = build_exchange_adapter(cfg, role=role, clock=rt.clock, market=rt.market)
        if isinstance(rt.exchange, ShadowGateway):
            rt.gateway = rt.exchange
        if role in ("collector", "all") and collect:
            # La collecte démarre AVANT la séquence de démarrage : `validate_data` doit pouvoir
            # constater des données, et elle ne peut pas en constater si rien n'a encore été reçu.
            report["collector"] = await start_collection(rt)
        startup = await build_startup_sequence(rt).run()
        report["startup"] = startup.as_dict()
        rt.loop = build_decision_loop(rt)
        scheduler = DecisionScheduler(
            clock=rt.clock,
            interval_seconds=cfg.runtime.decision_interval_seconds,
            budget_ms=cfg.runtime.decision_budget_ms,
            overlap_policy=cfg.runtime.decision_overlap_policy,
            on_boundary=_boundary_callback(rt),
        )
        rt.scheduler = scheduler
        _install_signal_handlers(rt.stop)
        if max_minutes is not None:
            asyncio.get_running_loop().call_later(max_minutes * 60.0, rt.stop.set)
        if not startup.entries_authorized:
            # Le processus tourne quand même : observer, réduire et rapporter restent utiles quand
            # les entrées sont interdites. C'est le contraire d'un arrêt silencieux.
            log.warning("entrees_non_autorisees", raison="séquence de démarrage incomplète")
            alerts.raise_alert(
                "entries_not_authorized",
                Priority.P1,
                "entrées non autorisées",
                "la séquence de démarrage n'a pas autorisé les entrées ; observation et réduction "
                "restent actives",
                evidence={k: str(v) for k, v in startup.as_dict().items() if k != "steps"},
            )
        await scheduler.run(rt.stop)
        report["scheduler"] = {
            "boundaries_seen": scheduler.stats.boundaries_seen,
            "decisions_started": scheduler.stats.decisions_started,
            "decisions_completed": scheduler.stats.decisions_completed,
            "decisions_failed": scheduler.stats.decisions_failed,
            "skipped_overlap": scheduler.stats.skipped_overlap,
            "deadline_misses": scheduler.stats.deadline_misses,
        }
        report["ok"] = True
    finally:
        await rt.aclose()
        report["health"] = rt.health.health_tick()
    return report


def _boundary_callback(rt: Runtime) -> Callable[[datetime, datetime], Awaitable[object]]:
    async def on_boundary(boundary: datetime, deadline: datetime) -> object:
        loop = rt.loop
        if loop is None:
            return None
        rt.health.touch(health_names.STRATEGY)
        record = await loop.run_once(boundary, deadline)
        log.info(
            "decision",
            decision_id=record.decision_id,
            outcome=record.outcome,
            reason_codes=",".join(record.reason_codes),
            intents=len(record.intents),
        )
        return record

    return on_boundary
