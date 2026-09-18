"""Cache sémantique, feature store point-in-time et worker (T50, T51, T57, budget, disjoncteur, file bornée)."""

import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from typer.testing import CliRunner

from okxq.cli import app as cli_app
from okxq.config.schema import JevCfg
from okxq.domain.clocks import SimulatedClock
from okxq.domain.events import JevEvaluation
from okxq.domain.events import JevStatus as EvaluationStatus
from okxq.domain.protocols import JevEventEvaluator
from okxq.jev.cache import CacheKey, DocumentStore, JevFeatureStore, JevResultCache
from okxq.jev.client import JevClient
from okxq.jev.quality import FixtureJevEvaluator, agreement_metrics, document_from_case, load_corpus
from okxq.jev.schemas import JevUsage, load_question_set
from okxq.jev.worker import CircuitBreaker, DailyBudget, JevWorker, SubmitOutcome
from okxq.persistence import models
from okxq.persistence.db import make_session_factory, memory_engine

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "jev"
QUESTIONS = load_question_set(ROOT / "configs" / "jev_questions.v1.json")
CORPUS = load_corpus(FIX / "corpus.jsonl", question_set=QUESTIONS)
CASES = {c.case_id: c for c in CORPUS}
RESPONSE = json.loads((FIX / "response_reference.json").read_text(encoding="utf-8"))
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
KEY = "sk-test-secret"
UNRELATED_TABLES = (
    models.OrderRow,
    models.OrderIntentRow,
    models.RiskApproval,
    models.RiskEventRow,
    models.RiskState,
    models.PositionSnapshot,
    models.AccountSnapshot,
    models.LedgerTransaction,
    models.OutboxEvent,
    models.OperatorAction,
)


class Provider:
    def __init__(self, clock: SimulatedClock, *, status=200, delay_s=0.0, body=None):
        self.clock, self.status, self.delay_s, self.body = clock, status, delay_s, body
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.delay_s:
            self.clock.advance_seconds(self.delay_s)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "x"})
        return httpx.Response(200, json=self.body or RESPONSE)


class Harness:
    def __init__(self, clock: SimulatedClock, provider: Provider, *, api_key=KEY, **cfg_over):
        self.engine = memory_engine()
        self.factory = make_session_factory(self.engine)
        self.documents = DocumentStore(self.factory)
        self.cache = JevResultCache(self.factory)
        self.features = JevFeatureStore(self.factory)
        self.cfg = JevCfg(
            **{
                "timeout_total_ms": 1500,
                "max_retry_attempts": 0,
                "circuit_breaker_failures": 2,
                "circuit_breaker_cooldown_seconds": 60,
                "max_queue_length": 2,
                "max_daily_spend_usd": "1.00",
                **cfg_over,
            }
        )
        self.provider = provider

        async def no_sleep(seconds: float) -> None:
            clock.advance_seconds(seconds)

        self.client = JevClient(
            endpoint=self.cfg.endpoint,
            api_key=api_key,
            model=self.cfg.model,
            timeout_total_ms=self.cfg.timeout_total_ms,
            max_retry_attempts=self.cfg.max_retry_attempts,
            clock=clock,
            transport=httpx.MockTransport(provider),
            sleep=no_sleep,
        )
        self.worker = JevWorker(
            self.cfg,
            client=self.client,
            question_set=QUESTIONS,
            documents=self.documents,
            cache=self.cache,
            clock=clock,
        )

    def unrelated_rows(self) -> int:
        total = 0
        with self.factory() as session:
            for table in UNRELATED_TABLES:
                total += session.scalar(select(func.count()).select_from(table)) or 0
        return total


def doc(case_id: str, received_at: datetime, **over):
    return document_from_case(CASES[case_id], received_at=received_at, **over)


# --- T50 : panne fournisseur --------------------------------------------------------------------------------


