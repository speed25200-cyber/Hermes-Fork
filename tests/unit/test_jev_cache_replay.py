"""T57 : un document rejoué depuis le cache conserve son âge et sa provenance D'ORIGINE.

Le piège est subtil et coûteux. Une évaluation sémantique servie depuis le cache est instantanée,
donc si on la datait de MAINTENANT, un document vieux de plusieurs heures paraîtrait tout frais. La
décision le traiterait alors comme une nouvelle publiée à l'instant — et agirait sur une nouvelle que
le marché a déjà digérée depuis longtemps.

L'horodatage qui compte n'est jamais celui de la lecture : c'est celui de la publication et de la
réception initiales.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from okxq.domain.events import JevEvaluation, JevStatus
from okxq.domain.ids import sha256_hex
from okxq.jev.cache import CacheKey, DocumentStore, JevResultCache, asset_scoped_mapping_version
from okxq.jev.source_connectors import DocumentCandidate
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import Base, DocumentVersion, SourceDocumentRow

PUBLIE = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
RECU = datetime(2026, 9, 18, 6, 0, 30, tzinfo=UTC)
EVALUE = datetime(2026, 9, 18, 6, 0, 31, tzinfo=UTC)
#: Plusieurs heures plus tard : c'est le moment où l'on RELIT le cache.
RELU = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

DOC = "doc-annonce-1"


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def version_id(factory) -> int:
    with factory() as session:
        session.add(
            SourceDocumentRow(
                document_id=DOC,
                source="okx-annonces",
                source_url="https://exemple.invalid/annonce-1",
                deduplication_id="dedup-1",
                first_seen_at=RECU,
                language="fr",
            )
        )
        session.flush()
        version = DocumentVersion(
            document_id=DOC,
            version=1,
            published_at=PUBLIE,
            date_method="entete_http",
            received_at=RECU,
            raw_text_hash="hash-du-texte",
            title="Annonce de listing",
            text="corps de l'annonce",
            asset_mapping=[{"inst_id": "BTC-USDT-SWAP", "symbol": "BTC"}],
            asset_mapping_version="map-v1",
        )
        session.add(version)
        session.commit()
        return int(version.id)


def _key(version_id: int) -> CacheKey:
    return CacheKey(
        model_version="jev-1.13.0",
        question_hash="hash-des-questions",
        document_version_id=version_id,
        asset_mapping_version="map-v1",
    )


def _evaluation(status: JevStatus = JevStatus.OK, **overrides) -> JevEvaluation:
    base = {
        "evaluation_id": "ev-1",
        "document_id": DOC,
        "document_version": 1,
        "question_set_hash": "hash-des-questions",
        "asset_mapping_version": "map-v1",
        "model_requested": "jev-1.13.0",
        "model_effective": "jev-1.13.0",
        "requested_at": EVALUE,
        "completed_at": EVALUE + timedelta(milliseconds=700),
        "inference_completed_at": EVALUE + timedelta(milliseconds=700),
        "features_committed_at": EVALUE + timedelta(seconds=1),
        "answers": {},
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "status": status,
        "inst_id": "BTC-USDT-SWAP",
    }
    base.update(overrides)
    return JevEvaluation(**base)  # type: ignore[arg-type]


def test_T57_a_cached_evaluation_keeps_its_original_timestamps(factory, version_id) -> None:
    """Le cœur de T57 : relire ne réhorodate pas.

    Si la relecture datait l'évaluation de maintenant, un document vieux de six heures paraîtrait
    frais, et la décision agirait sur une nouvelle déjà digérée par le marché.
    """
    cache = JevResultCache(factory)
    cache.put(_key(version_id), _evaluation())

    relu = cache.get(_key(version_id))
    assert relu is not None, "l'évaluation valide doit être un hit de cache"
    assert relu.requested_at == EVALUE, "l'horodatage de la demande d'origine est perdu"
    assert relu.completed_at == EVALUE + timedelta(milliseconds=700)
    assert relu.features_committed_at == EVALUE + timedelta(seconds=1)
    # Aucun horodatage ne doit avoir glissé vers l'instant de relecture.
    for moment in (relu.requested_at, relu.completed_at, relu.features_committed_at):
        assert moment is None or moment < RELU


def test_T57_a_cached_evaluation_keeps_its_provenance(factory, version_id) -> None:
    """La provenance identifie le document ET sa version : servir la bonne réponse pour le mauvais
    document serait invisible et faux."""
    cache = JevResultCache(factory)
    cache.put(_key(version_id), _evaluation())
    relu = cache.get(_key(version_id))
    assert relu is not None
    assert relu.document_id == DOC
    assert relu.document_version == 1
    assert relu.asset_mapping_version == "map-v1"
    assert relu.model_requested == "jev-1.13.0"
    assert relu.inst_id == "BTC-USDT-SWAP"


def test_T57_the_document_age_is_measured_from_publication_not_from_the_cache_read(
    factory, version_id
) -> None:
    """L'âge d'un document se compte depuis sa PUBLICATION. Mesuré depuis la lecture, il vaudrait
    zéro à chaque relecture, ce qui ferait d'un vieux document une nouvelle perpétuelle."""
    with factory() as session:
        version = session.get(DocumentVersion, version_id)
        assert version is not None
        age_reel = int((RELU - version.published_at).total_seconds())
    assert age_reel == 6 * 3600, "l'âge doit refléter six heures, pas l'instant de lecture"


