"""0001 — schéma initial (§44) : 30 tables, contraintes, index et clés étrangères.

Revision ID: 0001
Revises: aucune (base vide)
Create Date: 2026-09-18

Pourquoi : la plateforme n'a pas de schéma avant cette révision. Elle matérialise le schéma déclaré
dans ``okxq.persistence.models`` — source unique de vérité — pour que la base d'exploitation ne soit
jamais créée par ``create_all`` (qui ne laisse aucune trace de version et ne sait pas évoluer).

Pourquoi des types figés ici plutôt qu'un import de ``okxq.persistence.types`` : une migration décrit
l'état du schéma à l'instant où elle a été écrite. Si elle importait les types applicatifs, une
évolution ultérieure de ces types réécrirait silencieusement le passé de la base et deux
environnements migrés à des dates différentes n'auraient plus le même schéma. Les définitions
ci-dessous reproduisent donc exactement ce que produisaient, à la révision 0001, ``TZDateTime``
(TIMESTAMPTZ), ``JsonDoc`` (JSONB sur PostgreSQL, JSON ailleurs) et les NUMERIC de précision
explicite. Le test ``tests/unit/test_migrations.py`` vérifie cette égalité à chaque exécution.

Réversibilité : ``downgrade`` supprime toutes les tables et donc toutes les données. C'est une
migration destructive au sens du §44 : sauvegarde vérifiée et procédure de reprise obligatoires
avant de l'appliquer. La CLI l'exige explicitement (``okxq db downgrade --confirmer EFFACER``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Types figés à la révision 0001 (voir la docstring du module).
TS = sa.DateTime(timezone=True)
MONEY = sa.Numeric(precision=28, scale=8)
PRICE = sa.Numeric(precision=28, scale=12)
QTY = sa.Numeric(precision=28, scale=12)
FRACTION = sa.Numeric(precision=18, scale=12)
JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """Crée le schéma complet sur une base vide, dans l'ordre des dépendances de clés étrangères."""
    op.create_table(
        "account_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_scope", sa.String(length=64), nullable=False),
        sa.Column("equity_version", sa.String(length=64), nullable=False),
        sa.Column("as_of", TS, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("cash_collateral", MONEY, nullable=False),
        sa.Column("unrealized_pnl", MONEY, nullable=False),
        sa.Column("equity", MONEY, nullable=False),
        sa.Column("available_margin", MONEY, nullable=True),
        sa.Column("used_margin", MONEY, nullable=True),
        sa.Column("external_cashflow_cum", MONEY, nullable=False),
        sa.Column("unit_value", FRACTION, nullable=True),
        sa.Column("raw", JSON_DOC, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_scope", "equity_version", name="uq_account_snapshot_version"),
    )
    op.create_table(
        "consumer_offsets",
        sa.Column("consumer_name", sa.String(length=64), nullable=False),
        sa.Column("last_event_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("consumer_name"),
    )
    op.create_table(
        "data_quality_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("inst_id", sa.String(length=64), nullable=True),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("details", JSON_DOC, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dq_events_time", "data_quality_events", ["occurred_at"], unique=False)
    op.create_table(
        "decisions",
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("cutoff_at", TS, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reason_codes", JSON_DOC, nullable=False),
        sa.Column("model_id", sa.String(length=64), nullable=True),
        sa.Column("universe_version", sa.String(length=64), nullable=True),
        sa.Column("equity_version", sa.String(length=64), nullable=True),
        sa.Column("inputs_hash", sa.String(length=64), nullable=True),
        sa.Column("forecasts", JSON_DOC, nullable=False),
        sa.Column("edges", JSON_DOC, nullable=False),
        sa.Column("rejected_alternatives", JSON_DOC, nullable=False),
        sa.Column("timings_ms", JSON_DOC, nullable=False),
        sa.Column("software_version", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("decision_id"),
    )
    op.create_index("ix_decisions_cutoff", "decisions", ["cutoff_at"], unique=False)
    op.create_table(
        "experiment_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("plan", JSON_DOC, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trials", JSON_DOC, nullable=False),
        sa.Column("final_test_consulted_at", TS, nullable=True),
        sa.Column("code_commit", sa.String(length=64), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "feature_schemas",
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("names", JSON_DOC, nullable=False),
        sa.Column("definitions", JSON_DOC, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("schema_hash"),
    )
    op.create_table(
        "instrument_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("inst_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("valid_from", TS, nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("settle_ccy", sa.String(length=16), nullable=False),
        sa.Column("base_ccy", sa.String(length=32), nullable=False),
        sa.Column("contract_type", sa.String(length=16), nullable=False),
        sa.Column("base_units_per_contract", QTY, nullable=False),
        sa.Column("tick_size", PRICE, nullable=False),
        sa.Column("lot_size", QTY, nullable=False),
        sa.Column("min_size", QTY, nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("max_leverage", QTY, nullable=True),
        sa.Column("provenance", sa.String(length=256), nullable=False),
        sa.Column("raw", JSON_DOC, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inst_id", "version", name="uq_instrument_version"),
        sa.CheckConstraint("tick_size > 0 AND lot_size > 0 AND min_size > 0", name="ck_instr_grid_positive"),
        sa.CheckConstraint("base_units_per_contract > 0", name="ck_instr_v_positive"),
    )
    op.create_index(
        "ix_instrument_versions_inst_valid", "instrument_versions", ["inst_id", "valid_from"], unique=False
    )
    op.create_table(
        "operator_actions",
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("requested_at", TS, nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("observed_result", JSON_DOC, nullable=False),
        sa.Column("completed_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index("ix_operator_actions_time", "operator_actions", ["requested_at"], unique=False)
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", JSON_DOC, nullable=False),
        sa.Column("idempotency_key", sa.String(length=192), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_until", TS, nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("processed_at", TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_idempotency"),
    )
    op.create_index("ix_outbox_pending", "outbox_events", ["status", "created_at"], unique=False)
    op.create_table(
        "position_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_scope", sa.String(length=64), nullable=False),
        sa.Column("inst_id", sa.String(length=64), nullable=False),
        sa.Column("as_of", TS, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("signed_base_qty", QTY, nullable=False),
        sa.Column("signed_contracts", QTY, nullable=False),
        sa.Column("average_entry_price", PRICE, nullable=False),
        sa.Column("mark_price", PRICE, nullable=True),
        sa.Column("liquidation_price", PRICE, nullable=True),
        sa.Column("margin", MONEY, nullable=True),
        sa.Column("leverage", QTY, nullable=True),
        sa.Column("protection", JSON_DOC, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_position_snapshots_latest",
        "position_snapshots",
        ["account_scope", "inst_id", "as_of"],
        unique=False,
    )
    op.create_table(
        "raw_partitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column("inst_id", sa.String(length=64), nullable=True),
        sa.Column("partition_key", sa.String(length=128), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=False),
        sa.Column("first_ts", TS, nullable=True),
        sa.Column("last_ts", TS, nullable=True),
        sa.Column("written_at", TS, nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset", "partition_key", name="uq_raw_partition"),
    )
    op.create_table(
        "risk_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("affected_scope", sa.String(length=128), nullable=False),
        sa.Column("evidence", JSON_DOC, nullable=False),
        sa.Column("requested_action", sa.String(length=64), nullable=False),
        sa.Column("observed_result", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_risk_events_time", "risk_events", ["created_at"], unique=False)
    op.create_table(
        "risk_state",
        sa.Column("account_scope", sa.String(length=64), nullable=False),
        sa.Column("halt_level", sa.String(length=24), nullable=False),
        sa.Column("halt_reason", sa.String(length=256), nullable=True),
        sa.Column("halt_since", TS, nullable=True),
        sa.Column("utc_day", TS, nullable=True),
        sa.Column("day_start_equity", MONEY, nullable=True),
        sa.Column("day_realized_loss", MONEY, nullable=False),
        sa.Column("high_water_mark_unit", FRACTION, nullable=True),
        sa.Column("high_water_mark_equity", MONEY, nullable=True),
        sa.Column("limits_version", sa.String(length=64), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("account_scope"),
    )
    op.create_table(
        "runtime_leases",
        sa.Column("lease_name", sa.String(length=64), nullable=False),
        sa.Column("holder", sa.String(length=128), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", TS, nullable=True),
        sa.Column("heartbeat_at", TS, nullable=True),
        sa.Column("expires_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("lease_name"),
    )
    op.create_table(
        "source_documents",
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("deduplication_id", sa.String(length=128), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.PrimaryKeyConstraint("document_id"),
        sa.UniqueConstraint("source", "deduplication_id", name="uq_source_dedup"),
    )
    op.create_table(
        "universe_snapshots",
        sa.Column("universe_version", sa.String(length=64), nullable=False),
        sa.Column("computed_at", TS, nullable=False),
        sa.Column("valid_from", TS, nullable=False),
        sa.Column("eligible", JSON_DOC, nullable=False),
        sa.Column("held_only", JSON_DOC, nullable=False),
        sa.Column("criteria", JSON_DOC, nullable=False),
        sa.Column("bias_note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("universe_version"),
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("published_at", TS, nullable=True),
        sa.Column("date_method", sa.String(length=64), nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("parsed_at", TS, nullable=True),
        sa.Column("raw_text_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("asset_mapping", JSON_DOC, nullable=False),
        sa.Column("asset_mapping_version", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["source_documents.document_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version", name="uq_document_version"),
    )
    op.create_table(
        "model_versions",
        sa.Column("model_id", sa.String(length=64), nullable=False),
        sa.Column("family", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("code_commit", sa.String(length=64), nullable=True),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
        sa.Column("feature_schema_hash", sa.String(length=64), nullable=True),
        sa.Column("period_start", TS, nullable=False),
        sa.Column("period_end", TS, nullable=False),
        sa.Column("universe_version", sa.String(length=64), nullable=True),
        sa.Column("manifest", JSON_DOC, nullable=False),
        sa.Column("artifact_path", sa.String(length=512), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("promoted_at", TS, nullable=True),
        sa.Column("promoted_by", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["feature_schema_hash"], ["feature_schemas.schema_hash"]),
        sa.PrimaryKeyConstraint("model_id"),
    )
    op.create_table(
        "portfolio_targets",
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("equity_version", sa.String(length=64), nullable=False),
        sa.Column("signed_weights", JSON_DOC, nullable=False),
        sa.Column("constraints_version", sa.String(length=64), nullable=False),
        sa.Column("solver_status", sa.String(length=32), nullable=False),
        sa.Column("solver_report", JSON_DOC, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.decision_id"]),
        sa.PrimaryKeyConstraint("target_id"),
    )
    op.create_table(
        "evaluation_reports",
        sa.Column("report_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("period_start", TS, nullable=False),
        sa.Column("period_end", TS, nullable=False),
        sa.Column("independent", sa.Boolean(), nullable=False),
        sa.Column("metrics", JSON_DOC, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["model_versions.model_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["experiment_runs.run_id"]),
        sa.PrimaryKeyConstraint("report_id"),
    )
    op.create_table(
        "jev_requests",
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("question_hash", sa.String(length=64), nullable=False),
        sa.Column("asset_mapping_version", sa.String(length=64), nullable=False),
        sa.Column("requested_at", TS, nullable=False),
        sa.Column("deadline_at", TS, nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_table(
        "order_intents",
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("account_scope", sa.String(length=64), nullable=False),
        sa.Column("inst_id", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("contracts", QTY, nullable=False),
        sa.Column("price_limit", PRICE, nullable=True),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("ttl_ms", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.decision_id"]),
        sa.ForeignKeyConstraint(["target_id"], ["portfolio_targets.target_id"]),
        sa.PrimaryKeyConstraint("intent_id"),
        sa.UniqueConstraint("account_scope", "client_order_id", name="uq_intent_client_order_id"),
        sa.CheckConstraint("contracts > 0", name="ck_intent_contracts_positive"),
        sa.CheckConstraint("ttl_ms > 0", name="ck_intent_ttl_positive"),
    )
    op.create_table(
        "execution_reservations",
        sa.Column("reservation_id", sa.String(length=64), nullable=False),
        sa.Column("account_scope", sa.String(length=64), nullable=False),
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("inst_id", sa.String(length=64), nullable=False),
        sa.Column("signed_contracts", QTY, nullable=False),
        sa.Column("notional_usdt", MONEY, nullable=False),
        sa.Column("pessimistic", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("released_at", TS, nullable=True),
        sa.Column("release_reason", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["intent_id"], ["order_intents.intent_id"]),
        sa.PrimaryKeyConstraint("reservation_id"),
    )
    op.create_index(
        "ix_reservations_active", "execution_reservations", ["account_scope", "status"], unique=False
    )
    op.create_table(
        "jev_results",
        sa.Column("evaluation_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("document_version_id", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("model_effective", sa.String(length=64), nullable=True),
        sa.Column("question_hash", sa.String(length=64), nullable=False),
        sa.Column("asset_mapping_version", sa.String(length=64), nullable=False),
        sa.Column("requested_at", TS, nullable=False),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("inference_completed_at", TS, nullable=True),
        sa.Column("features_committed_at", TS, nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("answers", JSON_DOC, nullable=False),
        sa.Column("usage", JSON_DOC, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "cleaning_pipeline_version",
            sa.String(length=64),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
        sa.Column("inst_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.ForeignKeyConstraint(["request_id"], ["jev_requests.request_id"]),
        sa.PrimaryKeyConstraint("evaluation_id"),
        sa.UniqueConstraint(
            "model_version",
            "question_hash",
            "document_version_id",
            "asset_mapping_version",
            "cleaning_pipeline_version",
            name="uq_jev_semantic_cache",
        ),
    )
    op.create_index("ix_jev_results_committed", "jev_results", ["features_committed_at"], unique=False)
    op.create_table(
        "risk_approvals",
        sa.Column("approval_id", sa.String(length=64), nullable=False),
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("intent_hash", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("allowed_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("allowed_contracts", QTY, nullable=True),
        sa.Column("limits_version", sa.String(length=64), nullable=False),
        sa.Column("position_version", sa.String(length=64), nullable=False),
        sa.Column("reservations", JSON_DOC, nullable=False),
        sa.Column("reason_codes", JSON_DOC, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["intent_id"], ["order_intents.intent_id"]),
        sa.PrimaryKeyConstraint("approval_id"),
    )
    op.create_index("ix_risk_approvals_intent", "risk_approvals", ["intent_id"], unique=False)
    op.create_table(
        "orders",
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("account_scope", sa.String(length=64), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("approval_id", sa.String(length=64), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=64), nullable=True),
        sa.Column("inst_id", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("contracts", QTY, nullable=False),
        sa.Column("price_limit", PRICE, nullable=True),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("observed_state", sa.String(length=24), nullable=False),
        sa.Column("pending_operation", sa.String(length=16), nullable=False),
        sa.Column("cumulative_filled", QTY, nullable=False),
        sa.Column("average_fill_price", PRICE, nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("sent_payload", JSON_DOC, nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("sent_at", TS, nullable=True),
        sa.Column("ack_at", TS, nullable=True),
        sa.Column("terminal_at", TS, nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["approval_id"], ["risk_approvals.approval_id"]),
        sa.ForeignKeyConstraint(["intent_id"], ["order_intents.intent_id"]),
        sa.PrimaryKeyConstraint("order_id"),
        sa.UniqueConstraint("account_scope", "client_order_id", name="uq_orders_client_order_id"),
        sa.CheckConstraint("contracts > 0", name="ck_orders_contracts_positive"),
        sa.CheckConstraint("cumulative_filled >= 0", name="ck_orders_filled_nonneg"),
    )
    op.create_index("ix_orders_exchange_id", "orders", ["exchange_order_id"], unique=False)
    op.create_index("ix_orders_state", "orders", ["account_scope", "observed_state"], unique=False)
    op.create_table(
        "fills",
        sa.Column("execution_key", sa.String(length=192), nullable=False),
        sa.Column("account_scope", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=64), nullable=True),
        sa.Column("trade_id", sa.String(length=64), nullable=True),
        sa.Column("inst_id", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("contracts", QTY, nullable=False),
        sa.Column("fill_price", PRICE, nullable=False),
        sa.Column("fee_cashflow", MONEY, nullable=False),
        sa.Column("fee_ccy", sa.String(length=16), nullable=False),
        sa.Column("liquidity", sa.String(length=8), nullable=False),
        sa.Column("fill_at", TS, nullable=False),
        sa.Column("receive_ts", TS, nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.order_id"]),
        sa.PrimaryKeyConstraint("execution_key"),
        sa.CheckConstraint("contracts > 0", name="ck_fills_contracts_positive"),
        sa.CheckConstraint("fill_price > 0", name="ck_fills_price_positive"),
    )
    op.create_index("ix_fills_order", "fills", ["order_id"], unique=False)
    op.create_index("ix_fills_time", "fills", ["account_scope", "fill_at"], unique=False)
    op.create_table(
        "order_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("account_scope", sa.String(length=64), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=64), nullable=True),
        sa.Column("event_kind", sa.String(length=16), nullable=False),
        sa.Column("observed_state", sa.String(length=24), nullable=False),
        sa.Column("cumulative_filled", QTY, nullable=False),
        sa.Column("event_ts", TS, nullable=True),
        sa.Column("receive_ts", TS, nullable=False),
        sa.Column("raw_hash", sa.String(length=64), nullable=True),
        sa.Column("raw", JSON_DOC, nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.order_id"]),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("account_scope", "client_order_id", "raw_hash", name="uq_order_event_dedup"),
    )
    op.create_index("ix_order_events_order", "order_events", ["order_id"], unique=False)
    op.create_table(
        "ledger_transactions",
        sa.Column("txn_id", sa.String(length=64), nullable=False),
        sa.Column("account_scope", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=192), nullable=False),
        sa.Column("fill_key", sa.String(length=192), nullable=True),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False),
        sa.Column("metadata", JSON_DOC, nullable=False),
        sa.ForeignKeyConstraint(["fill_key"], ["fills.execution_key"]),
        sa.PrimaryKeyConstraint("txn_id"),
        sa.UniqueConstraint("account_scope", "idempotency_key", name="uq_ledger_txn_idempotency"),
    )
    op.create_index(
        "ix_ledger_txn_time", "ledger_transactions", ["account_scope", "occurred_at"], unique=False
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("txn_id", sa.String(length=64), nullable=False),
        sa.Column("account", sa.String(length=64), nullable=False),
        sa.Column("ccy", sa.String(length=16), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("inst_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["txn_id"], ["ledger_transactions.txn_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ledger_entries_txn", "ledger_entries", ["txn_id"], unique=False)


def downgrade() -> None:
    """Supprime tout le schéma. Destructif : exige une sauvegarde vérifiée (§44)."""
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("order_events")
    op.drop_table("fills")
    op.drop_table("orders")
    op.drop_table("risk_approvals")
    op.drop_table("jev_results")
    op.drop_table("execution_reservations")
    op.drop_table("order_intents")
    op.drop_table("jev_requests")
    op.drop_table("evaluation_reports")
    op.drop_table("portfolio_targets")
    op.drop_table("model_versions")
    op.drop_table("document_versions")
    op.drop_table("universe_snapshots")
    op.drop_table("source_documents")
    op.drop_table("runtime_leases")
    op.drop_table("risk_state")
    op.drop_table("risk_events")
    op.drop_table("raw_partitions")
    op.drop_table("position_snapshots")
    op.drop_table("outbox_events")
    op.drop_table("operator_actions")
    op.drop_table("instrument_versions")
    op.drop_table("feature_schemas")
    op.drop_table("experiment_runs")
    op.drop_table("decisions")
    op.drop_table("data_quality_events")
    op.drop_table("consumer_offsets")
    op.drop_table("account_snapshots")