async def test_T50_provider_outage_gives_explicit_reason_and_touches_nothing_else(clock):
    h = Harness(clock, Provider(clock, status=503))
    assert isinstance(h.worker, JevEventEvaluator)
    params = inspect.signature(JevWorker.__init__).parameters
    assert not any(
        word in " ".join(params) for word in ("risk", "gateway", "exchange", "position", "account")
    )
    first = await h.worker.evaluate(doc("c01_maintenance_en", T0))
    assert (
        first.status is EvaluationStatus.ERROR
        and first.error == "JEV_UPSTREAM_UNAVAILABLE"
        and first.answers == {}
    )
    second = await h.worker.evaluate(doc("c04_security_incident_en", T0))
    assert second.status is EvaluationStatus.ERROR and h.provider.calls == 2
    status = h.worker.status()
    assert (
        status.available is False and "CIRCUIT_OPEN" in status.reasons and status.real_call_status == "FAILED"
    )
    assert status.last_error == "JEV_UPSTREAM_UNAVAILABLE" and status.counters["call_failures"] == 2
    third = await h.worker.evaluate(doc("c19_partnership_en", T0))
    assert third.status is EvaluationStatus.ERROR and third.error == "CIRCUIT_OPEN" and h.provider.calls == 2
    assert h.unrelated_rows() == 0
    assert h.features.evaluations_for("EXM-USDT-SWAP", clock.now_utc()) == []


async def test_T50_missing_key_and_disabled_config_are_explicit_without_calls(clock):
    h = Harness(clock, Provider(clock), api_key=None)
    ev = await h.worker.evaluate(doc("c01_maintenance_en", T0))
    assert ev.status is EvaluationStatus.ERROR and ev.error == "JEV_API_KEY_MISSING" and h.provider.calls == 0
    assert (
        "JEV_API_KEY_MISSING" in h.worker.status().reasons and h.worker.status().real_call_status == "NOT_RUN"
    )
    off = Harness(clock, Provider(clock), enabled=False, influence_mode="off")
    assert off.worker.submit(doc("c01_maintenance_en", T0)) is SubmitOutcome.DISABLED
    assert (
        await off.worker.evaluate(doc("c01_maintenance_en", T0))
    ).error == "JEV_DISABLED" and off.provider.calls == 0


# --- T51 : réponse tardive ----------------------------------------------------------------------------------


async def test_T51_late_response_is_archived_late_excluded_from_past_snapshots_never_backdated(clock):
    h = Harness(clock, Provider(clock, delay_s=3.0))
    late = await h.worker.evaluate(doc("c01_maintenance_en", T0))
    assert late.status is EvaluationStatus.LATE and late.requested_at == T0
    assert late.inference_completed_at == T0 + timedelta(
        seconds=3
    ) and late.features_committed_at == T0 + timedelta(seconds=3)
    assert late.answers["event_kind"].choice == "maintenance"  # archivée, mais jamais promue
    assert h.features.evaluations_for("EXM-USDT-SWAP", T0 + timedelta(seconds=2)) == []
    assert h.features.evaluations_for("EXM-USDT-SWAP", T0 + timedelta(hours=1)) == []
    assert h.cache.count(status="late") == 1 and h.worker.status().counters["late"] == 1
    h.provider.delay_s = 0.5
    t1 = clock.now_utc()
    ok = await h.worker.evaluate(doc("c19_partnership_en", t1))
    assert ok.status is EvaluationStatus.OK and ok.features_committed_at == t1 + timedelta(seconds=0.5)
    assert h.features.evaluations_for("EXM-USDT-SWAP", t1 + timedelta(seconds=0.4)) == []
    (record,) = h.features.evaluations_for("EXM-USDT-SWAP", t1 + timedelta(seconds=0.5))
    assert record.evaluation.evaluation_id == ok.evaluation_id and record.document_id == ok.document_id


