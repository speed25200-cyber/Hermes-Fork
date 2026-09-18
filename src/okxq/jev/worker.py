"""Worker JEV (§50.3) : file bornée, concurrence limitée, deadline totale, disjoncteur, budget journalier.

Isolation (T50) : le worker ne connaît que le client JEV, le dépôt de documents et le cache. Il n'a aucune
référence vers le risque, l'exécution, les positions ou l'exchange ; en panne il rend une évaluation
``error``/``rejected`` avec une raison explicite et ne lève jamais vers l'appelant.

Déclenchement : uniquement par documents nouveaux ou modifiés (``submit`` ignore les versions déjà vues) ;
``process_pending`` traite un lot borné — aucune boucle illimitée ici, c'est le scheduler qui cadence.

Attention aux noms : ``okxq.domain.events.JevStatus`` est le statut d'UNE évaluation (ok/late/error/rejected) ;
``okxq.jev.worker.JevStatus`` est l'état du worker exposé à l'API et aux métriques.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

from okxq.config.schema import JevCfg
from okxq.domain.clocks import Clock, SystemClock, utc_day
from okxq.domain.errors import JevContractError, JevError
from okxq.domain.events import AssetMapping, JevEvaluation, SourceDocument
from okxq.domain.events import JevStatus as EvaluationStatus
from okxq.domain.ids import new_id
from okxq.domain.money import dec
from okxq.jev.cache import (
    CLEANING_PIPELINE_VERSION,
    CacheKey,
    DocumentStore,
    JevResultCache,
    asset_scoped_mapping_version,
)
from okxq.jev.client import JevCallResult, JevClient, percentile
from okxq.jev.entity_mapping import DEFAULT_MAX_PRIOR, DEFAULT_PRIOR_WINDOW, select_prior_documents
from okxq.jev.schemas import JevUsage, QuestionSet, build_request

log = structlog.get_logger("okxq.jev.worker")

REJECTED_CODES = frozenset({"JEV_SCHEMA_REJECTED", "JEV_BAD_REQUEST", "JEV_STATE_TOO_LARGE"})
NON_AVAILABILITY_CODES = REJECTED_CODES | {"JEV_CONFIG_INVALID"}
LATENCY_WINDOW = 512
DEFAULT_FALLBACK_COST_PER_ATTEMPT_USD = Decimal("0.01")
"""Coût conservateur imputé à une tentative sans usage retourné (échec, timeout). Hypothèse documentée."""


class SubmitOutcome(StrEnum):
    QUEUED = "queued"
    DUPLICATE = "duplicate"
    QUEUE_FULL = "queue_full"
    DISABLED = "disabled"


class CircuitBreaker:
    """Ouvert après ``threshold`` échecs consécutifs ; se referme après un succès en demi-ouverture."""

    def __init__(self, *, threshold: int, cooldown_seconds: int, clock: Clock) -> None:
        if threshold <= 0 or cooldown_seconds <= 0:
            raise JevError(
                "seuil et cooldown du disjoncteur doivent être positifs", code="JEV_CONFIG_INVALID"
            )
        self._threshold = threshold
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._clock = clock
        self.consecutive_failures = 0
        self.opened_at: datetime | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self._clock.now_utc() - self.opened_at >= self._cooldown:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self._probe_in_flight = False
        if self.consecutive_failures >= self._threshold:
            self.opened_at = self._clock.now_utc()


class DailyBudget:
    """Budget financier journalier (jour UTC) : usage retourné si présent, sinon coût conservateur par tentative."""

    def __init__(
        self,
        *,
        max_usd: Decimal,
        clock: Clock,
        fallback_cost_per_attempt_usd: Decimal = DEFAULT_FALLBACK_COST_PER_ATTEMPT_USD,
        usd_per_1k_input_tokens: Decimal | None = None,
        usd_per_1k_output_tokens: Decimal | None = None,
    ) -> None:
        if max_usd < 0 or fallback_cost_per_attempt_usd < 0:
            raise JevError("budget négatif", code="JEV_CONFIG_INVALID")
        self.max_usd = max_usd
        self._clock = clock
        self._fallback = fallback_cost_per_attempt_usd
        self._in_price = usd_per_1k_input_tokens
        self._out_price = usd_per_1k_output_tokens
        self._day = utc_day(clock.now_utc())
        self._spent = Decimal(0)
        self.charged_attempts = 0

    def _roll(self) -> None:
        today = utc_day(self._clock.now_utc())
        if today != self._day:
            self._day = today
            self._spent = Decimal(0)
            self.charged_attempts = 0

    @property
    def spent_today(self) -> Decimal:
        self._roll()
        return self._spent

    def can_spend(self) -> bool:
        self._roll()
        return self._spent < self.max_usd

    def estimate(self, usage: JevUsage | None, attempts: int) -> Decimal:
        attempts = max(1, attempts)
        if usage is not None:
            if usage.cost_usd is not None:
                return Decimal(repr(usage.cost_usd))
            if (
                self._in_price is not None
                and self._out_price is not None
                and (usage.input_tokens is not None or usage.output_tokens is not None)
            ):
                tokens_in = Decimal(usage.input_tokens or 0)
                tokens_out = Decimal(usage.output_tokens or 0)
                billed = (tokens_in * self._in_price + tokens_out * self._out_price) / Decimal(1000)
                # Les tentatives échouées avant la réussite sont comptées au coût conservateur.
                return billed + self._fallback * (attempts - 1)
        return self._fallback * attempts

    def charge(self, usage: JevUsage | None, attempts: int) -> Decimal:
        self._roll()
        cost = self.estimate(usage, attempts)
        self._spent += cost
        self.charged_attempts += max(1, attempts)
        return cost


@dataclass(slots=True)
class JevStatus:
    """État du worker pour l'API et les métriques. ``real_call_status`` reste ``NOT_RUN`` sans appel réel."""

    available: bool
    reasons: list[str]
    enabled: bool
    influence_mode: str
    model_requested: str
    model_effective: str | None
    last_ok_at: datetime | None
    age_seconds: float | None
    counters: dict[str, int]
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    cache_hit_ratio: float | None
    tokens_input: int
    tokens_output: int
    spent_today_usd: str
    max_daily_spend_usd: str
    circuit_state: str
    queue_length: int
    real_call_status: str
    last_error: str | None
    generated_at: datetime

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("last_ok_at", "generated_at"):
            value = data[key]
            data[key] = value.isoformat() if isinstance(value, datetime) else None
        return data


