"""Cache sémantique JEV et dépôts persistants (``source_documents``, ``document_versions``, ``jev_requests``,
``jev_results``). Fonctionne sur ``memory_engine()`` (SQLite) et PostgreSQL.

Clé du cache : (model_version, question_hash, document_version_id, asset_mapping_version,
cleaning_pipeline_version). Un hit rend l'évaluation ORIGINALE, avec ses horodatages : l'âge de l'événement
est toujours dérivé du document (``published_at`` ou, à défaut, ``first_seen_at``), jamais du cache (T57).

``JevFeatureStore.evaluations_for(inst_id, cutoff_at)`` ne rend que les évaluations ``status=ok`` dont
``features_committed_at <= cutoff_at`` : une réponse tardive (``late``) reste archivée mais n'entre jamais
dans un snapshot passé (T51).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import JevError
from okxq.domain.events import AssetMapping, JevEvaluation, JevStatus, SourceDocument
from okxq.domain.ids import payload_hash
from okxq.jev.schemas import answers_from_json, answers_to_json
from okxq.jev.source_connectors import DocumentCandidate, build_source_document
from okxq.persistence.db import session_scope
from okxq.persistence.models import DocumentVersion, JevRequest, JevResult, SourceDocumentRow

CLEANING_PIPELINE_VERSION = "clean-v1"
"""Version du pipeline de nettoyage (``normalize_text`` + parseurs bornés). À incrémenter à chaque changement."""


@dataclass(frozen=True, slots=True)
class CacheKey:
    model_version: str
    question_hash: str
    document_version_id: int
    asset_mapping_version: str
    cleaning_pipeline_version: str = CLEANING_PIPELINE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "question_hash": self.question_hash,
            "document_version_id": self.document_version_id,
            "asset_mapping_version": self.asset_mapping_version,
            "cleaning_pipeline_version": self.cleaning_pipeline_version,
        }

    def hash(self) -> str:
        return payload_hash(self.as_dict())


def asset_scoped_mapping_version(mapping_version: str, inst_id: str) -> str:
    """Version de mapping PAR ACTIF : un document multi-actifs donne une évaluation par actif."""
    return f"{mapping_version}#{inst_id}"


def _row_to_document(doc: SourceDocumentRow, version: DocumentVersion) -> SourceDocument:
    return SourceDocument(
        document_id=doc.document_id,
        source=doc.source,
        source_url=doc.source_url,
        published_at=version.published_at,
        first_seen_at=doc.first_seen_at,
        received_at=version.received_at,
        parsed_at=version.parsed_at,
        language=doc.language,
        version=version.version,
        date_method=version.date_method,
        raw_text_hash=version.raw_text_hash,
        deduplication_id=doc.deduplication_id,
        asset_mapping=[AssetMapping.model_validate(m) for m in version.asset_mapping],
        mapping_quality=None,
        title=version.title,
        text=version.text,
    )


class DocumentStore:
    """Documents versionnés : un contenu modifié (raw_text_hash) crée une nouvelle version, jamais une réécriture."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def upsert(self, candidate: DocumentCandidate) -> tuple[SourceDocument, bool]:
        with session_scope(self._factory) as session:
            doc = session.scalar(
                select(SourceDocumentRow).where(
                    SourceDocumentRow.source == candidate.source,
                    SourceDocumentRow.deduplication_id == candidate.deduplication_id,
                )
            )
            if doc is None:
                doc = SourceDocumentRow(
                    document_id=candidate.document_id,
                    source=candidate.source,
                    source_url=candidate.source_url,
                    deduplication_id=candidate.deduplication_id,
                    first_seen_at=candidate.received_at,
                    language=candidate.language,
                )
                session.add(doc)
                session.flush()
                latest = None
            else:
                latest = session.scalar(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == doc.document_id)
                    .order_by(DocumentVersion.version.desc())
                    .limit(1)
                )
            if latest is not None and latest.raw_text_hash == candidate.raw_text_hash:
                return _row_to_document(doc, latest), False
            version = 1 if latest is None else latest.version + 1
            document = build_source_document(candidate, version=version, first_seen_at=doc.first_seen_at)
            session.add(_version_row(document, candidate.asset_mapping_version))
            return document, True

    def ensure(self, document: SourceDocument, *, asset_mapping_version: str) -> int:
        """Enregistre un ``SourceDocument`` déjà construit (tests, replays) et rend l'id de sa version."""
        with session_scope(self._factory) as session:
            doc = session.get(SourceDocumentRow, document.document_id)
            if doc is None:
                doc = SourceDocumentRow(
                    document_id=document.document_id,
                    source=document.source,
                    source_url=document.source_url,
                    deduplication_id=document.deduplication_id,
                    first_seen_at=document.first_seen_at,
                    language=document.language,
                )
                session.add(doc)
                session.flush()
            row = session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == document.document_id,
                    DocumentVersion.version == document.version,
                )
            )
            if row is None:
                row = _version_row(document, asset_mapping_version)
                session.add(row)
                session.flush()
            elif row.raw_text_hash != document.raw_text_hash:
                raise JevError(
                    "version de document déjà enregistrée avec un contenu différent",
                    code="DOCUMENT_VERSION_CONFLICT",
                    document_id=document.document_id,
                    version=document.version,
                )
            return row.id

    def version_row_id(self, document_id: str, version: int) -> int | None:
        with session_scope(self._factory) as session:
            return session.scalar(
                select(DocumentVersion.id).where(
                    DocumentVersion.document_id == document_id, DocumentVersion.version == version
                )
            )

    def get(self, document_id: str, version: int | None = None) -> SourceDocument | None:
        with session_scope(self._factory) as session:
            doc = session.get(SourceDocumentRow, document_id)
            if doc is None:
                return None
            stmt = select(DocumentVersion).where(DocumentVersion.document_id == document_id)
            stmt = (
                stmt.where(DocumentVersion.version == version)
                if version is not None
                else stmt.order_by(DocumentVersion.version.desc()).limit(1)
            )
            row = session.scalar(stmt)
            return None if row is None else _row_to_document(doc, row)

    def received_before(self, before: datetime, *, window: timedelta) -> list[SourceDocument]:
        """Toutes les versions reçues dans ``[before - window, before[`` (candidats aux documents antérieurs)."""
        before = ensure_utc(before, field="before")
        with session_scope(self._factory) as session:
            rows = session.execute(
                select(SourceDocumentRow, DocumentVersion)
                .join(DocumentVersion, DocumentVersion.document_id == SourceDocumentRow.document_id)
                .where(DocumentVersion.received_at < before, DocumentVersion.received_at >= before - window)
                .order_by(DocumentVersion.received_at)
            ).all()
            return [_row_to_document(doc, ver) for doc, ver in rows]