def test_T51_contract_refuses_backdated_evaluations():
    base = dict(
        evaluation_id="e",
        document_id="d",
        document_version=1,
        question_set_hash="q",
        asset_mapping_version="m",
        model_requested="jev-1.13.0",
        model_effective="jev-1.13.0",
        requested_at=T0,
        completed_at=T0,
        status=EvaluationStatus.OK,
    )
    JevEvaluation(**base, inference_completed_at=T0, features_committed_at=T0)
    with pytest.raises(ValidationError):
        JevEvaluation(**base, inference_completed_at=T0, features_committed_at=T0 - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        JevEvaluation(**{**base, "completed_at": T0 - timedelta(seconds=1)})
    with pytest.raises(ValidationError):
        JevEvaluation(**base)  # ok sans features_committed_at


# --- T57 : le cache ne rajeunit pas l'événement ---------------------------------------------------------


async def test_T57_cache_hit_returns_original_evaluation_and_event_age_keeps_growing(clock):
    h = Harness(clock, Provider(clock))
    case = CASES["c01_maintenance_en"]
    assert case.document.published_at == T0 - timedelta(hours=4)
    document = doc("c01_maintenance_en", T0)
    first = await h.worker.evaluate(document)
    assert first.status is EvaluationStatus.OK and h.provider.calls == 1
    clock.advance(timedelta(hours=2))
    again = await h.worker.evaluate(document)
    assert (
        again.evaluation_id == first.evaluation_id
        and again.features_committed_at == first.features_committed_at
    )
    assert (
        h.provider.calls == 1
        and h.worker.status().counters["cache_hits"] == 1
        and h.worker.status().cache_hit_ratio == 0.5
    )
    (record,) = h.features.evaluations_for("EXM-USDT-SWAP", clock.now_utc())
    assert record.age_basis == "published_at" and record.event_age(clock.now_utc()) == timedelta(hours=6)
    assert record.event_age(T0) == timedelta(hours=4)
    assert h.worker.status().age_seconds == pytest.approx(7200.0)
    assert h.features.evaluations_for("EXM-USDT-SWAP", clock.now_utc(), max_age=timedelta(hours=5)) == []
    modified = doc("c01_maintenance_en", clock.now_utc(), version=2)
    modified = modified.model_copy(update={"text": modified.text + " Updated.", "raw_text_hash": "other"})
    assert (await h.worker.evaluate(modified)).evaluation_id != first.evaluation_id and h.provider.calls == 2


def test_cache_key_and_result_cache_semantics(clock):
    h = Harness(clock, Provider(clock))
    document = doc("c01_maintenance_en", T0)
    version_id = h.documents.ensure(document, asset_mapping_version="v")
    key = CacheKey(
        model_version="jev-1.13.0",
        question_hash=QUESTIONS.question_set_hash,
        document_version_id=version_id,
        asset_mapping_version="v#EXM-USDT-SWAP",
    )
    assert h.cache.get(key) is None
    base = dict(
        evaluation_id="e1",
        document_id=document.document_id,
        document_version=1,
        question_set_hash=QUESTIONS.question_set_hash,
        asset_mapping_version=key.asset_mapping_version,
        model_requested="jev-1.13.0",
        requested_at=T0,
        completed_at=T0,
        inst_id="EXM-USDT-SWAP",
    )
    h.cache.put(
        key, JevEvaluation(**base, model_effective=None, status=EvaluationStatus.ERROR, error="JEV_TIMEOUT")
    )
    assert h.cache.get(key) is None  # un échec n'est jamais un hit
    ok = JevEvaluation(
        **{**base, "evaluation_id": "e2"},
        model_effective="jev-1.13.0",
        inference_completed_at=T0,
        features_committed_at=T0,
        status=EvaluationStatus.OK,
    )
    h.cache.put(key, ok)
    assert h.cache.get(key).evaluation_id == "e2"
    h.cache.put(
        key,
        JevEvaluation(
            **{**base, "evaluation_id": "e3"},
            model_effective=None,
            status=EvaluationStatus.ERROR,
            error="later failure",
        ),
    )
    assert h.cache.get(key).evaluation_id == "e2"  # un succès n'est pas écrasé par un échec ultérieur
    other_pipeline = CacheKey(**{**key.as_dict(), "cleaning_pipeline_version": "clean-v2"})
    assert h.cache.get(other_pipeline) is None and other_pipeline.hash() != key.hash()
    assert (
        h.documents.version_row_id(document.document_id, 1) == version_id
        and h.documents.get(document.document_id).text == document.text
    )


# --- budget, disjoncteur, file ------------------------------------------------------------------------------


async def test_budget_counts_retries_blocks_and_resets_on_utc_day(clock):
    h = Harness(
        clock,
        Provider(clock, status=503),
        max_retry_attempts=1,
        max_daily_spend_usd="0.02",
        circuit_breaker_failures=50,
    )
    first = await h.worker.evaluate(doc("c01_maintenance_en", T0))
    assert first.error == "JEV_UPSTREAM_UNAVAILABLE" and h.provider.calls == 2
    status = h.worker.status()
    assert (
        status.spent_today_usd == "0.02"
        and "BUDGET_EXHAUSTED" in status.reasons
        and status.counters["attempts"] == 2
    )
    blocked = await h.worker.evaluate(doc("c19_partnership_en", T0))
    assert (
        blocked.error == "BUDGET_EXHAUSTED"
        and h.provider.calls == 2
        and h.worker.status().counters["budget_refused"] == 1
    )
    clock.set(datetime(2026, 9, 19, 0, 0, 1, tzinfo=UTC))
    h.provider.status = 200
    ok = await h.worker.evaluate(doc("c21_listing_en", clock.now_utc()))
    assert (
        ok.status is EvaluationStatus.OK
        and h.provider.calls == 3
        and h.worker.status().spent_today_usd == "0.01"
    )


def test_daily_budget_uses_returned_cost_or_token_prices(clock):
    budget = DailyBudget(
        max_usd=Decimal("1"),
        clock=clock,
        usd_per_1k_input_tokens=Decimal("0.5"),
        usd_per_1k_output_tokens=Decimal("1"),
    )
    assert budget.estimate(JevUsage.model_validate({"costUsd": 0.25}), 3) == Decimal("0.25")
    assert budget.estimate(JevUsage.model_validate({"inputTokens": 1000, "outputTokens": 500}), 2) == Decimal(
        "1.01"
    )
    assert budget.estimate(None, 2) == Decimal("0.02")
    budget.charge(None, 1)
    assert budget.spent_today == Decimal("0.01") and budget.can_spend()
    with pytest.raises(Exception, match="négatif"):
        DailyBudget(max_usd=Decimal("-1"), clock=clock)


async def test_circuit_breaker_opens_cools_down_and_half_open_probe_closes_it(clock):
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=60, clock=clock)
    assert breaker.allow() and breaker.state == "closed"
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()
    clock.advance_seconds(61)
    assert breaker.state == "half_open" and breaker.allow() and not breaker.allow()  # une seule sonde
    breaker.record_success()
    assert breaker.state == "closed" and breaker.consecutive_failures == 0
    h = Harness(clock, Provider(clock, status=503))
    for case_id in ("c01_maintenance_en", "c04_security_incident_en"):
        await h.worker.evaluate(doc(case_id, clock.now_utc()))
    assert h.worker.status().circuit_state == "open"
    clock.advance_seconds(61)
    h.provider.status = 200
    ok = await h.worker.evaluate(doc("c19_partnership_en", clock.now_utc()))
    assert (
        ok.status is EvaluationStatus.OK
        and h.worker.status().circuit_state == "closed"
        and h.worker.status().available
    )