@dataclass(slots=True)
class _Counters:
    submitted: int = 0
    queued: int = 0
    duplicates: int = 0
    queue_dropped: int = 0
    evaluations: int = 0
    ok: int = 0
    late: int = 0
    error: int = 0
    rejected: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    calls: int = 0
    call_failures: int = 0
    budget_refused: int = 0
    circuit_refused: int = 0
    no_mapping: int = 0
    attempts: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(slots=True)
class _Evaluated:
    evaluation: JevEvaluation
    from_cache: bool = False
    call: JevCallResult | None = None
    reasons: list[str] = field(default_factory=list)


class JevWorker:
    """Implémente ``JevEventEvaluator``. Aucune dépendance vers le risque, l'exécution ou l'exchange."""

    def __init__(
        self,
        cfg: JevCfg,
        *,
        client: JevClient,
        question_set: QuestionSet,
        documents: DocumentStore,
        cache: JevResultCache,
        clock: Clock | None = None,
        budget: DailyBudget | None = None,
        breaker: CircuitBreaker | None = None,
        prior_window: timedelta = DEFAULT_PRIOR_WINDOW,
        max_prior: int = DEFAULT_MAX_PRIOR,
        cleaning_pipeline_version: str = CLEANING_PIPELINE_VERSION,
    ) -> None:
        if client.model != cfg.model or question_set.model != cfg.model:
            raise JevError(
                "modèle du client, du jeu de questions et de la configuration incohérents",
                code="JEV_CONFIG_INVALID",
            )
        self._cfg = cfg
        self._client = client
        self._questions = question_set
        self._documents = documents
        self._cache = cache
        self._clock: Clock = clock or SystemClock()
        self._budget = budget or DailyBudget(max_usd=dec(cfg.max_daily_spend_usd), clock=self._clock)
        self._breaker = breaker or CircuitBreaker(
            threshold=cfg.circuit_breaker_failures,
            cooldown_seconds=cfg.circuit_breaker_cooldown_seconds,
            clock=self._clock,
        )
        self._prior_window = prior_window
        self._max_prior = max_prior
        self._cleaning_version = cleaning_pipeline_version
        self._queue: deque[SourceDocument] = deque()
        self._seen: set[tuple[str, int]] = set()
        self._semaphore = asyncio.Semaphore(cfg.max_concurrency)
        self._latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._counters = _Counters()
        self._tokens_in = 0
        self._tokens_out = 0
        self._last_ok_at: datetime | None = None
        self._last_error: str | None = None
        self._model_effective: str | None = None
        self._real_call_status = "NOT_RUN"

    # --- file bornée ------------------------------------------------------------------------------------

    def submit(self, document: SourceDocument) -> SubmitOutcome:
        self._counters.submitted += 1
        if not self._cfg.enabled:
            return SubmitOutcome.DISABLED
        key = (document.document_id, document.version)
        if key in self._seen:
            self._counters.duplicates += 1
            return SubmitOutcome.DUPLICATE
        if len(self._queue) >= self._cfg.max_queue_length:
            self._counters.queue_dropped += 1
            log.warning(
                "jev_queue_full",
                document_id=document.document_id,
                max_queue_length=self._cfg.max_queue_length,
            )
            return SubmitOutcome.QUEUE_FULL
        self._seen.add(key)
        self._queue.append(document)
        self._counters.queued += 1
        return SubmitOutcome.QUEUED

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    async def process_pending(self, max_items: int | None = None) -> list[JevEvaluation]:
        """Traite un lot borné de documents en attente (concurrence ≤ ``max_concurrency``). Pas de boucle."""
        budget = len(self._queue) if max_items is None else max(0, min(max_items, len(self._queue)))
        batch = [self._queue.popleft() for _ in range(budget)]
        if not batch:
            return []
        results = await asyncio.gather(*(self._guarded(doc) for doc in batch))
        return [evaluation for group in results for evaluation in group]

    async def _guarded(self, document: SourceDocument) -> list[JevEvaluation]:
        async with self._semaphore:
            return await self.evaluate_all(document)

    # --- évaluation -----------------------------------------------------------------------------------

    async def evaluate(self, document: SourceDocument) -> JevEvaluation:
        """Contrat ``JevEventEvaluator`` : évalue l'actif principal (confiance la plus haute). Ne lève jamais."""
        evaluations = await self.evaluate_all(document)
        return evaluations[0]

    async def evaluate_all(self, document: SourceDocument) -> list[JevEvaluation]:
        mappings = sorted(document.asset_mapping, key=lambda m: (-m.confidence, m.inst_id))
        if not mappings:
            self._counters.no_mapping += 1
            self._counters.evaluations += 1
            self._counters.rejected += 1
            reason = f"NO_ASSET_MAPPING:{document.mapping_quality or 'none'}"
            return [self._local_evaluation(document, None, EvaluationStatus.REJECTED, reason)]
        out: list[JevEvaluation] = []
        for mapping in mappings:
            try:
                out.append(await self._evaluate_asset(document, mapping))
            except Exception as exc:  # panne interne : jamais propagée (T50)
                log.error("jev_internal_error", document_id=document.document_id, error=type(exc).__name__)
                self._last_error = f"JEV_INTERNAL_ERROR:{type(exc).__name__}"
                self._counters.evaluations += 1
                self._counters.error += 1
                out.append(
                    self._local_evaluation(document, mapping, EvaluationStatus.ERROR, self._last_error)
                )
        return out

    async def _evaluate_asset(self, document: SourceDocument, mapping: AssetMapping) -> JevEvaluation:
        requested_at = self._clock.now_utc()
        version_id = self._documents.ensure(document, asset_mapping_version=mapping.mapping_version)
        key = CacheKey(
            model_version=self._cfg.model,
            question_hash=self._questions.question_set_hash,
            document_version_id=version_id,
            asset_mapping_version=asset_scoped_mapping_version(mapping.mapping_version, mapping.inst_id),
            cleaning_pipeline_version=self._cleaning_version,
        )
        cached = self._cache.get(key)
        if cached is not None:
            self._counters.cache_hits += 1
            return cached  # horodatages d'origine : l'âge de l'événement n'est pas réinitialisé (T57)
        self._counters.cache_misses += 1
        self._counters.evaluations += 1
        blocking = self._blocking_reasons()
        if blocking:
            reason = blocking[0]
            if reason == "BUDGET_EXHAUSTED":
                self._counters.budget_refused += 1
            elif reason == "CIRCUIT_OPEN":
                self._counters.circuit_refused += 1
            self._counters.error += 1
            evaluation = self._local_evaluation(document, mapping, EvaluationStatus.ERROR, reason)
            self._cache.put(key, evaluation)
            return evaluation
        priors = select_prior_documents(
            document,
            self._documents.received_before(document.received_at, window=self._prior_window),
            inst_id=mapping.inst_id,
            window=self._prior_window,
            max_prior=self._max_prior,
        )
        build = build_request(
            model=self._cfg.model,
            question_set=self._questions,
            canonical_name=mapping.canonical_name,
            symbol=mapping.symbol,
            title=document.title,
            text=document.text or document.title,
            prior_documents=priors.as_request_pairs(),
            max_document_characters=self._cfg.max_document_characters,
        )
        deadline_at = requested_at + timedelta(milliseconds=self._cfg.timeout_total_ms)
        request_id = new_id("jreq")
        self._cache.record_request(
            request_id=request_id,
            key=key,
            requested_at=requested_at,
            deadline_at=deadline_at,
            payload_hash_value=build.request.payload_hash(),
        )
        self._counters.calls += 1
        try:
            call = await self._client.evaluate(build.request, self._questions.questions)
        except JevError as exc:
            return self._on_failure(document, mapping, key, request_id, requested_at, exc)
        return self._on_success(
            document, mapping, key, request_id, requested_at, deadline_at, call, build.truncated
        )

    def _on_success(
        self,
        document: SourceDocument,
        mapping: AssetMapping,
        key: CacheKey,
        request_id: str,
        requested_at: datetime,
        deadline_at: datetime,
        call: JevCallResult,
        truncated: bool,
    ) -> JevEvaluation:
        committed_at = self._clock.now_utc()
        late = committed_at > deadline_at
        status = EvaluationStatus.LATE if late else EvaluationStatus.OK
        self._real_call_status = "OK"
        self._breaker.record_success()
        self._budget.charge(call.parsed.usage, call.attempts)
        self._counters.attempts += call.attempts
        self._latencies.append(call.latency_ms)
        self._tokens_in += int(call.parsed.usage.input_tokens or 0)
        self._tokens_out += int(call.parsed.usage.output_tokens or 0)
        self._model_effective = call.model_effective
        usage = dict(call.usage)
        usage["attempts"] = call.attempts
        usage["latency_ms"] = round(call.latency_ms, 3)
        if truncated:
            usage["document_truncated"] = True
        evaluation = JevEvaluation(
            evaluation_id=new_id("jev"),
            document_id=document.document_id,
            document_version=document.version,
            question_set_hash=self._questions.question_set_hash,
            asset_mapping_version=key.asset_mapping_version,
            model_requested=self._cfg.model,
            model_effective=call.model_effective,
            requested_at=requested_at,
            completed_at=call.finished_at,
            inference_completed_at=call.finished_at,
            features_committed_at=committed_at,
            answers=call.parsed.answers,
            usage=usage,
            status=status,
            error=("LATE_RESPONSE: reçue après la deadline, exclue des features" if late else None),
            inst_id=mapping.inst_id,
        )
        if late:
            self._counters.late += 1
        else:
            self._counters.ok += 1
            self._last_ok_at = committed_at
        self._cache.put(key, evaluation)
        self._cache.finish_request(request_id, status=status.value, attempts=call.attempts)
        log.info(
            "jev_evaluation",
            document_id=document.document_id,
            inst_id=mapping.inst_id,
            status=status.value,
            model_effective=call.model_effective,
            usage=call.usage,
            late=late,
        )
        return evaluation

    def _on_failure(
        self,
        document: SourceDocument,
        mapping: AssetMapping,
        key: CacheKey,
        request_id: str,
        requested_at: datetime,
        exc: JevError,
    ) -> JevEvaluation:
        raw_attempts = exc.context.get("attempts")
        attempts = raw_attempts if isinstance(raw_attempts, int) and raw_attempts > 0 else 1
        self._counters.call_failures += 1
        self._counters.attempts += attempts
        self._budget.charge(None, attempts)
        self._real_call_status = "FAILED"
        self._last_error = exc.code
        rejected = exc.code in REJECTED_CODES
        if exc.code not in NON_AVAILABILITY_CODES or isinstance(exc, JevContractError):
            self._breaker.record_failure()
        status = EvaluationStatus.REJECTED if rejected else EvaluationStatus.ERROR
        if rejected:
            self._counters.rejected += 1
        else:
            self._counters.error += 1
        evaluation = self._local_evaluation(document, mapping, status, exc.code, requested_at=requested_at)
        self._cache.put(key, evaluation)
        self._cache.finish_request(request_id, status=status.value, attempts=attempts)
        return evaluation

    def _local_evaluation(
        self,
        document: SourceDocument,
        mapping: AssetMapping | None,
        status: EvaluationStatus,
        reason: str,
        *,
        requested_at: datetime | None = None,
    ) -> JevEvaluation:
        started = requested_at or self._clock.now_utc()
        now = self._clock.now_utc()
        return JevEvaluation(
            evaluation_id=new_id("jev"),
            document_id=document.document_id,
            document_version=document.version,
            question_set_hash=self._questions.question_set_hash,
            asset_mapping_version=(
                asset_scoped_mapping_version(mapping.mapping_version, mapping.inst_id) if mapping else "none"
            ),
            model_requested=self._cfg.model,
            model_effective=None,
            requested_at=started,
            completed_at=now,
            inference_completed_at=None,
            features_committed_at=None,
            answers={},
            usage={},
            status=status,
            error=reason,
            inst_id=mapping.inst_id if mapping else None,
        )

    def _blocking_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self._cfg.enabled:
            reasons.append("JEV_DISABLED")
        if not self._client.has_api_key:
            reasons.append("JEV_API_KEY_MISSING")
        if not self._breaker.allow():
            reasons.append("CIRCUIT_OPEN")
        if not self._budget.can_spend():
            reasons.append("BUDGET_EXHAUSTED")
        return reasons

    # --- état -----------------------------------------------------------------------------------------

    def status(self) -> JevStatus:
        now = self._clock.now_utc()
        reasons: list[str] = []
        if not self._cfg.enabled:
            reasons.append("JEV_DISABLED")
        if not self._client.has_api_key:
            reasons.append("JEV_API_KEY_MISSING")
        if self._breaker.state == "open":
            reasons.append("CIRCUIT_OPEN")
        if not self._budget.can_spend():
            reasons.append("BUDGET_EXHAUSTED")
        age: float | None = None
        if self._last_ok_at is None:
            reasons.append("NO_EVALUATION_YET")
        else:
            age = (now - self._last_ok_at).total_seconds()
            if age > self._cfg.default_event_max_age_seconds:
                reasons.append("LAST_EVALUATION_STALE")
        blocking = {"JEV_DISABLED", "JEV_API_KEY_MISSING", "CIRCUIT_OPEN", "BUDGET_EXHAUSTED"}
        hits, misses = self._counters.cache_hits, self._counters.cache_misses
        latencies = list(self._latencies)
        return JevStatus(
            available=not (set(reasons) & blocking),
            reasons=reasons,
            enabled=self._cfg.enabled,
            influence_mode=self._cfg.influence_mode,
            model_requested=self._cfg.model,
            model_effective=self._model_effective,
            last_ok_at=self._last_ok_at,
            age_seconds=age,
            counters=self._counters.as_dict(),
            latency_p50_ms=percentile(latencies, 0.50),
            latency_p95_ms=percentile(latencies, 0.95),
            cache_hit_ratio=(hits / (hits + misses)) if (hits + misses) else None,
            tokens_input=self._tokens_in,
            tokens_output=self._tokens_out,
            spent_today_usd=format(self._budget.spent_today, "f"),
            max_daily_spend_usd=format(self._budget.max_usd, "f"),
            circuit_state=self._breaker.state,
            queue_length=len(self._queue),
            real_call_status=self._real_call_status,
            last_error=self._last_error,
            generated_at=now,
        )

    def write_status(self, path: str | Path) -> Path:
        """Instantané JSON de l'état (lu par ``okxq jev status``). Ne contient aucun secret."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.status().to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p