def test_T57_a_failed_evaluation_is_not_a_cache_hit(factory, version_id) -> None:
    """Seule une évaluation valide est un hit. Servir un échec depuis le cache ferait passer une
    panne pour une réponse."""
    cache = JevResultCache(factory)
    cache.put(_key(version_id), _evaluation(status=JevStatus.ERROR, error="délai dépassé"))
    assert cache.get(_key(version_id)) is None


def test_T57_a_later_failure_never_overwrites_a_valid_evaluation(factory, version_id) -> None:
    """Une panne postérieure ne doit pas effacer une réponse déjà obtenue et déjà utilisée."""
    cache = JevResultCache(factory)
    cache.put(_key(version_id), _evaluation())
    cache.put(_key(version_id), _evaluation(evaluation_id="ev-2", status=JevStatus.ERROR))
    relu = cache.get(_key(version_id))
    assert relu is not None, "l'évaluation valide a été écrasée par un échec ultérieur"
    assert relu.evaluation_id == "ev-1"


@pytest.mark.parametrize(
    "champ", ["model_version", "question_hash", "asset_mapping_version", "cleaning_pipeline_version"]
)
def test_T57_any_change_in_the_key_is_a_cache_miss(factory, version_id, champ: str) -> None:
    """Changer de modèle, de questions, de mapping ou de nettoyage change le SENS de la réponse.

    Réutiliser l'ancienne réponse attribuerait à la nouvelle configuration un résultat qu'elle n'a
    jamais produit.
    """
    cache = JevResultCache(factory)
    cache.put(_key(version_id), _evaluation())
    autre = _key(version_id)
    modifie = CacheKey(
        **{**autre.as_dict(), champ: f"{getattr(autre, champ)}-modifie"}  # type: ignore[arg-type]
    )
    assert cache.get(modifie) is None
    # Contre-épreuve : la clé inchangée reste un hit.
    assert cache.get(autre) is not None


def test_T57_the_mapping_version_is_scoped_per_instrument() -> None:
    """Un document multi-actifs produit une évaluation PAR actif : la clé doit les distinguer,
    sinon la réponse du premier instrument serait servie pour tous les autres."""
    btc = asset_scoped_mapping_version("map-v1", "BTC-USDT-SWAP")
    eth = asset_scoped_mapping_version("map-v1", "ETH-USDT-SWAP")
    assert btc != eth
    assert "map-v1" in btc


def _candidat(texte: str, *, recu: datetime) -> DocumentCandidate:
    return DocumentCandidate(
        source="okx-annonces",
        source_url="https://exemple.invalid/annonce-2",
        deduplication_id="dedup-2",
        title="Annonce",
        text=texte,
        raw_text_hash=sha256_hex(texte.encode("utf-8")),
        published_at=PUBLIE,
        date_method="entete_http",
        language="fr",
        received_at=recu,
        parsed_at=recu,
        asset_mapping_version="map-v1",
    )


def test_T57_reingesting_an_identical_document_keeps_its_first_seen_date(factory) -> None:
    """Un même document revu plus tard ne se duplique pas et NE RAJEUNIT PAS.

    Sans cette garantie, un collecteur qui repasse sur la même page ferait repartir l'âge du document
    à zéro à chaque passage : une annonce ancienne redeviendrait une nouvelle fraîche à chaque cycle.
    """
    store = DocumentStore(factory)
    premier, cree = store.upsert(_candidat("corps initial", recu=RECU))
    assert cree is True
    assert premier.version == 1

    # Six heures plus tard, le même texte : ni nouvelle version, ni nouvelle date de première vue.
    relu, cree_encore = store.upsert(_candidat("corps initial", recu=RELU))
    assert cree_encore is False, "un texte identique ne doit pas créer de version"
    assert relu.version == 1
    assert relu.first_seen_at == RECU, "la date de première vue a été réécrite : le document rajeunit"

    with factory() as session:
        assert session.query(DocumentVersion).count() == 1

    # Contre-épreuve : un texte RÉELLEMENT modifié crée bien une nouvelle version, car son contenu
    # est nouveau — mais la date de première vue du document, elle, ne bouge pas.
    modifie, cree_v2 = store.upsert(_candidat("corps corrigé", recu=RELU))
    assert cree_v2 is True
    assert modifie.version == 2
    assert modifie.first_seen_at == RECU