def _version_row(document: SourceDocument, asset_mapping_version: str) -> DocumentVersion:
    return DocumentVersion(
        document_id=document.document_id,
        version=document.version,
        published_at=document.published_at,
        date_method=document.date_method,
        received_at=document.received_at,
        parsed_at=document.parsed_at,
        raw_text_hash=document.raw_text_hash,
        title=document.title,
        text=document.text,
        asset_mapping=[m.model_dump(mode="json") for m in document.asset_mapping],
        asset_mapping_version=asset_mapping_version,
    )


def _evaluation_from_rows(row: JevResult, version: DocumentVersion) -> JevEvaluation:
    return JevEvaluation(
        evaluation_id=row.evaluation_id,
        document_id=version.document_id,
        document_version=version.version,
        question_set_hash=row.question_hash,
        asset_mapping_version=row.asset_mapping_version,
        model_requested=row.model_version,
        model_effective=row.model_effective,
        requested_at=row.requested_at,
        completed_at=row.completed_at,
        inference_completed_at=row.inference_completed_at,
        features_committed_at=row.features_committed_at,
        answers=answers_from_json(row.answers),
        usage=dict(row.usage),
        status=JevStatus(row.status),
        error=row.error,
        inst_id=row.inst_id,
    )


class JevResultCache:
    """Cache sémantique persistant. Seules les évaluations ``ok`` sont des hits ; tout est archivé."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def get(self, key: CacheKey) -> JevEvaluation | None:
        with session_scope(self._factory) as session:
            row = session.scalar(self._select(key))
            if row is None or row.status != JevStatus.OK.value:
                return None
            version = session.get(DocumentVersion, row.document_version_id)
            assert version is not None
            return _evaluation_from_rows(row, version)

    def put(self, key: CacheKey, evaluation: JevEvaluation) -> None:
        with session_scope(self._factory) as session:
            row = session.scalar(self._select(key))
            if row is None:
                row = JevResult(
                    evaluation_id=evaluation.evaluation_id,
                    document_version_id=key.document_version_id,
                    model_version=key.model_version,
                    question_hash=key.question_hash,
                    asset_mapping_version=key.asset_mapping_version,
                    cleaning_pipeline_version=key.cleaning_pipeline_version,
                    requested_at=evaluation.requested_at,
                    status=evaluation.status.value,
                )
                session.add(row)
            elif row.status == JevStatus.OK.value and evaluation.status is not JevStatus.OK:
                # Une évaluation valide n'est jamais écrasée par un échec ultérieur.
                return
            row.evaluation_id = evaluation.evaluation_id
            row.model_effective = evaluation.model_effective
            row.requested_at = evaluation.requested_at
            row.completed_at = evaluation.completed_at
            row.inference_completed_at = evaluation.inference_completed_at
            row.features_committed_at = evaluation.features_committed_at
            row.status = evaluation.status.value
            row.answers = answers_to_json(evaluation.answers)
            row.usage = dict(evaluation.usage)
            row.error = evaluation.error
            row.inst_id = evaluation.inst_id

    @staticmethod
    def _select(key: CacheKey) -> Any:
        return select(JevResult).where(
            JevResult.model_version == key.model_version,
            JevResult.question_hash == key.question_hash,
            JevResult.document_version_id == key.document_version_id,
            JevResult.asset_mapping_version == key.asset_mapping_version,
            JevResult.cleaning_pipeline_version == key.cleaning_pipeline_version,
        )

    def record_request(
        self,
        *,
        request_id: str,
        key: CacheKey,
        requested_at: datetime,
        deadline_at: datetime,
        payload_hash_value: str,
        status: str = "pending",
    ) -> None:
        with session_scope(self._factory) as session:
            session.add(
                JevRequest(
                    request_id=request_id,
                    document_version_id=key.document_version_id,
                    model_version=key.model_version,
                    question_hash=key.question_hash,
                    asset_mapping_version=key.asset_mapping_version,
                    requested_at=requested_at,
                    deadline_at=deadline_at,
                    payload_hash=payload_hash_value,
                    status=status,
                    attempts=0,
                )
            )

    def finish_request(self, request_id: str, *, status: str, attempts: int) -> None:
        with session_scope(self._factory) as session:
            row = session.get(JevRequest, request_id)
            if row is not None:
                row.status = status
                row.attempts = attempts

    def count(self, *, status: str | None = None) -> int:
        with session_scope(self._factory) as session:
            rows = session.scalars(select(JevResult)).all()
            return len([r for r in rows if status is None or r.status == status])


@dataclass(frozen=True, slots=True)
class JevFeatureRecord:
    """Évaluation utilisable comme feature, avec la datation du document (jamais celle du cache)."""

    evaluation: JevEvaluation
    inst_id: str
    document_id: str
    document_version: int
    published_at: datetime | None
    first_seen_at: datetime
    received_at: datetime
    date_method: str

    @property
    def age_basis(self) -> str:
        return "published_at" if self.published_at is not None else "first_seen_at"

    def event_age(self, at: datetime) -> timedelta:
        origin = self.published_at if self.published_at is not None else self.first_seen_at
        return ensure_utc(at, field="at") - origin


class JevFeatureStore:
    """Lecture point-in-time des évaluations : ``status=ok`` et ``features_committed_at <= cutoff_at`` seulement."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def evaluations_for(
        self, inst_id: str, cutoff_at: datetime, *, max_age: timedelta | None = None
    ) -> list[JevFeatureRecord]:
        cutoff_at = ensure_utc(cutoff_at, field="cutoff_at")
        with session_scope(self._factory) as session:
            rows = session.execute(
                select(JevResult, DocumentVersion, SourceDocumentRow)
                .join(DocumentVersion, DocumentVersion.id == JevResult.document_version_id)
                .join(SourceDocumentRow, SourceDocumentRow.document_id == DocumentVersion.document_id)
                .where(
                    JevResult.status == JevStatus.OK.value,
                    JevResult.features_committed_at.is_not(None),
                    JevResult.features_committed_at <= cutoff_at,
                )
                .order_by(JevResult.features_committed_at)
            ).all()
            out: list[JevFeatureRecord] = []
            for result, version, doc in rows:
                mapped = result.inst_id == inst_id or any(
                    m.get("inst_id") == inst_id for m in version.asset_mapping
                )
                if not mapped:
                    continue
                record = JevFeatureRecord(
                    evaluation=_evaluation_from_rows(result, version),
                    inst_id=inst_id,
                    document_id=doc.document_id,
                    document_version=version.version,
                    published_at=version.published_at,
                    first_seen_at=doc.first_seen_at,
                    received_at=version.received_at,
                    date_method=version.date_method,
                )
                if max_age is not None and record.event_age(cutoff_at) > max_age:
                    continue
                out.append(record)
            return out