async def test_queue_is_bounded_and_only_new_or_modified_documents_trigger_work(clock):
    h = Harness(clock, Provider(clock))
    d1, d2, d3 = (
        doc(c, T0) for c in ("c01_maintenance_en", "c04_security_incident_en", "c19_partnership_en")
    )
    assert h.worker.submit(d1) is SubmitOutcome.QUEUED
    assert h.worker.submit(d1) is SubmitOutcome.DUPLICATE
    assert h.worker.submit(d2) is SubmitOutcome.QUEUED
    assert h.worker.submit(d3) is SubmitOutcome.QUEUE_FULL and h.worker.queue_length == 2
    evaluations = await h.worker.process_pending()
    assert len(evaluations) == 2 and h.provider.calls == 2 and h.worker.queue_length == 0
    assert await h.worker.process_pending() == []
    assert h.worker.submit(d1) is SubmitOutcome.DUPLICATE
    assert (
        h.worker.submit(d1.model_copy(update={"version": 2, "raw_text_hash": "changed"}))
        is SubmitOutcome.QUEUED
    )
    assert h.worker.submit(d3) is SubmitOutcome.QUEUED
    assert len(await h.worker.process_pending(max_items=1)) == 1 and h.worker.queue_length == 1
    counters = h.worker.status().counters
    assert counters["queue_dropped"] == 1 and counters["duplicates"] == 2 and counters["queued"] == 4


async def test_no_mapping_is_rejected_without_a_call_and_multi_asset_gets_one_evaluation_per_asset(clock):
    h = Harness(clock, Provider(clock))
    ambiguous = doc("c12_ticker_collision_ambiguous", T0)
    assert ambiguous.asset_mapping == [] and ambiguous.mapping_quality == "ambiguous"
    rejected = await h.worker.evaluate(ambiguous)
    assert (
        rejected.status is EvaluationStatus.REJECTED
        and rejected.error == "NO_ASSET_MAPPING:ambiguous"
        and h.provider.calls == 0
    )
    multi = doc("c15_multi_asset_maintenance", T0)
    eth = multi.asset_mapping[0].model_copy(
        update={"inst_id": "ETH-USDT-SWAP", "canonical_name": "Ethereum", "symbol": "ETH"}
    )
    multi = multi.model_copy(update={"asset_mapping": [*multi.asset_mapping, eth]})
    evaluations = await h.worker.evaluate_all(multi)
    assert (
        sorted(e.inst_id for e in evaluations) == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"] and h.provider.calls == 2
    )
    assert len(h.features.evaluations_for("ETH-USDT-SWAP", clock.now_utc())) == 1
    assert len(h.features.evaluations_for("BTC-USDT-SWAP", clock.now_utc())) == 1


