"""Contrats de données internes (§43) : objets immuables validés.

Conventions :
- décimaux monétaires en ``Decimal`` (sérialisés en chaînes) ;
- probabilités et prédictions statistiques en flottants FINIS ;
- identifiants non vides ; horodatages timezone-aware ;
- une Forecast ne contient aucun champ donnant une permission d'exchange ;
- une RiskDecision est liée au hash exact du payload normalisé de l'intention.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    model_validator,
)

from okxq.domain.ids import payload_hash
from okxq.domain.money import Side
from okxq.domain.orders import OrderKind, OrderState


def _finite_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("décimal non fini")
    return value


FiniteDecimal = Annotated[Decimal, AfterValidator(_finite_decimal)]
NonEmptyStr = Annotated[str, Field(min_length=1, max_length=256)]
Probability = Annotated[FiniteFloat, Field(ge=0.0, le=1.0)]


class Contract(BaseModel):
    """Base immuable, champs inconnus refusés."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    def canonical_hash(self) -> str:
        return payload_hash(self.model_dump(mode="json"))


class QualityFlag(StrEnum):
    OK = "OK"
    MISSING = "MISSING"
    STALE = "STALE"
    INVALID = "INVALID"


class CostBasis(StrEnum):
    MID = "MID"
    EXECUTABLE = "EXECUTABLE"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"

    @property
    def sign(self) -> int:
        return {"LONG": 1, "SHORT": -1, "FLAT": 0}[self.value]


class RiskAction(StrEnum):
    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    FLATTEN = "FLATTEN"


class Severity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class EventEnvelope(Contract):
    event_id: NonEmptyStr
    event_type: NonEmptyStr
    schema_version: int = Field(ge=1)
    source: NonEmptyStr
    exchange_ts: AwareDatetime | None
    receive_ts: AwareDatetime
    available_at: AwareDatetime
    ingest_seq: int = Field(ge=0)
    payload_hash: NonEmptyStr
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _causal(self) -> EventEnvelope:
        if self.available_at < self.receive_ts:
            raise ValueError("available_at ne peut précéder receive_ts")
        return self


class MarketSnapshot(Contract):
    snapshot_id: NonEmptyStr
    cutoff_at: AwareDatetime
    universe_version: NonEmptyStr
    metadata_version: NonEmptyStr
    equity_version: NonEmptyStr
    eligible_instruments: list[str]
    held_instruments: list[str] = Field(default_factory=list)
    book_versions: dict[str, int] = Field(default_factory=dict)
    feature_versions: dict[str, str] = Field(default_factory=dict)
    quality_flags: dict[str, list[QualityFlag]] = Field(default_factory=dict)
    features: dict[str, FeatureVector] = Field(default_factory=dict)
    reference_prices: dict[str, FiniteDecimal] = Field(default_factory=dict)


class FeatureVector(Contract):
    instrument: NonEmptyStr
    cutoff_at: AwareDatetime
    available_at: AwareDatetime
    names: list[str]
    values: list[FiniteFloat | None]
    masks: list[QualityFlag]
    schema_hash: NonEmptyStr

    @model_validator(mode="after")
    def _aligned(self) -> FeatureVector:
        if not (len(self.names) == len(self.values) == len(self.masks)):
            raise ValueError("names/values/masks désalignés")
        if len(set(self.names)) != len(self.names):
            raise ValueError("noms de features dupliqués")
        for name, value, mask in zip(self.names, self.values, self.masks, strict=True):
            if mask is QualityFlag.OK and value is None:
                raise ValueError(f"feature {name} marquée OK sans valeur")
            if mask is not QualityFlag.OK and value is not None:
                raise ValueError(
                    f"feature {name} marquée {mask} avec une valeur : ne pas imputer silencieusement"
                )
        if self.available_at < self.cutoff_at:
            raise ValueError("available_at < cutoff_at")
        return self

    def as_dict(self) -> dict[str, float | None]:
        return dict(zip(self.names, self.values, strict=True))


