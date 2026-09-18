"""T68 : saturation d'une file → contre-pression et COMPTAGE, jamais de perte silencieuse.

Une file bornée est un choix délibéré : sans borne, une rafale de marché fait grandir la mémoire
jusqu'à ce que le système soit tué par l'OS, et on perd tout au lieu de perdre un peu. Mais une file
bornée n'est acceptable qu'à une condition — que l'abandon soit COMPTÉ et VISIBLE. Une perte
silencieuse est pire qu'un plantage : le système continue de décider sur des données trouées en
paraissant sain.

Ce test vérifie les deux moitiés : la borne tient, et l'abandon laisse une trace.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from okxq.data.collector import PublicCollector
from okxq.data.quality import DataQualityTracker
from okxq.domain.clocks import SimulatedClock
from okxq.domain.events import EventEnvelope
from okxq.domain.ids import payload_hash
from okxq.exchange.okx.websocket_public import SubscriptionArg

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
TAILLE = 4


async def _never_connect(url: str) -> Any:  # pragma: no cover - jamais appelé : aucun réseau ici
    raise AssertionError("aucune connexion ne doit être ouverte par ce test")


def _collector(maxsize: int = TAILLE) -> PublicCollector:
    return PublicCollector(
        clock=SimulatedClock(T0),
        subscriptions=[SubscriptionArg(channel="books", inst_id=INST)],
        connect=_never_connect,
        url="wss://exemple.invalid/ws/v5/public",
        quality=DataQualityTracker(),
        queue_maxsize=maxsize,
    )


def _envelope(index: int) -> EventEnvelope:
    moment = T0 + timedelta(milliseconds=index)
    payload = {"instId": INST, "bids": [], "asks": []}
    return EventEnvelope(
        event_id=f"evt_{index:04d}",
        event_type="book.update",
        source="test",
        schema_version=1,
        exchange_ts=moment,
        receive_ts=moment,
        available_at=moment,
        ingest_seq=index,
        payload_hash=payload_hash(payload),
        payload=payload,
    )


# --- file de collecte -------------------------------------------------------------------------------


def test_T68_the_queue_never_grows_beyond_its_bound() -> None:
    """Sans borne, une rafale fait grandir la mémoire jusqu'à ce que l'OS tue le processus : on perd
    alors TOUT au lieu de perdre un peu."""
    collector = _collector()
    asyncio.run(collector.publish([_envelope(i) for i in range(TAILLE * 5)]))
    assert collector.queue.qsize() == TAILLE


def test_T68_dropped_messages_are_counted_and_attributed() -> None:
    """Le comptage est la condition qui rend la borne acceptable. Un abandon non compté, c'est un
    trou dans les données que personne ne verra jamais."""
    collector = _collector()
    total = TAILLE * 3
    asyncio.run(collector.publish([_envelope(i) for i in range(total)]))
    assert collector.stats.dropped == total - TAILLE
    # La qualité des données porte le MOTIF de l'abandon, pas seulement son nombre.
    compteurs = collector.quality.counters
    assert compteurs.drops == total - TAILLE
    assert compteurs.by_reason.get("queue_full") == total - TAILLE


def test_T68_publishing_never_blocks_on_a_full_queue() -> None:
    """Bloquer sur une file pleine gèlerait la lecture du flux, donc la détection de rupture de
    séquence : la contre-pression doit abandonner, pas attendre."""

    async def scenario() -> int:
        collector = _collector()
        # Première affirmation, implicite : si `publish` bloquait, ce `wait_for` lèverait.
        await asyncio.wait_for(collector.publish([_envelope(i) for i in range(TAILLE * 10)]), timeout=2.0)
        return collector.stats.dropped

    abandonnes = asyncio.run(scenario())
    # Seconde affirmation, explicite : on est bien passé par le chemin de contre-pression. Sans elle,
    # le test passerait aussi sur une file assez grande pour ne jamais saturer.
    assert abandonnes == TAILLE * 10 - TAILLE


def test_T68_consuming_frees_room_again() -> None:
    """Contre-épreuve : la borne n'est pas un arrêt définitif. Une fois consommée, la place revient."""
    collector = _collector()
    asyncio.run(collector.publish([_envelope(i) for i in range(TAILLE)]))
    assert collector.stats.dropped == 0, "aucun abandon tant que la file n'est pas pleine"
    asyncio.run(collector.queue.get())
    asyncio.run(collector.publish([_envelope(99)]))
    assert collector.stats.dropped == 0
    assert collector.queue.qsize() == TAILLE


