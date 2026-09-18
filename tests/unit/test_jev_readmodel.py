"""Chemin de lecture JEV avec de VRAIES lignes en base.

Ce chemin n'était exercé que sur une base vide : la jointure
``jev_results ⋈ document_versions ⋈ source_documents`` et la projection vers le dictionnaire
d'événement n'avaient jamais tourné sur des données. Une base vide fait passer n'importe quelle
requête — y compris une requête cassée.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from okxq.api.readmodel import JEV_SOURCE_FRESH_SECONDS, JEV_SOURCE_STALE_SECONDS, ReadModel
from okxq.api.routes.jev import jev_event_to_dict
from okxq.config import load_config
from okxq.domain.clocks import SimulatedClock
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import Base, DocumentVersion, JevResult, SourceDocumentRow

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "tests" / "fixtures" / "configs" / "smoke.fixture.yaml"
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def cfg():
    return load_config(CONFIG)


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed(factory, *, seen_at: datetime, status: str = "ok") -> None:
    with factory() as session:
        session.add(
            SourceDocumentRow(
                document_id="doc1",
                source="okx-annonces",
                source_url="https://exemple.invalid/annonce",
                deduplication_id="dedup-1",
                first_seen_at=seen_at,
                language="fr",
            )
        )
        # La version référence le document : il doit exister avant l'insertion.
        session.flush()
        version = DocumentVersion(
            document_id="doc1",
            version=1,
            published_at=seen_at,
            date_method="entete_http",
            received_at=seen_at,
            raw_text_hash="hash_texte",
            title="Annonce de test",
            text="corps de l'annonce",
            asset_mapping=[{"inst_id": "BTC-USDT-SWAP", "symbol": "BTC"}],
            asset_mapping_version="map-v1",
        )
        session.add(version)
        session.flush()
        session.add(
            JevResult(
                evaluation_id="ev1",
                request_id=None,
                document_version_id=version.id,
                model_version="jev-1.13.0",
                model_effective="jev-1.13.0",
                question_hash="hash_question",
                asset_mapping_version="map-v1",
                requested_at=seen_at,
                completed_at=seen_at + timedelta(milliseconds=800),
                features_committed_at=seen_at + timedelta(seconds=1),
                status=status,
                answers={"event_type": {"choice": "listing"}},
                usage={"input_tokens": 120, "output_tokens": 30},
            )
        )
        session.commit()


def test_the_jev_join_returns_its_rows(cfg, factory) -> None:
    _seed(factory, seen_at=T0)
    rows = ReadModel(factory, cfg=cfg, clock=SimulatedClock(T0)).jev_results(limit=10)
    assert len(rows) == 1
    result, version, document = rows[0]
    assert result.evaluation_id == "ev1"
    assert version.document_id == document.document_id == "doc1"


def test_an_event_projects_without_leaking_the_document_body(cfg, factory) -> None:
    """L'API expose le TITRE et le mapping, jamais le corps du document.

    Le texte intégral appartient à sa source ; le relayer dans une réponse d'API en ferait une
    rediffusion, et ce n'est pas le rôle de cette plateforme.
    """
    _seed(factory, seen_at=T0)
    rows = ReadModel(factory, cfg=cfg, clock=SimulatedClock(T0)).jev_results(limit=10)
    payload = jev_event_to_dict(T0 + timedelta(minutes=5), *rows[0])
    assert payload["title"] == "Annonce de test"
    assert "corps de l'annonce" not in str(payload)
    assert payload["status"] == "ok"
    assert payload["latency_ms"] == 800
    assert payload["age_s"] == 300


@pytest.mark.parametrize(
    ("age_seconds", "attendu"),
    [
        (0, "fresh"),
        (JEV_SOURCE_FRESH_SECONDS - 1, "fresh"),
        (JEV_SOURCE_FRESH_SECONDS + 1, "late"),
        (JEV_SOURCE_STALE_SECONDS + 1, "stale"),
    ],
)
def test_source_freshness_is_derived_from_the_observed_age(
    cfg, factory, age_seconds: int, attendu: str
) -> None:
    _seed(factory, seen_at=T0 - timedelta(seconds=age_seconds))
    sources = ReadModel(factory, cfg=cfg, clock=SimulatedClock(T0)).jev_sources()
    assert len(sources) == 1
    assert sources[0]["age_seconds"] == age_seconds
    assert sources[0]["status"] == attendu
    assert sources[0]["documents"] == 1


def test_a_source_that_never_delivered_has_no_status(cfg, factory) -> None:
    """Une absence de livraison n'est pas de l'obsolescence : le statut reste `None`."""
    sources = ReadModel(factory, cfg=cfg, clock=SimulatedClock(T0)).jev_sources()
    assert sources == []


def test_errors_are_counted_but_absent_measurement_stays_null(cfg, factory) -> None:
    """Sans aucune évaluation, le compte d'erreurs est `None` : pas de mesure, pas de zéro."""
    from okxq.api.auth import AuditTrail
    from okxq.api.context import ApiContext
    from okxq.api.routes.jev import jev_status_payload
    from okxq.api.sse import EventBus, JournalPoller
    from okxq.runtime.health import HealthRegistry
    from okxq.runtime.metrics import Metrics

    clock = SimulatedClock(T0)

    def context() -> ApiContext:
        bus = EventBus()
        readmodel = ReadModel(factory, cfg=cfg, clock=clock)
        return ApiContext(
            cfg=cfg,
            session_factory=factory,
            readmodel=readmodel,
            health=HealthRegistry(clock=clock, mode=cfg.mode.value),
            metrics=Metrics(),
            bus=bus,
            audit=AuditTrail(factory, bus=bus, clock=clock),
            clock=clock,
            alerts=None,
            poller=JournalPoller(readmodel, bus),
            frontend_dir=None,
            synthetic_data=True,
        )

    vide = jev_status_payload(context())
    assert vide["errors"] is None, "aucune évaluation ⇒ erreurs non mesurées, pas nulles"
    assert vide["age_seconds"] is None
    assert vide["latency_p95_ms"] is None

    _seed(factory, seen_at=T0, status="error")
    plein = jev_status_payload(context())
    assert plein["evaluations_24h"] == 1
    assert plein["errors"] == 1, "une évaluation en erreur doit être comptée"
    assert plein["age_seconds"] == 0
