"""Toute la chaîne JEV existait, sauf le fil qui la relie au calcul.

Client typé, worker borné, budget quotidien, coupe-circuit, garde anti-SSRF, cache sémantique
persistant, lecture point-in-time (`JevFeatureStore.evaluations_for`), features JEV : tout est
construit et testé. Mais `LiveFeatureProvider.snapshot` appelait `compute()` SANS jamais lui passer
`jev_evaluations`. En marche réelle, les features JEV étaient donc toutes nulles quoi qu'ait produit
le worker — et personne ne l'aurait vu, puisque « feature absente » est un état normal et masqué.

C'est le même mode d'échec que les défauts précédents de cette session : chaque pièce fonctionne,
c'est le câblage qui manque, et tous les tests des pièces passent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from okxq.domain.events import FeatureVector, JevAnswer, JevAnswerType, JevEvaluation, JevStatus
from okxq.runtime.composition import LiveFeatureProvider, UniverseView

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"


class _MoteurEspion:
    """Un moteur de features qui n'enregistre qu'une chose : ce qu'on lui a passé."""

    def __init__(self) -> None:
        self.recu: list[Any] = []

    def compute(self, inst: str, cutoff: datetime, **kw: Any) -> Any:
        self.recu.append(kw.get("jev_evaluations"))

        class _Calcul:
            vector = FeatureVector(
                instrument=inst,
                cutoff_at=cutoff,
                available_at=cutoff,
                names=[],
                values=[],
                masks=[],
                schema_hash="h" * 8,
            )

        return _Calcul()


def evaluation(*, engagee_a: datetime | None) -> JevEvaluation:
    return JevEvaluation(
        evaluation_id="ev_1",
        document_id="doc_1",
        document_version=1,
        question_set_hash="qs",
        asset_mapping_version="am_1",
        model_requested="jev-1.13.0",
        model_effective="jev-1.13.0",
        # L'ordre des instants est un invariant du contrat : demande, puis inférence, puis
        # engagement dans les features. L'engagement est ce qui compte pour la causalité.
        requested_at=T0 - timedelta(minutes=10),
        completed_at=T0 - timedelta(minutes=8),
        inference_completed_at=T0 - timedelta(minutes=8),
        features_committed_at=engagee_a,
        answers={"asset_relevance": JevAnswer(type=JevAnswerType.NOUL, probability=0.9)},
        status=JevStatus.OK,
        inst_id=INST,
    )


def fournisseur(moteur: _MoteurEspion, jev_pour: Any) -> LiveFeatureProvider:
    return LiveFeatureProvider(
        engine=moteur,  # type: ignore[arg-type]
        universe=lambda: UniverseView(
            universe_version="u",
            metadata_version="m",
            eligible=(INST,),
            held=(),
        ),
        equity_version=lambda: "e",
        reference_prices=lambda: {INST: Decimal("100")},
        jev_pour=jev_pour,
    )


@pytest.mark.asyncio
async def test_les_evaluations_atteignent_le_calcul_des_features() -> None:
    """Le défaut exact : sans ce fil, le moteur recevait toujours une liste vide."""
    moteur = _MoteurEspion()
    ev = evaluation(engagee_a=T0 - timedelta(minutes=5))
    provider = fournisseur(moteur, lambda inst, cutoff: [ev])
    await provider.snapshot(T0)
    assert moteur.recu, "le moteur n'a pas été appelé"
    assert list(moteur.recu[0] or []) == [ev], (
        "les évaluations JEV n'atteignent pas le calcul : les features JEV resteront nulles"
    )


@pytest.mark.asyncio
async def test_sans_source_jev_le_calcul_a_lieu_quand_meme() -> None:
    """JEV absent est une dégradation déclarée, pas une panne : la décision continue sans lui."""
    moteur = _MoteurEspion()
    provider = fournisseur(moteur, None)
    instantane = await provider.snapshot(T0)
    assert instantane.cutoff_at == T0
    assert list(moteur.recu[0] or []) == []


@pytest.mark.asyncio
async def test_une_lecture_jev_en_echec_narrete_pas_la_decision() -> None:
    """Une base indisponible ne doit pas interrompre la boucle : sinon JEV devient un point de panne.

    La plateforme doit pouvoir observer, réduire et rapporter même quand une source d'information
    secondaire tombe. Laisser l'exception remonter ferait échouer la décision entière.
    """
    moteur = _MoteurEspion()

    def casse(inst: str, cutoff: datetime) -> list[JevEvaluation]:
        raise RuntimeError("base injoignable")

    provider = fournisseur(moteur, casse)
    instantane = await provider.snapshot(T0)
    assert instantane.cutoff_at == T0, "une lecture JEV en échec a interrompu la décision"
    assert list(moteur.recu[0] or []) == [], "le calcul doit avoir lieu sans évaluation"
