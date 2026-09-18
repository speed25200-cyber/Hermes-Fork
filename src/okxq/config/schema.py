"""Schéma strict de configuration (§63) : champs inconnus refusés, unités explicites, contradictions détectées."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from okxq.config.modes import Mode
from okxq.domain.events import CostBasis
from okxq.domain.money import dec

Fraction = Annotated[float, Field(ge=0.0, le=10.0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegInt = Annotated[int, Field(ge=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ProjectCfg(StrictModel):
    name: str = Field(min_length=1)
    schema_version: Literal[1]
    mode: Mode
    live_enabled: bool = False
    timezone_storage: Literal["UTC"] = "UTC"
    timezone_display: str = "Europe/Zurich"
    fixture_only: bool = False


class AccountCfg(StrictModel):
    scope: str = Field(min_length=1)
    settlement_currency: Literal["USDT"] = "USDT"
    position_mode: Literal["net"] = "net"
    margin_mode: Literal["isolated", "cross"] = "isolated"
    paper_initial_equity_usdt: str = "100000"
    real_capital_usdt: str | None = None
    region_profile: str = "unspecified"

    @field_validator("paper_initial_equity_usdt", "real_capital_usdt")
    @classmethod
    def _decimal_string(cls, v: str | None) -> str | None:
        if v is None:
            return None
        d = dec(v, field="montant")
        if d <= 0:
            raise ValueError("montant non positif")
        return format(d, "f")

    @property
    def paper_initial_equity(self) -> Decimal:
        return Decimal(self.paper_initial_equity_usdt)


class RuntimeCfg(StrictModel):
    decision_interval_seconds: PositiveInt = 60
    decision_budget_ms: PositiveInt = 3000
    decision_overlap_policy: Literal["skip", "coalesce"] = "skip"
    max_clock_offset_ms: PositiveInt = 250
    require_single_execution_writer: bool = True
    lease_ttl_ms: PositiveInt = 5000
    lease_heartbeat_ms: PositiveInt = 1000

    @model_validator(mode="after")
    def _lease(self) -> RuntimeCfg:
        if self.lease_heartbeat_ms * 2 > self.lease_ttl_ms:
            raise ValueError("lease_heartbeat_ms doit être au plus la moitié de lease_ttl_ms")
        if self.decision_budget_ms >= self.decision_interval_seconds * 1000:
            raise ValueError("decision_budget_ms doit être inférieur à l'intervalle de décision")
        return self


class UniverseCfg(StrictModel):
    instrument_type: Literal["SWAP"] = "SWAP"
    contract_type: Literal["linear"] = "linear"
    settlement_currency: Literal["USDT"] = "USDT"
    target_size: PositiveInt = 20
    minimum_eligible: PositiveInt = 10
    maximum_eligible: PositiveInt = 30
    minimum_history_days: NonNegInt = 30
    refresh_interval_seconds: PositiveInt = 3600
    on_removal: Literal["manage_existing_reduce_only"] = "manage_existing_reduce_only"
    min_notional_volume_24h_usdt: str = "5000000"
    max_relative_spread: float = Field(gt=0, le=0.05, default=0.0015)
    min_depth_notional_usdt: str = "20000"
    max_missing_fraction: float = Field(ge=0, le=1, default=0.05)

    @model_validator(mode="after")
    def _bounds(self) -> UniverseCfg:
        if not (self.minimum_eligible <= self.target_size <= self.maximum_eligible):
            raise ValueError("minimum_eligible <= target_size <= maximum_eligible requis")
        dec(self.min_notional_volume_24h_usdt, field="min_notional_volume_24h_usdt")
        dec(self.min_depth_notional_usdt, field="min_depth_notional_usdt")
        return self


class MarketDataCfg(StrictModel):
    orderbook_channel: Literal["books", "books5", "books50-l2-tbt", "books-l2-tbt", "bbo-tbt"] = "books"
    max_quote_age_ms: PositiveInt = 2000
    max_private_state_age_ms: PositiveInt = 15000
    require_valid_sequence: bool = True
    checksum_policy: Literal["channel_version_aware", "disabled"] = "channel_version_aware"
    reject_crossed_book: bool = True
    persist_raw: bool = True
    fail_on_unrecoverable_gap: bool = True
    max_forward_fill_ms: dict[str, PositiveInt] = Field(
        default_factory=lambda: {"book": 2000, "trade": 60000, "funding": 3600000, "open_interest": 300000}
    )


class StrategyCfg(StrictModel):
    horizons_seconds: list[PositiveInt] = Field(default_factory=lambda: [60, 120, 180, 300, 600, 900])
    optimizer_horizon_seconds: PositiveInt = 300
    allow_no_trade: Literal[True] = True
    cost_basis: CostBasis = CostBasis.MID
    allow_unvalidated_model: bool = False
    allow_unvalidated_fallback: bool = False
    uncertainty_penalty_coefficient: float = Field(ge=0, default=1.0)
    min_edge_score: float = Field(default=0.0)

    @model_validator(mode="after")
    def _horizon(self) -> StrategyCfg:
        if self.optimizer_horizon_seconds not in self.horizons_seconds:
            raise ValueError("optimizer_horizon_seconds doit figurer dans horizons_seconds")
        return self


class RiskCfg(StrictModel):
    max_gross_equity_multiple: float = Field(gt=0, default=1.0)
    max_abs_net_equity_multiple: float = Field(ge=0, default=0.10)
    max_asset_equity_multiple: float = Field(gt=0, default=0.10)
    max_cluster_gross_equity_multiple: float = Field(gt=0, default=0.30)
    max_abs_btc_beta_exposure: float = Field(ge=0, default=0.10)
    max_abs_eth_beta_exposure: float = Field(ge=0, default=0.10)
    max_margin_utilization: float = Field(gt=0, le=1, default=0.50)
    modeled_risk_per_idea_fraction: float = Field(gt=0, le=0.1, default=0.001)
    daily_loss_halt_fraction: float = Field(gt=0, le=1, default=0.01)
    drawdown_review_fraction: float = Field(gt=0, le=1, default=0.05)
    include_pending_and_unknown_orders: Literal[True] = True
    approval_ttl_ms: PositiveInt = 1000
    auto_resume_after_critical_halt: Literal[False] = False
    resume_stability_seconds: PositiveInt = 300
    max_orders_per_minute: PositiveInt = 30
    liquidation_distance_min_fraction: float = Field(gt=0, le=1, default=0.25)
    margin_buffer_fraction: float = Field(ge=0, le=1, default=0.20)

    @model_validator(mode="after")
    def _coherent(self) -> RiskCfg:
        if self.max_asset_equity_multiple > self.max_gross_equity_multiple:
            raise ValueError("limite par actif supérieure à la limite brute")
        if self.max_cluster_gross_equity_multiple > self.max_gross_equity_multiple:
            raise ValueError("limite par cluster supérieure à la limite brute")
        if self.daily_loss_halt_fraction > self.drawdown_review_fraction:
            raise ValueError("seuil journalier supérieur au seuil de drawdown")
        return self


_DEFAULT_ENTRY_TYPES: tuple[Literal["post_only", "ioc", "limit"], ...] = ("post_only", "ioc")


class ExecutionCfg(StrictModel):
    allowed_entry_order_types: list[Literal["post_only", "ioc", "limit"]] = Field(
        default_factory=lambda: list(_DEFAULT_ENTRY_TYPES)
    )
    allow_market_entries: bool = False
    max_order_intent_age_ms: PositiveInt = 2000
    max_order_participation_fraction: float = Field(gt=0, le=1, default=0.01)
    participation_window_seconds: PositiveInt = 60
    participation_reference: Literal["observed_notional_volume"] = "observed_notional_volume"
    max_depth_consumption_fraction: float = Field(gt=0, le=1, default=0.10)
    max_turnover_equity_fraction_per_decision: float = Field(gt=0, le=2, default=0.30)
    max_price_chase_attempts: NonNegInt = 2
    require_reconciliation_before_entries: Literal[True] = True
    cancel_all_after_enabled: bool = True
    cancel_all_after_timeout_seconds: PositiveInt = 30
    cancel_all_after_heartbeat_seconds: PositiveInt = 1
    emergency_market_exit_enabled: bool = False
    passive_entry_max_wait_ms: PositiveInt = 20000
    execution_policy_version: str = "exec-policy-v1"

    @model_validator(mode="after")
    def _coherent(self) -> ExecutionCfg:
        if self.cancel_all_after_heartbeat_seconds * 3 > self.cancel_all_after_timeout_seconds:
            raise ValueError("heartbeat CAA trop lent par rapport à son timeout")
        if not self.allowed_entry_order_types:
            raise ValueError("au moins un type d'ordre d'entrée")
        return self


class JevCfg(StrictModel):
    enabled: bool = True
    influence_mode: Literal["off", "shadow", "validated"] = "shadow"
    model: str = "jev-1.13.0"
    endpoint: str = "https://api.typesafe.ai/v1/systemone"
    questions_path: str = "configs/jev_questions.v1.json"
    timeout_total_ms: PositiveInt = 1500
    max_concurrency: PositiveInt = 2
    max_queue_length: PositiveInt = 100
    max_document_characters: PositiveInt = 20000
    default_event_max_age_seconds: PositiveInt = 3600
    max_retry_attempts: NonNegInt = 1
    max_daily_spend_usd: str = "5.00"
    source_allowlist_path: str = "configs/sources.example.yaml"
    on_unavailable: Literal["validated_quant_only_else_halt_entries", "halt_entries"] = (
        "validated_quant_only_else_halt_entries"
    )
    never_receive_okx_credentials: Literal[True] = True
    circuit_breaker_failures: PositiveInt = 5
    circuit_breaker_cooldown_seconds: PositiveInt = 300

    @field_validator("endpoint")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("endpoint JEV doit être en HTTPS")
        return v

    @field_validator("max_daily_spend_usd")
    @classmethod
    def _spend(cls, v: str) -> str:
        d = dec(v, field="max_daily_spend_usd")
        if d < 0:
            raise ValueError("budget négatif")
        return format(d, "f")


class ResearchCfg(StrictModel):
    random_seed: int = 25200
    max_trials_per_experiment: PositiveInt = 30
    require_temporal_oof_stacking: Literal[True] = True
    require_frozen_final_test: Literal[True] = True
    require_cost_stress_tests: Literal[True] = True
    promotion_auto_approve: Literal[False] = False
    max_job_minutes: PositiveInt = 120
    max_cpu_threads: PositiveInt = 2


class ApiCfg(StrictModel):
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(ge=1, le=65535, default=8899)
    require_authentication: Literal[True] = True
    #: Accès à l'interface SANS clé. Décision d'exploitation explicite, jamais un défaut.
    #:
    #: - ``non`` (défaut) : la porte reste fermée, une clé est exigée ;
    #: - ``lecture`` : une requête sans clé reçoit le rôle LECTEUR. Le tableau de bord s'ouvre, et
    #:   les commandes opérateur restent refusées — exactement comme avec une clé de lecteur ;
    #: - ``total`` : une requête sans clé reçoit le rôle ADMIN. Tout est ouvert, commandes comprises.
    #:
    #: Refusé en DEMO et en LIVE par la validation ci-dessous. Le port est joignable depuis Internet,
    #: et une API de pilotage ouverte sur un compte qui envoie de vrais ordres n'est pas un réglage
    #: de confort. Ce garde-fou existe pour que ce choix ne suive pas discrètement la plateforme le
    #: jour où elle passe en argent réel.
    acces_sans_cle: Literal["non", "lecture", "total"] = "non"
    allow_browser_live_activation: Literal[False] = False
    session_ttl_minutes: PositiveInt = 720
    cors_origins: list[str] = Field(default_factory=list)
    serve_frontend: bool = True
    metrics_enabled: bool = True


class StorageCfg(StrictModel):
    raw_format: Literal["parquet"] = "parquet"
    compression: Literal["zstd", "snappy"] = "zstd"
    require_durable_order_journal: Literal[True] = True
    audit_append_only: Literal[True] = True
    data_root: str = "data"
    raw_retention_days: PositiveInt = 90


class ObservabilityCfg(StrictModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    alert_sink: Literal["local", "webhook"] = "local"
    alert_dedup_seconds: PositiveInt = 300


class AppConfig(StrictModel):
    project: ProjectCfg
    account: AccountCfg
    runtime: RuntimeCfg = RuntimeCfg()
    universe: UniverseCfg = UniverseCfg()
    market_data: MarketDataCfg = MarketDataCfg()
    strategy: StrategyCfg = StrategyCfg()
    risk: RiskCfg = RiskCfg()
    execution: ExecutionCfg = ExecutionCfg()
    jev: JevCfg = JevCfg()
    research: ResearchCfg = ResearchCfg()
    api: ApiCfg = ApiCfg()
    storage: StorageCfg = StorageCfg()
    observability: ObservabilityCfg = ObservabilityCfg()
    source_path: str | None = None
    config_hash: str | None = None

    @model_validator(mode="after")
    def _contradictions(self) -> AppConfig:
        p = self.project
        if p.live_enabled and p.mode is not Mode.LIVE:
            raise ValueError("live_enabled=true exige mode LIVE")
        if p.fixture_only and p.mode not in (Mode.PAPER, Mode.RESEARCH):
            raise ValueError("une configuration de fixture ne peut pas être chargée hors PAPER/RESEARCH")
        if p.mode is Mode.LIVE and self.account.real_capital_usdt is None and p.live_enabled:
            raise ValueError("LIVE exige real_capital_usdt renseigné par l'opérateur")
        if p.mode is not Mode.LIVE and self.account.real_capital_usdt is not None:
            raise ValueError("real_capital_usdt n'a de sens qu'en LIVE")
        if self.execution.allow_market_entries and not self.execution.emergency_market_exit_enabled:
            raise ValueError(
                "allow_market_entries exige une politique de crise explicite (emergency_market_exit_enabled)"
            )
        if self.jev.influence_mode == "validated" and not self.jev.enabled:
            raise ValueError("influence JEV validée exige jev.enabled")
        if self.strategy.allow_unvalidated_model and p.mode in (Mode.DEMO, Mode.LIVE):
            raise ValueError("un modèle non validé ne peut pas piloter DEMO ou LIVE")
        if self.api.acces_sans_cle != "non" and p.mode in (Mode.DEMO, Mode.LIVE):
            # Un accès sans clé se décide pour un bac à sable, et se garde rarement en tête le jour
            # où le même fichier de configuration sert à un compte qui envoie de vrais ordres. Le
            # refus est ici, dans la validation, et non dans une consigne d'exploitation.
            raise ValueError(
                "api.acces_sans_cle est interdit en DEMO et en LIVE : une API de pilotage ouverte "
                "sur un compte qui envoie des ordres réels n'est pas un réglage de confort"
            )
        return self

    @property
    def mode(self) -> Mode:
        return self.project.mode

    @property
    def is_live(self) -> bool:
        return self.project.mode is Mode.LIVE and self.project.live_enabled
