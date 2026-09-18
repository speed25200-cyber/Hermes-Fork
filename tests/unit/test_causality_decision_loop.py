"""Causalité point-in-time dans la boucle décisionnelle (T17, T19).

L'invariant est ``données utilisées <= cutoff <= début de la décision``. Deux contrôles le portent
dans ``decision_loop``, et tous deux étaient défaillants :

- le contrôle sur les vecteurs de features regardait ``available_at`` (l'instant où le vecteur est
  devenu disponible) au lieu de ``cutoff_at`` (la borne des données utilisées). Comme le contrat
  impose déjà ``available_at >= cutoff_at``, exiger ``available_at <= cutoff_at`` forçait l'égalité
  stricte : tout fournisseur horodatant réellement sa production faisait échouer chaque décision ;
- le contrôle sur les prévisions avait la bonne condition mais un corps vide (``pass``) : une
  prévision datée avant la borne de ses propres données passait silencieusement.

Ces tests échouent si l'un des deux contrôles redevient inopérant.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from okxq.domain.clocks import SimulatedClock
from okxq.domain.events import (
    CostBasis,
    FeatureVector,
    Forecast,
    MarketSnapshot,
    QualityFlag,
)
from okxq.runtime.decision_loop import DecisionDeps, DecisionLoop

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
CAUSALITY = "CAUSALITY_VIOLATION"
INST = "BTC-USDT-SWAP"


def vector(*, cutoff_at: datetime, available_at: datetime) -> FeatureVector:
    return FeatureVector(
        instrument=INST,
        cutoff_at=cutoff_at,
        available_at=available_at,
        names=["mid"],
        values=[100.0],
        masks=[QualityFlag.OK],
        schema_hash="h_features",
    )


def snapshot(*, cutoff_at: datetime, features: dict[str, FeatureVector]) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id="snp_test",
        cutoff_at=cutoff_at,
        universe_version="uni_test",
        metadata_version="meta_test",
        equity_version="eq_test",
        eligible_instruments=[INST],
        features=features,
    )


def forecast(*, available_at: datetime, snapshot_id: str = "snp_test") -> Forecast:
    return Forecast(
        forecast_id="fc_test",
        snapshot_id=snapshot_id,
        instrument=INST,
        model_id="m_test",
        horizon_s=60,
        cost_basis=CostBasis.MID,
        execution_policy_id="exec-policy-test",
        gross_mu=0.001,
        uncertainty=0.0005,
        available_at=available_at,
    )


class _Provider:
    def __init__(self, snap: MarketSnapshot) -> None:
        self._snap = snap

    async def snapshot(self, cutoff_at: datetime) -> MarketSnapshot:
        return self._snap


class _Predictor:
    def __init__(self, forecasts: list[Forecast]) -> None:
        self._forecasts = forecasts

    async def predict(self, snapshot: MarketSnapshot) -> list[Forecast]:
        return list(self._forecasts)


class _Gate:
    def blocking_reasons(self, cutoff_at: datetime) -> list:
        return []


class _Sink:
    def __init__(self) -> None:
        self.records: list = []

    def persist(self, record) -> None:
        self.records.append(record)


class _Unused:
    """Étages jamais atteints par ces tests : les atteindre signalerait que le contrôle a laissé passer."""

    def build(self, *args, **kwargs):
        raise AssertionError("étage atteint alors que la causalité aurait dû arrêter la décision")

    def optimize(self, *args, **kwargs):
        raise AssertionError("optimiseur atteint alors que la causalité aurait dû arrêter la décision")

    async def evaluate(self, *args, **kwargs):
        raise AssertionError("risque atteint alors que la causalité aurait dû arrêter la décision")

    async def submit(self, *args, **kwargs):
        raise AssertionError("gateway atteint alors que la causalité aurait dû arrêter la décision")

    async def reconcile(self, *args, **kwargs):
        raise AssertionError("gateway atteint alors que la causalité aurait dû arrêter la décision")


def _loop(snap: MarketSnapshot, forecasts: list[Forecast]) -> tuple[DecisionLoop, _Sink]:
    sink = _Sink()
    unused = _Unused()
    deps = DecisionDeps(
        clock=SimulatedClock(T0),
        mode="PAPER",
        minimum_eligible=1,
        gate=_Gate(),
        feature_provider=_Provider(snap),
        predictor=_Predictor(forecasts),
        edge_builder=unused,
        inputs_builder=unused,
        portfolio_builder=unused,
        intent_builder=unused,
        risk_service=unused,
        gateway=unused,
        sink=sink,
    )
    return DecisionLoop(deps), sink


def test_a_vector_computed_after_the_cutoff_is_accepted() -> None:
    """Un vecteur mis à disposition APRÈS son cutoff est le cas normal : le calcul prend du temps.

    Le rejeter revenait à n'accepter que des vecteurs calculés instantanément, ce qu'aucun
    fournisseur réel ne produit.
    """
    snap = snapshot(
        cutoff_at=T0,
        features={INST: vector(cutoff_at=T0, available_at=T0 + timedelta(milliseconds=250))},
    )
    loop, _sink = _loop(snap, [forecast(available_at=T0 + timedelta(milliseconds=500))])
    # Les étages suivants lèvent volontairement : on vérifie ici qu'on les ATTEINT, donc que la
    # causalité n'a pas arrêté une décision légitime.
    with pytest.raises(AssertionError):
        asyncio.run(loop.run_once(T0, T0 + timedelta(seconds=3)))


def _refused(snap: MarketSnapshot, forecasts: list[Forecast]) -> tuple[str, str]:
    """Exécute la décision et rend ``(issue, texte du refus)``.

    Une violation de causalité n'interrompt PAS le processus : elle produit une décision FAILED,
    journalisée avec son code et son motif. C'est le bon comportement — une boucle qui s'arrête sur
    un horodatage douteux ne peut plus ni réduire une position ni rapporter son état — et c'est ce
    contrat que ces tests vérifient, pas la remontée d'une exception.
    """
    loop, sink = _loop(snap, forecasts)
    record = asyncio.run(loop.run_once(T0, T0 + timedelta(seconds=3)))
    assert sink.records == [record], "toute décision, refus compris, doit être journalisée"
    assert CAUSALITY in record.reason_codes
    detail = " ".join(str(a.get("error", "")) for a in record.rejected_alternatives)
    return record.outcome, detail


def test_a_vector_computed_on_another_cutoff_is_refused() -> None:
    """C'est `cutoff_at` qui porte la causalité : un vecteur d'un autre cutoff décrit un autre marché."""
    autre = T0 - timedelta(seconds=60)
    snap = snapshot(cutoff_at=T0, features={INST: vector(cutoff_at=autre, available_at=T0)})
    outcome, detail = _refused(snap, [forecast(available_at=T0)])
    assert outcome == "FAILED"
    assert "autre cutoff" in detail


def test_a_forecast_dated_before_its_own_data_is_refused() -> None:
    """Contrôle qui existait avec un corps vide : la condition était juste, la conséquence absente.

    Une prévision disponible avant la borne des données qu'elle utilise est soit mal horodatée, soit
    entraînée sur le futur. Les deux cas doivent refuser la décision, pas la traverser en silence.
    """
    snap = snapshot(cutoff_at=T0, features={INST: vector(cutoff_at=T0, available_at=T0)})
    outcome, detail = _refused(snap, [forecast(available_at=T0 - timedelta(seconds=1))])
    assert outcome == "FAILED"
    assert "avant le cutoff" in detail


def test_a_forecast_from_another_snapshot_is_refused() -> None:
    snap = snapshot(cutoff_at=T0, features={INST: vector(cutoff_at=T0, available_at=T0)})
    outcome, detail = _refused(snap, [forecast(available_at=T0, snapshot_id="snp_autre")])
    assert outcome == "FAILED"
    assert "autre snapshot" in detail


def test_the_contract_itself_refuses_a_vector_available_before_its_cutoff() -> None:
    """Le contrat porte déjà la moitié de l'invariant : un vecteur ne peut pas précéder ses données."""
    with pytest.raises(ValueError, match="available_at"):
        vector(cutoff_at=T0, available_at=T0 - timedelta(milliseconds=1))