async def test_rejected_provider_codes_do_not_trip_the_breaker_but_contract_errors_do(clock):
    h = Harness(clock, Provider(clock, status=422))
    ev = await h.worker.evaluate(doc("c01_maintenance_en", T0))
    assert ev.status is EvaluationStatus.REJECTED and ev.error == "JEV_SCHEMA_REJECTED"
    await h.worker.evaluate(doc("c04_security_incident_en", T0))
    assert h.worker.status().circuit_state == "closed" and h.worker.status().counters["rejected"] == 2
    bad = json.loads(json.dumps(RESPONSE))
    bad["answers"]["asset_relevance"]["probability"] = 1.7
    h2 = Harness(clock, Provider(clock, body=bad))
    for case_id in ("c01_maintenance_en", "c04_security_incident_en"):
        ev = await h2.worker.evaluate(doc(case_id, T0))
        assert ev.status is EvaluationStatus.ERROR and ev.error == "JEV_CONTRACT_INVALID"
    assert h2.worker.status().circuit_state == "open"


# --- évaluateur factice et métriques d'accord --------------------------------------------------------------


async def test_fixture_evaluator_is_labelled_fake_and_agrees_with_corpus(clock):
    evaluator = FixtureJevEvaluator(CORPUS, question_set=QUESTIONS, clock=clock)
    assert evaluator.is_fixture and isinstance(evaluator, JevEventEvaluator)
    predictions = {}
    for case in CORPUS:
        ev = await evaluator.evaluate(document_from_case(case, received_at=T0))
        assert ev.status is EvaluationStatus.OK and ev.model_effective == "fixture:corpus"
        assert ev.usage["fixture"] is True and ev.usage["real_call"] == "NOT_RUN"
        predictions[case.case_id] = ev.answers
    metrics = agreement_metrics(predictions, CORPUS, QUESTIONS)
    assert set(metrics) == set(QUESTIONS.questions)
    assert all(m.n == len(CORPUS) and m.agreement == 1.0 for m in metrics.values())
    assert (
        metrics["reported_severity"].within_one_rate == 1.0 and metrics["event_kind"].within_one_rate is None
    )
    unknown = document_from_case(CORPUS[0], received_at=T0).model_copy(update={"text": "not in corpus"})
    assert (await evaluator.evaluate(unknown)).status is EvaluationStatus.REJECTED
    assert evaluator.calls == len(CORPUS) + 1


# --- état exposé et CLI -------------------------------------------------------------------------------------


async def test_status_snapshot_and_cli(clock, tmp_path):
    h = Harness(clock, Provider(clock))
    await h.worker.evaluate(doc("c01_maintenance_en", T0))
    snapshot = h.worker.status().to_json()
    assert (
        snapshot["available"]
        and snapshot["model_effective"] == "jev-1.13.0"
        and snapshot["tokens_input"] == 812
    )
    assert (
        snapshot["latency_p50_ms"] == 0.0
        and snapshot["counters"]["ok"] == 1
        and "sk-test" not in json.dumps(snapshot)
    )
    path = h.worker.write_status(tmp_path / "runtime" / "jev_status.json")
    runner = CliRunner()
    absent = runner.invoke(cli_app, ["jev", "status", "--status-file", str(tmp_path / "nope.json")])
    assert absent.exit_code == 0 and json.loads(absent.stdout)["real_call"] == "NOT_RUN"
    present = runner.invoke(
        cli_app, ["jev", "status", "--status-file", str(path), "--max-age-seconds", "999999999"]
    )
    assert present.exit_code == 0 and json.loads(present.stdout)["snapshot"]["counters"]["ok"] == 1
    validated = runner.invoke(
        cli_app,
        [
            "jev",
            "validate-fixtures",
            "--fixtures-dir",
            str(FIX),
            "--questions",
            str(ROOT / "configs" / "jev_questions.v1.json"),
        ],
    )
    assert validated.exit_code == 0 and json.loads(validated.stdout)["ok"] is True
    broken = runner.invoke(cli_app, ["jev", "validate-fixtures", "--fixtures-dir", str(tmp_path)])
    assert broken.exit_code == 1 and json.loads(broken.stdout)["ok"] is False