class Forecast(Contract):
    forecast_id: NonEmptyStr
    model_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    instrument: NonEmptyStr
    horizon_s: int = Field(gt=0)
    gross_mu: FiniteFloat
    quantiles: dict[str, FiniteFloat] = Field(default_factory=dict)
    uncertainty: FiniteFloat = Field(ge=0.0)
    execution_policy_id: NonEmptyStr
    cost_basis: CostBasis
    available_at: AwareDatetime
    fold_id: str | None = None
    producer: str | None = None


class EdgeEstimate(Contract):
    edge_id: NonEmptyStr
    forecast_id: NonEmptyStr
    instrument: NonEmptyStr
    side: PositionSide
    horizon_s: int = Field(gt=0)
    cost_basis: CostBasis
    expected_gross_return: FiniteDecimal
    components: dict[str, FiniteDecimal]
    included_in_price: list[str] = Field(default_factory=list)
    net_edge: FiniteDecimal
    uncertainty: FiniteDecimal = Field(ge=0)
    uncertainty_penalty: FiniteDecimal = Field(ge=0)
    edge_score: FiniteDecimal
    expires_at: AwareDatetime


class PortfolioTarget(Contract):
    target_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    equity_version: NonEmptyStr
    signed_weights: dict[str, FiniteDecimal]
    constraints_version: NonEmptyStr
    solver_status: NonEmptyStr
    objective_value: FiniteFloat | None = None
    active_constraints: list[str] = Field(default_factory=list)
    residuals: dict[str, FiniteFloat] = Field(default_factory=dict)
    solve_ms: int = Field(ge=0, default=0)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    reason_codes: list[str] = Field(default_factory=list)


class OrderIntent(Contract):
    intent_id: NonEmptyStr
    decision_id: NonEmptyStr
    target_id: NonEmptyStr
    account_scope: NonEmptyStr
    inst_id: NonEmptyStr
    side: Side
    contracts: FiniteDecimal = Field(gt=0)
    price_limit: FiniteDecimal | None = Field(default=None, gt=0)
    order_type: OrderKind
    reduce_only: bool
    ttl_ms: int = Field(gt=0)
    reason: NonEmptyStr
    client_order_id: NonEmptyStr
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _rules(self) -> OrderIntent:
        if (
            self.order_type in (OrderKind.POST_ONLY, OrderKind.LIMIT, OrderKind.IOC)
            and self.price_limit is None
        ):
            raise ValueError("un ordre à prix limité exige price_limit")
        if self.order_type is OrderKind.MARKET and self.price_limit is not None:
            raise ValueError("un ordre market ne porte pas de price_limit")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at doit suivre created_at")
        return self

    def normalized_payload(self) -> dict[str, Any]:
        """Le payload EXACT que le gateway enverra ; c'est lui qui est haché et approuvé."""
        return {
            "account_scope": self.account_scope,
            "client_order_id": self.client_order_id,
            "inst_id": self.inst_id,
            "side": self.side.value,
            "contracts": format(self.contracts, "f"),
            "price_limit": None if self.price_limit is None else format(self.price_limit, "f"),
            "order_type": self.order_type.value,
            "reduce_only": self.reduce_only,
        }

    def payload_hash(self) -> str:
        return payload_hash(self.normalized_payload())


class RiskDecision(Contract):
    decision_id: NonEmptyStr
    intent_id: NonEmptyStr
    intent_hash: NonEmptyStr
    action: RiskAction
    allowed_payload_hash: str | None
    allowed_contracts: FiniteDecimal | None = Field(default=None, ge=0)
    limits_version: NonEmptyStr
    position_version: NonEmptyStr
    reservations: dict[str, str] = Field(default_factory=dict)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> RiskDecision:
        if self.action in (RiskAction.ALLOW, RiskAction.REDUCE) and not self.allowed_payload_hash:
            raise ValueError("une décision ALLOW/REDUCE porte le hash du payload autorisé")
        if self.action in (RiskAction.REJECT, RiskAction.FLATTEN) and self.allowed_payload_hash:
            raise ValueError("une décision REJECT/FLATTEN n'autorise aucun payload")
        return self


