"""Schéma relationnel (§44). Toutes les tables, contraintes et index, en un seul endroit.

Règles de conception :
- ``TIMESTAMPTZ`` partout (TZDateTime) ; ``NUMERIC`` de précision explicite ;
- unicité durable de (account_scope, client_order_id) ; aucune réutilisation après état terminal ;
- fills dédupliqués par ``execution_key`` (clé construite selon la portée réelle des identifiants) ;
- cache sémantique JEV unique par (model_version, question_hash, document_version_id, asset_mapping_version) ;
- écriture métier + outbox dans la même transaction ; consommation idempotente via ``consumer_offsets`` ;
- versions optimistes sur les projections (orders.version, position_snapshots.version).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from okxq.persistence.types import (
    FractionNumeric,
    JsonDoc,
    MoneyNumeric,
    PriceNumeric,
    QtyNumeric,
    TZDateTime,
)


class Base(DeclarativeBase):
    pass


# --- données de marché et métadonnées ---------------------------------------------------------------


class InstrumentVersion(Base):
    __tablename__ = "instrument_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    settle_ccy: Mapped[str] = mapped_column(String(16), nullable=False)
    base_ccy: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_type: Mapped[str] = mapped_column(String(16), nullable=False)
    base_units_per_contract: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    lot_size: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    min_size: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    max_leverage: Mapped[Decimal | None] = mapped_column(QtyNumeric, nullable=True)
    provenance: Mapped[str] = mapped_column(String(256), nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    __table_args__ = (
        UniqueConstraint("inst_id", "version", name="uq_instrument_version"),
        CheckConstraint("base_units_per_contract > 0", name="ck_instr_v_positive"),
        CheckConstraint("tick_size > 0 AND lot_size > 0 AND min_size > 0", name="ck_instr_grid_positive"),
        Index("ix_instrument_versions_inst_valid", "inst_id", "valid_from"),
    )


class UniverseSnapshot(Base):
    __tablename__ = "universe_snapshots"
    universe_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    eligible: Mapped[list[str]] = mapped_column(JsonDoc, nullable=False)
    held_only: Mapped[list[str]] = mapped_column(JsonDoc, nullable=False, default=list)
    criteria: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    bias_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class RawPartition(Base):
    __tablename__ = "raw_partitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    inst_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    partition_key: Mapped[str] = mapped_column(String(128), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_ts: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    last_ts: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    written_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __table_args__ = (UniqueConstraint("dataset", "partition_key", name="uq_raw_partition"),)


class DataQualityEvent(Base):
    __tablename__ = "data_quality_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    inst_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    __table_args__ = (Index("ix_dq_events_time", "occurred_at"),)


# --- JEV -------------------------------------------------------------------------------------------------


class SourceDocumentRow(Base):
    __tablename__ = "source_documents"
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    deduplication_id: Mapped[str] = mapped_column(String(128), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    __table_args__ = (UniqueConstraint("source", "deduplication_id", name="uq_source_dedup"),)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.document_id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    date_method: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    parsed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    raw_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    asset_mapping: Mapped[list[dict[str, Any]]] = mapped_column(JsonDoc, nullable=False, default=list)
    asset_mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_document_version"),)


class JevRequest(Base):
    __tablename__ = "jev_requests"
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class JevResult(Base):
    __tablename__ = "jev_results"
    evaluation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(ForeignKey("jev_requests.request_id"), nullable=True)
    document_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_effective: Mapped[str | None] = mapped_column(String(64), nullable=True)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    inference_completed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    features_committed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    answers: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "model_version",
            "question_hash",
            "document_version_id",
            "asset_mapping_version",
            name="uq_jev_semantic_cache",
        ),
    )


# --- recherche et modèles -------------------------------------------------------------------------------


class FeatureSchema(Base):
    __tablename__ = "feature_schemas"
    schema_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    names: Mapped[list[str]] = mapped_column(JsonDoc, nullable=False)
    definitions: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    family: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    code_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_schema_hash: Mapped[str | None] = mapped_column(
        ForeignKey("feature_schemas.schema_hash"), nullable=True
    )
    period_start: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    universe_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    promoted_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False)
    started_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    trials: Mapped[list[dict[str, Any]]] = mapped_column(JsonDoc, nullable=False, default=list)
    final_test_consulted_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    code_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)


class EvaluationReport(Base):
    __tablename__ = "evaluation_reports"
    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("experiment_runs.run_id"), nullable=False)
    model_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.model_id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    independent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


# --- décisions, portefeuille, intentions, approbations --------------------------------------------------


class Decision(Base):
    __tablename__ = "decisions"
    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    started_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)  # TRADE / NO_TRADE / SKIPPED / FAILED
    reason_codes: Mapped[list[str]] = mapped_column(JsonDoc, nullable=False, default=list)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    universe_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    equity_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inputs_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    forecasts: Mapped[list[dict[str, Any]]] = mapped_column(JsonDoc, nullable=False, default=list)
    edges: Mapped[list[dict[str, Any]]] = mapped_column(JsonDoc, nullable=False, default=list)
    rejected_alternatives: Mapped[list[dict[str, Any]]] = mapped_column(JsonDoc, nullable=False, default=list)
    timings_ms: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    software_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (Index("ix_decisions_cutoff", "cutoff_at"),)


class PortfolioTargetRow(Base):
    __tablename__ = "portfolio_targets"
    target_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.decision_id"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    equity_version: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_weights: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False)
    constraints_version: Mapped[str] = mapped_column(String(64), nullable=False)
    solver_status: Mapped[str] = mapped_column(String(32), nullable=False)
    solver_report: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class OrderIntentRow(Base):
    __tablename__ = "order_intents"
    intent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.decision_id"), nullable=False)
    target_id: Mapped[str | None] = mapped_column(ForeignKey("portfolio_targets.target_id"), nullable=True)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    contracts: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    price_limit: Mapped[Decimal | None] = mapped_column(PriceNumeric, nullable=True)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ttl_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("contracts > 0", name="ck_intent_contracts_positive"),
        CheckConstraint("ttl_ms > 0", name="ck_intent_ttl_positive"),
        UniqueConstraint("account_scope", "client_order_id", name="uq_intent_client_order_id"),
    )


class RiskApproval(Base):
    __tablename__ = "risk_approvals"
    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intents.intent_id"), nullable=False)
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    allowed_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allowed_contracts: Mapped[Decimal | None] = mapped_column(QtyNumeric, nullable=True)
    limits_version: Mapped[str] = mapped_column(String(64), nullable=False)
    position_version: Mapped[str] = mapped_column(String(64), nullable=False)
    reservations: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    reason_codes: Mapped[list[str]] = mapped_column(JsonDoc, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    __table_args__ = (Index("ix_risk_approvals_intent", "intent_id"),)


# --- ordres, événements, fills, réservations ------------------------------------------------------------


class OrderRow(Base):
    __tablename__ = "orders"
    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intents.intent_id"), nullable=False)
    approval_id: Mapped[str] = mapped_column(ForeignKey("risk_approvals.approval_id"), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    contracts: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    price_limit: Mapped[Decimal | None] = mapped_column(PriceNumeric, nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_state: Mapped[str] = mapped_column(String(24), nullable=False)
    pending_operation: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    cumulative_filled: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False, default=Decimal(0))
    average_fill_price: Mapped[Decimal | None] = mapped_column(PriceNumeric, nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_payload: Mapped[dict[str, Any] | None] = mapped_column(JsonDoc, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (
        UniqueConstraint("account_scope", "client_order_id", name="uq_orders_client_order_id"),
        CheckConstraint("contracts > 0", name="ck_orders_contracts_positive"),
        CheckConstraint("cumulative_filled >= 0", name="ck_orders_filled_nonneg"),
        Index("ix_orders_state", "account_scope", "observed_state"),
        Index("ix_orders_exchange_id", "exchange_order_id"),
    )


class OrderEventRow(Base):
    __tablename__ = "order_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.order_id"), nullable=True)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_state: Mapped[str] = mapped_column(String(24), nullable=False)
    cumulative_filled: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    event_ts: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    receive_ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    raw_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    __table_args__ = (
        UniqueConstraint("account_scope", "client_order_id", "raw_hash", name="uq_order_event_dedup"),
        Index("ix_order_events_order", "order_id"),
    )


class FillRow(Base):
    __tablename__ = "fills"
    execution_key: Mapped[str] = mapped_column(String(192), primary_key=True)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.order_id"), nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    contracts: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    fee_cashflow: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    fee_ccy: Mapped[str] = mapped_column(String(16), nullable=False)
    liquidity: Mapped[str] = mapped_column(String(8), nullable=False, default="unknown")
    fill_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    receive_ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    __table_args__ = (
        CheckConstraint("contracts > 0", name="ck_fills_contracts_positive"),
        CheckConstraint("fill_price > 0", name="ck_fills_price_positive"),
        Index("ix_fills_order", "order_id"),
        Index("ix_fills_time", "account_scope", "fill_at"),
    )


class ExecutionReservation(Base):
    __tablename__ = "execution_reservations"
    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_id: Mapped[str] = mapped_column(ForeignKey("order_intents.intent_id"), nullable=False)
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_contracts: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    notional_usdt: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    pessimistic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ACTIVE / RELEASED
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (Index("ix_reservations_active", "account_scope", "status"),)


# --- comptabilité ------------------------------------------------------------------------------------


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    txn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(192), nullable=False)
    fill_key: Mapped[str | None] = mapped_column(ForeignKey("fills.execution_key"), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JsonDoc, nullable=False, default=dict)
    __table_args__ = (
        UniqueConstraint("account_scope", "idempotency_key", name="uq_ledger_txn_idempotency"),
        Index("ix_ledger_txn_time", "account_scope", "occurred_at"),
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_id: Mapped[str] = mapped_column(ForeignKey("ledger_transactions.txn_id"), nullable=False)
    account: Mapped[str] = mapped_column(String(64), nullable=False)  # ex. cash:USDT, fees:USDT, pnl:realized
    ccy: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    inst_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (Index("ix_ledger_entries_txn", "txn_id"),)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    equity_version: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # ledger / exchange
    cash_collateral: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    equity: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False)
    available_margin: Mapped[Decimal | None] = mapped_column(MoneyNumeric, nullable=True)
    used_margin: Mapped[Decimal | None] = mapped_column(MoneyNumeric, nullable=True)
    external_cashflow_cum: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False, default=Decimal(0))
    unit_value: Mapped[Decimal | None] = mapped_column(FractionNumeric, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    __table_args__ = (
        UniqueConstraint("account_scope", "equity_version", name="uq_account_snapshot_version"),
    )


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    signed_base_qty: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    signed_contracts: Mapped[Decimal] = mapped_column(QtyNumeric, nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(PriceNumeric, nullable=False)
    mark_price: Mapped[Decimal | None] = mapped_column(PriceNumeric, nullable=True)
    liquidation_price: Mapped[Decimal | None] = mapped_column(PriceNumeric, nullable=True)
    margin: Mapped[Decimal | None] = mapped_column(MoneyNumeric, nullable=True)
    leverage: Mapped[Decimal | None] = mapped_column(QtyNumeric, nullable=True)
    protection: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (Index("ix_position_snapshots_latest", "account_scope", "inst_id", "as_of"),)


# --- risque, exploitation, outbox --------------------------------------------------------------------


class RiskEventRow(Base):
    __tablename__ = "risk_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    affected_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    requested_action: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_result: Mapped[str | None] = mapped_column(String(256), nullable=True)
    __table_args__ = (Index("ix_risk_events_time", "created_at"),)


class RiskState(Base):
    """État de risque persistant : halts, pertes journalières, high-water mark (§38, §52.4, T44)."""

    __tablename__ = "risk_state"
    account_scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    halt_level: Mapped[str] = mapped_column(String(24), nullable=False, default="NONE")
    halt_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    halt_since: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    utc_day: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    day_start_equity: Mapped[Decimal | None] = mapped_column(MoneyNumeric, nullable=True)
    day_realized_loss: Mapped[Decimal] = mapped_column(MoneyNumeric, nullable=False, default=Decimal(0))
    high_water_mark_unit: Mapped[Decimal | None] = mapped_column(FractionNumeric, nullable=True)
    high_water_mark_equity: Mapped[Decimal | None] = mapped_column(MoneyNumeric, nullable=True)
    limits_version: Mapped[str] = mapped_column(String(64), nullable=False, default="limits-v1")
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RuntimeLease(Base):
    __tablename__ = "runtime_leases"
    lease_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    acquired_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(192), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    fencing_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_idempotency"),
        Index("ix_outbox_pending", "status", "created_at"),
    )


class ConsumerOffset(Base):
    __tablename__ = "consumer_offsets"
    consumer_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_event_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class OperatorAction(Base):
    __tablename__ = "operator_actions"
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    observed_result: Mapped[dict[str, Any]] = mapped_column(JsonDoc, nullable=False, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    __table_args__ = (Index("ix_operator_actions_time", "requested_at"),)


ALL_TABLES: tuple[str, ...] = tuple(sorted(Base.metadata.tables.keys()))
