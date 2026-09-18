"""Lectures SQLAlchemy pour l'API : rien d'autre que des ``SELECT``.

Règles :
- une métrique qui ne peut pas être calculée à partir des tables vaut ``None`` (« Non disponible »),
  jamais 0 ;
- les montants restent des ``Decimal`` ici ; la conversion en flottant n'a lieu que dans la couche de
  compatibilité, pour l'affichage ;
- les dérivations (positions clôturées, indicateurs de performance) sont explicitement nommées comme
  telles : elles viennent des instantanés de position, pas d'un relevé d'exchange.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from okxq.api.serialize import as_iso, to_jsonable
from okxq.config.schema import AppConfig
from okxq.domain.clocks import Clock, SystemClock, utc_day
from okxq.persistence.models import (
    AccountSnapshot,
    DataQualityEvent,
    Decision,
    DocumentVersion,
    EvaluationReport,
    ExperimentRun,
    FillRow,
    InstrumentVersion,
    JevRequest,
    JevResult,
    ModelVersion,
    OperatorAction,
    OrderEventRow,
    OrderIntentRow,
    OrderRow,
    OutboxEvent,
    PortfolioTargetRow,
    PositionSnapshot,
    RawPartition,
    RiskEventRow,
    RiskState,
    RuntimeLease,
    SourceDocumentRow,
)
from okxq.runtime.health import Status

MAX_LIMIT = 500


def clamp(limit: int | None, default: int = 100) -> int:
    if limit is None:
        return default
    return max(1, min(MAX_LIMIT, int(limit)))


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    """Dérivée des instantanés : ouverture = premier instantané non nul de la série, clôture = premier
    instantané nul qui suit. Le PnL est ``None`` sans prix de marque au moment de la clôture."""

    inst_id: str
    side: str
    leverage: Decimal | None
    entry_price: Decimal
    close_price: Decimal | None
    open_time: datetime
    close_time: datetime
    pnl: Decimal | None
    pnl_ratio: Decimal | None
    source: str = "derived:position_snapshots"


class ReadModel:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        cfg: AppConfig,
        clock: Clock | None = None,
    ) -> None:
        self._factory = session_factory
        self._cfg = cfg
        self._clock = clock or SystemClock()
        self.account_scope = cfg.account.scope

    def now(self) -> datetime:
        return self._clock.now_utc()

    def session(self) -> Session:
        return self._factory()

    def db_ok(self) -> tuple[bool, str]:
        try:
            with self.session() as s:
                s.execute(text("SELECT 1"))
            return True, "base joignable"
        except Exception as exc:
            return False, f"base injoignable : {type(exc).__name__}"

    # --- compte et positions --------------------------------------------------------------------------

    def latest_account(self) -> AccountSnapshot | None:
        with self.session() as s:
            return s.execute(
                select(AccountSnapshot)
                .where(AccountSnapshot.account_scope == self.account_scope)
                .order_by(AccountSnapshot.as_of.desc(), AccountSnapshot.id.desc())
                .limit(1)
            ).scalar_one_or_none()

    def account_series(self, limit: int = 2000) -> list[AccountSnapshot]:
        with self.session() as s:
            rows = s.execute(
                select(AccountSnapshot)
                .where(AccountSnapshot.account_scope == self.account_scope)
                .order_by(AccountSnapshot.as_of.desc(), AccountSnapshot.id.desc())
                .limit(limit)
            ).scalars()
            return list(reversed(list(rows)))

    def _all_position_rows(self, s: Session) -> list[PositionSnapshot]:
        return list(
            s.execute(
                select(PositionSnapshot)
                .where(PositionSnapshot.account_scope == self.account_scope)
                .order_by(PositionSnapshot.inst_id, PositionSnapshot.as_of, PositionSnapshot.id)
            ).scalars()
        )

    def latest_positions(self) -> list[PositionSnapshot]:
        """Dernier instantané par instrument (ouvert ou non)."""
        with self.session() as s:
            latest: dict[str, PositionSnapshot] = {}
            for row in self._all_position_rows(s):
                latest[row.inst_id] = row
            return sorted(latest.values(), key=lambda r: r.inst_id)

    def open_positions(self) -> list[tuple[PositionSnapshot, datetime | None]]:
        """Positions non nulles avec leur date d'ouverture dérivée (début de la série non nulle)."""
        with self.session() as s:
            rows = self._all_position_rows(s)
        open_since: dict[str, datetime | None] = {}
        latest: dict[str, PositionSnapshot] = {}
        for row in rows:
            if row.signed_base_qty == 0:
                open_since[row.inst_id] = None
            elif open_since.get(row.inst_id) is None:
                open_since[row.inst_id] = row.as_of
            latest[row.inst_id] = row
        return [
            (row, open_since.get(inst)) for inst, row in sorted(latest.items()) if row.signed_base_qty != 0
        ]

    def closed_positions(self, limit: int = 50) -> list[ClosedPosition]:
        with self.session() as s:
            rows = self._all_position_rows(s)
        out: list[ClosedPosition] = []
        streak: dict[str, tuple[datetime, PositionSnapshot]] = {}
        for row in rows:
            current = streak.get(row.inst_id)
            if row.signed_base_qty != 0:
                streak[row.inst_id] = (current[0] if current else row.as_of, row)
                continue
            if current is None:
                continue
            opened_at, last = current
            close_px = row.mark_price if row.mark_price is not None else last.mark_price
            pnl = None if close_px is None else last.signed_base_qty * (close_px - last.average_entry_price)
            ratio = None
            if pnl is not None and last.margin is not None and last.margin != 0:
                ratio = pnl / last.margin
            out.append(
                ClosedPosition(
                    inst_id=row.inst_id,
                    side="LONG" if last.signed_base_qty > 0 else "SHORT",
                    leverage=last.leverage,
                    entry_price=last.average_entry_price,
                    close_price=close_px,
                    open_time=opened_at,
                    close_time=row.as_of,
                    pnl=pnl,
                    pnl_ratio=ratio,
                )
            )
            streak.pop(row.inst_id, None)
        out.sort(key=lambda c: c.close_time, reverse=True)
        return out[:limit]

    def base_units_per_contract(self, inst_id: str) -> Decimal | None:
        with self.session() as s:
            row = s.execute(
                select(InstrumentVersion)
                .where(InstrumentVersion.inst_id == inst_id)
                .order_by(InstrumentVersion.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            return None if row is None else row.base_units_per_contract

    # --- ordres et fills ----------------------------------------------------------------------------------

    def orders(self, limit: int = 100, state: str | None = None, inst_id: str | None = None) -> list[OrderRow]:
        with self.session() as s:
            q = select(OrderRow).where(OrderRow.account_scope == self.account_scope)
            if state:
                q = q.where(OrderRow.observed_state == state)
            if inst_id:
                q = q.where(OrderRow.inst_id == inst_id)
            q = q.order_by(OrderRow.created_at.desc()).limit(limit)
            return list(s.execute(q).scalars())

    def order_events(self, order_id: str, limit: int = 100) -> list[OrderEventRow]:
        with self.session() as s:
            return list(
                s.execute(
                    select(OrderEventRow)
                    .where(OrderEventRow.order_id == order_id)
                    .order_by(OrderEventRow.receive_ts.asc())
                    .limit(limit)
                ).scalars()
            )

    def fills(self, limit: int = 100, since: datetime | None = None, inst_id: str | None = None) -> list[FillRow]:
        with self.session() as s:
            q = select(FillRow).where(FillRow.account_scope == self.account_scope)
            if since is not None:
                q = q.where(FillRow.fill_at >= since)
            if inst_id:
                q = q.where(FillRow.inst_id == inst_id)
            q = q.order_by(FillRow.fill_at.desc()).limit(limit)
            return list(s.execute(q).scalars())

    def fills_today(self) -> list[FillRow]:
        return self.fills(limit=MAX_LIMIT, since=utc_day(self.now()))

    def fees_cumulative_by_time(self) -> list[tuple[datetime, Decimal]]:
        """Frais cumulés dans le temps (pour distinguer brut et net sur la courbe)."""
        with self.session() as s:
            rows = s.execute(
                select(FillRow.fill_at, FillRow.fee_cashflow)
                .where(FillRow.account_scope == self.account_scope)
                .order_by(FillRow.fill_at.asc())
            ).all()
        out: list[tuple[datetime, Decimal]] = []
        total = Decimal(0)
        for fill_at, fee in rows:
            total += fee
            out.append((fill_at, total))
        return out

    # --- décisions ----------------------------------------------------------------------------------------

    def decisions(
        self,
        limit: int = 100,
        since: datetime | None = None,
        outcome: str | None = None,
        instrument: str | None = None,
    ) -> list[Decision]:
        with self.session() as s:
            q = select(Decision)
            if since is not None:
                q = q.where(Decision.cutoff_at >= since)
            if outcome:
                q = q.where(Decision.outcome == outcome)
            q = q.order_by(Decision.cutoff_at.desc()).limit(limit if not instrument else MAX_LIMIT)
            rows = list(s.execute(q).scalars())
        if instrument:
            rows = [
                d
                for d in rows
                if any(str(e.get("instrument")) == instrument for e in d.edges)
                or any(str(f.get("instrument")) == instrument for f in d.forecasts)
            ][:limit]
        return rows

    def decision(self, decision_id: str) -> Decision | None:
        with self.session() as s:
            return s.get(Decision, decision_id)

    def decision_links(self, decision_ids: Sequence[str]) -> dict[str, dict[str, list[str]]]:
        """Liens de provenance : cibles de portefeuille et intentions par décision."""
        out: dict[str, dict[str, list[str]]] = {d: {"target_ids": [], "intent_ids": []} for d in decision_ids}
        if not decision_ids:
            return out
        with self.session() as s:
            for tid, did in s.execute(
                select(PortfolioTargetRow.target_id, PortfolioTargetRow.decision_id).where(
                    PortfolioTargetRow.decision_id.in_(list(decision_ids))
                )
            ).all():
                out[did]["target_ids"].append(tid)
            for iid, did in s.execute(
                select(OrderIntentRow.intent_id, OrderIntentRow.decision_id).where(
                    OrderIntentRow.decision_id.in_(list(decision_ids))
                )
            ).all():
                out[did]["intent_ids"].append(iid)
        return out

    def portfolio_targets(self, decision_id: str) -> list[PortfolioTargetRow]:
        with self.session() as s:
            return list(
                s.execute(select(PortfolioTargetRow).where(PortfolioTargetRow.decision_id == decision_id)).scalars()
            )

    # --- risque et qualité des données ----------------------------------------------------------------

    def risk_events(self, limit: int = 100, severity: str | None = None) -> list[RiskEventRow]:
        with self.session() as s:
            q = select(RiskEventRow)
            if severity:
                q = q.where(RiskEventRow.severity == severity)
            q = q.order_by(RiskEventRow.created_at.desc()).limit(limit)
            return list(s.execute(q).scalars())

    def risk_state(self) -> RiskState | None:
        with self.session() as s:
            return s.get(RiskState, self.account_scope)

    def data_quality_events(self, limit: int = 100) -> list[DataQualityEvent]:
        with self.session() as s:
            return list(
                s.execute(select(DataQualityEvent).order_by(DataQualityEvent.occurred_at.desc()).limit(limit)).scalars()
            )

    # --- recherche ----------------------------------------------------------------------------------------

    def experiments(self, limit: int = 50) -> list[ExperimentRun]:
        with self.session() as s:
            return list(
                s.execute(select(ExperimentRun).order_by(ExperimentRun.started_at.desc()).limit(limit)).scalars()
            )

    def experiment(self, run_id: str) -> ExperimentRun | None:
        with self.session() as s:
            return s.get(ExperimentRun, run_id)

    def evaluation_reports(self, run_id: str | None = None, limit: int = 200) -> list[EvaluationReport]:
        with self.session() as s:
            q = select(EvaluationReport)
            if run_id:
                q = q.where(EvaluationReport.run_id == run_id)
            q = q.order_by(EvaluationReport.created_at.desc()).limit(limit)
            return list(s.execute(q).scalars())

    def models(self, limit: int = 100) -> list[ModelVersion]:
        with self.session() as s:
            return list(s.execute(select(ModelVersion).order_by(ModelVersion.created_at.desc()).limit(limit)).scalars())

    # --- JEV -------------------------------------------------------------------------------------------------

    def jev_results(self, limit: int = 100) -> list[tuple[JevResult, DocumentVersion, SourceDocumentRow]]:
        with self.session() as s:
            rows = s.execute(
                select(JevResult, DocumentVersion, SourceDocumentRow)
                .join(DocumentVersion, JevResult.document_version_id == DocumentVersion.id)
                .join(SourceDocumentRow, DocumentVersion.document_id == SourceDocumentRow.document_id)
                .order_by(JevResult.requested_at.desc())
                .limit(limit)
            ).all()
            return [(r, dv, sd) for r, dv, sd in rows]

    def jev_sources(self) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.execute(
                select(
                    SourceDocumentRow.source,
                    func.count(SourceDocumentRow.document_id),
                    func.max(SourceDocumentRow.first_seen_at),
                ).group_by(SourceDocumentRow.source)
            ).all()
        return [{"source": src, "documents": int(n), "last_seen_at": as_iso(last)} for src, n, last in rows]

    def jev_counts(self, since: datetime) -> dict[str, int]:
        with self.session() as s:
            rows = s.execute(
                select(JevResult.status, func.count(JevResult.evaluation_id))
                .where(JevResult.requested_at >= since)
                .group_by(JevResult.status)
            ).all()
            pending = s.execute(
                select(func.count(JevRequest.request_id)).where(JevRequest.status.in_(["PENDING", "RUNNING"]))
            ).scalar_one()
        out = {str(status): int(n) for status, n in rows}
        out["pending_requests"] = int(pending)
        return out

    # --- exploitation ---------------------------------------------------------------------------------------

    def operator_actions(self, limit: int = 100, status: str | None = None) -> list[OperatorAction]:
        with self.session() as s:
            q = select(OperatorAction)
            if status:
                q = q.where(OperatorAction.status == status)
            q = q.order_by(OperatorAction.requested_at.desc()).limit(limit)
            return list(s.execute(q).scalars())

    def operator_action(self, request_id: str) -> OperatorAction | None:
        with self.session() as s:
            return s.get(OperatorAction, request_id)

    def leases(self) -> list[RuntimeLease]:
        with self.session() as s:
            return list(s.execute(select(RuntimeLease)).scalars())

    def outbox_pending(self) -> int:
        with self.session() as s:
            return int(
                s.execute(select(func.count(OutboxEvent.id)).where(OutboxEvent.status == "PENDING")).scalar_one()
            )

    def latest_decision(self) -> Decision | None:
        with self.session() as s:
            return s.execute(select(Decision).order_by(Decision.started_at.desc()).limit(1)).scalar_one_or_none()

    def latest_partition_written_at(self) -> datetime | None:
        with self.session() as s:
            return s.execute(select(func.max(RawPartition.written_at))).scalar_one_or_none()

    # --- journal (ai-log) ------------------------------------------------------------------------------------

    def journal_entries(self, since: datetime | None, limit: int = 60) -> list[dict[str, Any]]:
        """Entrées de journal fusionnées et triées par temps : décisions, risque, qualité, opérateur."""
        entries: list[dict[str, Any]] = []
        with self.session() as s:
            q_dec = select(Decision).order_by(Decision.started_at.desc()).limit(limit)
            if since is not None:
                q_dec = q_dec.where(Decision.started_at > since)
            for d in s.execute(q_dec).scalars():
                entries.append(
                    {
                        "ts": as_iso(d.started_at),
                        "event": f"DECISION_{d.outcome}",
                        "id": d.decision_id,
                        "decision_id": d.decision_id,
                        "snapshot_id": d.snapshot_id,
                        "reasons": list(d.reason_codes),
                        "edges": len(d.edges),
                        "mode": d.mode,
                    }
                )
            q_risk = select(RiskEventRow).order_by(RiskEventRow.created_at.desc()).limit(limit)
            if since is not None:
                q_risk = q_risk.where(RiskEventRow.created_at > since)
            for r in s.execute(q_risk).scalars():
                entries.append(
                    {
                        "ts": as_iso(r.created_at),
                        "event": f"RISK_{r.severity}",
                        "id": r.event_id,
                        "reason_code": r.reason_code,
                        "scope": r.affected_scope,
                        "action": r.requested_action,
                        "result": r.observed_result,
                    }
                )
            q_dq = select(DataQualityEvent).order_by(DataQualityEvent.occurred_at.desc()).limit(limit)
            if since is not None:
                q_dq = q_dq.where(DataQualityEvent.occurred_at > since)
            for e in s.execute(q_dq).scalars():
                entries.append(
                    {
                        "ts": as_iso(e.occurred_at),
                        "event": f"DATA_{e.severity}",
                        "id": str(e.id),
                        "kind": e.kind,
                        "channel": e.channel,
                        "inst_id": e.inst_id,
                    }
                )
            q_op = select(OperatorAction).order_by(OperatorAction.requested_at.desc()).limit(limit)
            if since is not None:
                q_op = q_op.where(OperatorAction.requested_at > since)
            for a in s.execute(q_op).scalars():
                entries.append(
                    {
                        "ts": as_iso(a.requested_at),
                        "event": f"OPERATOR_{a.action}",
                        "id": a.request_id,
                        "status": a.status,
                        "actor": a.actor,
                        "scope": a.scope,
                    }
                )
        entries.sort(key=lambda e: str(e.get("ts") or ""))
        return [dict(to_jsonable(e)) for e in entries[-limit:]]

    # --- composants observés depuis la base ------------------------------------------------------------

    def observe_components(self) -> dict[str, tuple[Status, str]]:
        """Ce que le service API peut constater des autres processus à travers la base.

        Ce sont des OBSERVATIONS (dernière écriture connue), pas l'auto-diagnostic de chaque service ;
        elles suffisent à dire « muet » ou « en retard », jamais « sain » au-delà de ce qu'elles voient.
        """
        now = self.now()
        out: dict[str, tuple[Status, str]] = {}
        ok, info = self.db_ok()
        out["database"] = (Status.OK if ok else Status.FAULT, info)
        if not ok:
            return out
        interval = timedelta(seconds=self._cfg.runtime.decision_interval_seconds)
        last = self.latest_decision()
        if last is None:
            out["strategy"] = (Status.WARN, "aucune décision enregistrée")
        else:
            age = now - last.started_at
            if age <= 2 * interval:
                out["strategy"] = (Status.OK, f"dernière décision {last.outcome} il y a {int(age.total_seconds())} s")
            else:
                out["strategy"] = (Status.WARN, f"aucune décision depuis {int(age.total_seconds())} s")
        rs = self.risk_state()
        if rs is None:
            out["risk"] = (Status.WARN, "état de risque absent")
        else:
            age = now - rs.updated_at
            if rs.halt_level != "NONE":
                out["risk"] = (Status.WARN, f"halt {rs.halt_level} : {rs.halt_reason or ''}".strip())
            elif age > 5 * interval:
                out["risk"] = (Status.WARN, f"état de risque non rafraîchi depuis {int(age.total_seconds())} s")
            else:
                out["risk"] = (Status.OK, f"halt NONE · limites {rs.limits_version}")
        leases = {lease.lease_name: lease for lease in self.leases()}
        writer = leases.get("execution_writer")
        if self._cfg.mode.sends_orders_to_exchange or writer is not None:
            if writer is None or writer.heartbeat_at is None:
                out["gateway"] = (Status.FAULT, "aucun bail d'écriture : gateway absent")
            else:
                age = now - writer.heartbeat_at
                ttl = timedelta(milliseconds=self._cfg.runtime.lease_ttl_ms)
                if age <= ttl:
                    out["gateway"] = (Status.OK, f"bail {writer.holder} · jeton {writer.fencing_token}")
                else:
                    out["gateway"] = (Status.FAULT, f"bail expiré depuis {int(age.total_seconds())} s")
        pending = self.outbox_pending()
        out["outbox"] = (Status.OK if pending < 100 else Status.WARN, f"{pending} en attente")
        written = self.latest_partition_written_at()
        if written is None:
            out["market_data"] = (Status.WARN, "aucune partition brute écrite")
        else:
            age = now - written
            out["market_data"] = (
                Status.OK if age <= timedelta(hours=1) else Status.WARN,
                f"dernière partition il y a {int(age.total_seconds())} s",
            )
        if self._cfg.jev.enabled:
            counts = self.jev_counts(now - timedelta(hours=24))
            errors = counts.get("error", 0) + counts.get("rejected", 0)
            total = sum(v for k, v in counts.items() if k != "pending_requests")
            if total == 0:
                out["jev"] = (Status.WARN, "aucune évaluation sur 24 h")
            elif errors * 4 > total:
                out["jev"] = (Status.WARN, f"{errors}/{total} évaluations en erreur sur 24 h")
            else:
                out["jev"] = (Status.OK, f"{total} évaluations sur 24 h · {counts['pending_requests']} en attente")
        return out