class ApprovedOrder(Contract):
    intent: OrderIntent
    decision: RiskDecision
    payload_hash: NonEmptyStr

    @model_validator(mode="after")
    def _bound(self) -> ApprovedOrder:
        if self.decision.intent_id != self.intent.intent_id:
            raise ValueError("décision liée à une autre intention")
        if self.decision.action not in (RiskAction.ALLOW, RiskAction.REDUCE):
            raise ValueError("ordre non approuvé")
        if self.intent.payload_hash() != self.payload_hash:
            raise ValueError("payload_hash ne correspond pas à l'intention")
        if self.decision.allowed_payload_hash != self.payload_hash:
            raise ValueError("hash approuvé différent du payload : nouvelle validation requise")
        return self


class OrderEventKind(StrEnum):
    ACK = "ack"
    PARTIAL_FILL = "partial_fill"
    FILL = "fill"
    CANCEL = "cancel"
    REJECT = "reject"
    EXPIRE = "expire"
    AMEND = "amend"
    UNKNOWN = "unknown"


class OrderEvent(Contract):
    event_id: NonEmptyStr
    client_order_id: NonEmptyStr
    exchange_order_id: str | None
    event_kind: OrderEventKind
    observed_state: OrderState
    cumulative_filled: FiniteDecimal = Field(ge=0)
    average_fill_price: FiniteDecimal | None = None
    event_ts: AwareDatetime | None
    receive_ts: AwareDatetime
    raw_hash: str | None = None
    reason: str | None = None


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"
    UNKNOWN = "unknown"


class Fill(Contract):
    execution_key: NonEmptyStr
    account_scope: NonEmptyStr
    inst_id: NonEmptyStr
    client_order_id: NonEmptyStr
    exchange_order_id: str | None
    trade_id: str | None
    side: Side
    contracts: FiniteDecimal = Field(gt=0)
    fill_price: FiniteDecimal = Field(gt=0)
    fee_cashflow: FiniteDecimal
    fee_ccy: NonEmptyStr
    fill_at: AwareDatetime
    receive_ts: AwareDatetime
    liquidity: Liquidity = Liquidity.UNKNOWN


class RiskEvent(Contract):
    event_id: NonEmptyStr
    severity: Severity
    reason_code: NonEmptyStr
    affected_scope: NonEmptyStr
    evidence: dict[str, Any] = Field(default_factory=dict)
    requested_action: NonEmptyStr
    observed_result: str | None = None
    created_at: AwareDatetime


class SubmissionOutcome(StrEnum):
    ACK = "ACK"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    NOT_SENT = "NOT_SENT"


class SubmissionResult(Contract):
    intent_id: NonEmptyStr
    client_order_id: NonEmptyStr
    outcome: SubmissionOutcome
    exchange_order_id: str | None = None
    sent_at: AwareDatetime | None = None
    ack_at: AwareDatetime | None = None
    error_code: str | None = None
    message: str | None = None


class ReconciliationReport(Contract):
    started_at: AwareDatetime
    completed_at: AwareDatetime
    orders_checked: int = Field(ge=0)
    unknown_resolved: int = Field(ge=0)
    unknown_remaining: int = Field(ge=0)
    position_mismatches: list[dict[str, Any]] = Field(default_factory=list)
    balance_gap: FiniteDecimal | None = None
    ok: bool
    notes: list[str] = Field(default_factory=list)


class AssetMapping(Contract):
    inst_id: NonEmptyStr
    canonical_name: NonEmptyStr
    symbol: NonEmptyStr
    mapping_version: NonEmptyStr
    confidence: Probability
    method: NonEmptyStr


class SourceDocument(Contract):
    document_id: NonEmptyStr
    source: NonEmptyStr
    source_url: NonEmptyStr
    published_at: AwareDatetime | None
    first_seen_at: AwareDatetime
    received_at: AwareDatetime
    parsed_at: AwareDatetime | None = None
    language: str | None = None
    version: int = Field(ge=1)
    date_method: NonEmptyStr
    raw_text_hash: NonEmptyStr
    deduplication_id: NonEmptyStr
    asset_mapping: list[AssetMapping] = Field(default_factory=list)
    title: str = ""
    text: str = ""

    @model_validator(mode="after")
    def _causal(self) -> SourceDocument:
        if self.received_at < self.first_seen_at:
            raise ValueError("received_at < first_seen_at")
        return self