def test_T68_the_drop_does_not_corrupt_what_was_kept() -> None:
    """Les messages conservés restent les PREMIERS arrivés, dans l'ordre. Une file qui évince
    arbitrairement rendrait la reprise de séquence impossible à raisonner."""
    collector = _collector()
    asyncio.run(collector.publish([_envelope(i) for i in range(TAILLE * 2)]))
    gardes = [asyncio.run(collector.queue.get()).event_id for _ in range(TAILLE)]
    assert gardes == [f"evt_{i:04d}" for i in range(TAILLE)]


# --- file du worker JEV -----------------------------------------------------------------------------


def _document(index: int):
    """Document source minimal, distinct à chaque appel pour ne pas être vu comme un doublon."""
    from okxq.domain.events import SourceDocument

    moment = T0 + timedelta(seconds=index)
    return SourceDocument(
        document_id=f"doc-{index:03d}",
        source="okx-annonces",
        source_url=f"https://exemple.invalid/a/{index}",
        version=1,
        published_at=moment,
        date_method="entete_http",
        received_at=moment,
        first_seen_at=moment,
        raw_text_hash=f"hash-{index:03d}",
        title=f"Annonce {index}",
        text="corps",
        language="fr",
        deduplication_id=f"dedup-{index:03d}",
        asset_mapping=[],
    )


def _worker(max_queue_length: int):
    """Worker JEV réel, avec un transport HTTP factice : aucune requête ne sort."""
    import httpx

    from okxq.config.schema import JevCfg
    from okxq.jev.cache import DocumentStore, JevResultCache
    from okxq.jev.client import JevClient
    from okxq.jev.schemas import load_question_set
    from okxq.jev.worker import JevWorker
    from okxq.persistence.db import make_engine, make_session_factory
    from okxq.persistence.models import Base

    clock = SimulatedClock(T0)
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    questions = load_question_set("configs/jev_questions.v1.json")
    cfg = JevCfg(model=questions.model, max_queue_length=max_queue_length)

    def refuser(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - jamais atteint
        raise AssertionError("aucun appel réseau ne doit partir de ce test")

    client = JevClient(
        endpoint=cfg.endpoint,
        api_key="cle-de-test-jamais-utilisee",
        model=cfg.model,
        timeout_total_ms=cfg.timeout_total_ms,
        max_retry_attempts=0,
        clock=clock,
        transport=httpx.MockTransport(refuser),
    )
    return JevWorker(
        cfg,
        client=client,
        question_set=questions,
        documents=DocumentStore(factory),
        cache=JevResultCache(factory),
        clock=clock,
    )


def test_T68_the_jev_queue_refuses_explicitly_once_full() -> None:
    """Le worker rend un RÉSULTAT explicite quand sa file est pleine.

    Un `None` ou un succès muet laisserait l'appelant croire que le document a été pris en compte,
    et l'absence d'évaluation passerait pour « rien à dire » au lieu de « pas regardé ».
    """
    from okxq.jev.worker import SubmitOutcome

    taille = 3
    worker = _worker(taille)
    acceptes = [worker.submit(_document(i)) for i in range(taille)]
    assert acceptes == [SubmitOutcome.QUEUED] * taille, "contre-épreuve : la file accepte jusqu'à sa borne"
    assert worker.queue_length == taille

    refuse = worker.submit(_document(taille))
    assert refuse is SubmitOutcome.QUEUE_FULL
    assert worker.queue_length == taille, "un refus ne doit pas faire grandir la file"


def test_T68_a_duplicate_document_is_distinguished_from_a_full_queue() -> None:
    """Confondre « déjà vu » et « file pleine » ferait diagnostiquer une saturation là où il n'y a
    qu'une ré-ingestion, et inversement."""
    from okxq.jev.worker import SubmitOutcome

    worker = _worker(10)
    assert worker.submit(_document(1)) is SubmitOutcome.QUEUED
    assert worker.submit(_document(1)) is SubmitOutcome.DUPLICATE
    assert worker.queue_length == 1


def test_T68_a_disabled_component_says_so_rather_than_silently_dropping() -> None:
    """Désactivé n'est pas saturé, et ce n'est pas non plus un succès."""
    from okxq.config.schema import JevCfg
    from okxq.jev.worker import SubmitOutcome

    worker = _worker(10)
    worker._cfg = JevCfg(model=worker._cfg.model, enabled=False)
    assert worker.submit(_document(2)) is SubmitOutcome.DISABLED
    assert worker.queue_length == 0