class JevAnswerType(StrEnum):
    CHOICE = "choice"
    NOUL = "noul"
    SCORE = "score"


class JevAnswer(Contract):
    type: JevAnswerType
    choice: str | None = None
    probabilities: dict[str, Probability] | None = None
    probability: Probability | None = None
    score: int | None = None
    score_distribution: dict[str, Probability] | None = None
    confidence: Probability | None = None


class JevStatus(StrEnum):
    OK = "ok"
    LATE = "late"
    ERROR = "error"
    REJECTED = "rejected"


class JevEvaluation(Contract):
    evaluation_id: NonEmptyStr
    document_id: NonEmptyStr
    document_version: int = Field(ge=1)
    question_set_hash: NonEmptyStr
    asset_mapping_version: NonEmptyStr
    model_requested: NonEmptyStr
    model_effective: str | None
    requested_at: AwareDatetime
    completed_at: AwareDatetime | None
    inference_completed_at: AwareDatetime | None = None
    features_committed_at: AwareDatetime | None = None
    answers: dict[str, JevAnswer] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    status: JevStatus
    error: str | None = None


class PortfolioInputs(Contract):
    """Entrées de l'optimiseur (§51) sur un horizon commun ; listes alignées sur ``instruments``."""

    instruments: list[str]
    mu: list[FiniteFloat]
    sigma: list[list[FiniteFloat]]
    w0: list[FiniteFloat]
    cost_buy: list[FiniteFloat]
    cost_sell: list[FiniteFloat]
    uncertainty_penalty: list[FiniteFloat]
    expected_funding_cost: list[FiniteFloat]
    future_exit_cost: list[FiniteFloat]
    asset_limit: list[FiniteFloat]
    liquidity_capacity: list[FiniteFloat]
    beta_btc: list[FiniteFloat]
    beta_eth: list[FiniteFloat]
    clusters: dict[str, list[str]]
    horizon_s: int = Field(gt=0)
    risk_aversion: FiniteFloat = Field(gt=0)
    gross_limit: FiniteFloat = Field(gt=0)
    net_limit: FiniteFloat = Field(ge=0)
    btc_beta_limit: FiniteFloat = Field(ge=0)
    eth_beta_limit: FiniteFloat = Field(ge=0)
    cluster_limit: FiniteFloat = Field(gt=0)
    turnover_limit: FiniteFloat = Field(ge=0)
    margin_capacity: FiniteFloat = Field(ge=0)
    margin_requirement_per_unit: list[FiniteFloat]
    equity_version: NonEmptyStr
    snapshot_id: NonEmptyStr
    constraints_version: NonEmptyStr

    @model_validator(mode="after")
    def _aligned(self) -> PortfolioInputs:
        n = len(self.instruments)
        for name in (
            "mu",
            "w0",
            "cost_buy",
            "cost_sell",
            "uncertainty_penalty",
            "expected_funding_cost",
            "future_exit_cost",
            "asset_limit",
            "liquidity_capacity",
            "beta_btc",
            "beta_eth",
            "margin_requirement_per_unit",
        ):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} désaligné avec instruments")
        if len(self.sigma) != n or any(len(row) != n for row in self.sigma):
            raise ValueError("sigma doit être carrée n×n")
        for members in self.clusters.values():
            for m in members:
                if m not in self.instruments:
                    raise ValueError(f"cluster référence un instrument inconnu : {m}")
        return self


# Sérialisation utilitaire (les datetime restent des objets, pour les journaux et l'API).
def to_json_dict(obj: Contract) -> dict[str, Any]:
    return obj.model_dump(mode="json")


def utc_now_placeholder() -> datetime:  # pragma: no cover - petite aide pour les tests
    from datetime import UTC

    return datetime.now(tz=UTC)
