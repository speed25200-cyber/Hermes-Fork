"""Client HTTP TypeSafe (``POST /v1/systemone``) à transport injectable.

Garanties :
- deadline MURALE totale (``timeout_total_ms``) mesurée par l'horloge injectée, distincte des timeouts de
  socket (connexion/lecture) ; aucune tentative ne démarre au-delà de la deadline ;
- ``max_retry_attempts`` borne les rejeux ; seuls 429 (avec ``Retry-After`` borné), 529/5xx, timeouts et
  erreurs de transport sont rejouables ; 401, 400 ``max_tokens_exceeded`` (état trop gros), 422 (schéma
  refusé) et toute réponse invalide (``JevContractError``) ne le sont jamais ;
- la clé d'API n'apparaît jamais dans les journaux, les erreurs ni ``repr`` ; elle n'est lue qu'au moment de
  poser l'en-tête ``Authorization`` ;
- l'état envoyé est un ``JevRequest`` strict : aucun credential OKX, aucune position, aucune identité.
"""

from __future__ import annotations

import asyncio
import email.utils
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn

import httpx
import structlog

from okxq.domain.clocks import Clock, SystemClock
from okxq.domain.errors import JevContractError, JevError
from okxq.jev.schemas import JevQuestionSpec, JevRequest, ParsedResponse, parse_json_strict, parse_response

log = structlog.get_logger("okxq.jev.client")

API_KEY_ENV = "TYPESAFE_API_KEY"  # nom de la variable d'environnement, jamais sa valeur
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})
DEFAULT_MAX_RETRY_AFTER_SECONDS = 5.0
DEFAULT_BACKOFF_SECONDS = 0.2
USER_AGENT = "okx-quant-jev/0.1 (jev-client)"

SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JevCallResult:
    """Résultat d'un appel réussi : réponses validées, version effective, usage facturé, mesures."""

    parsed: ParsedResponse
    attempts: int
    latency_ms: float
    status_code: int
    started_at: datetime
    finished_at: datetime

    @property
    def model_effective(self) -> str:
        return self.parsed.model_effective

    @property
    def usage(self) -> dict[str, Any]:
        return self.parsed.raw_usage


@dataclass(slots=True)
class _Attempt:
    number: int
    status_code: int | None = None
    error_code: str | None = None
    retryable: bool = False
    retry_after_s: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """``Retry-After`` en secondes (entier ou date HTTP) ; ``None`` si absent ou illisible."""
    if value is None or not value.strip():
        return None
    raw = value.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - now).total_seconds())


class JevClient:
    """Client asynchrone. ``transport`` permet un ``httpx.MockTransport`` dans les tests (aucun réseau)."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None,
        model: str,
        timeout_total_ms: int,
        max_retry_attempts: int,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout_s: float = 2.0,
        read_timeout_s: float = 5.0,
        max_retry_after_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleep: SleepFn | None = None,
    ) -> None:
        if not endpoint.startswith("https://"):
            raise JevError("endpoint JEV doit être en HTTPS", code="JEV_CONFIG_INVALID")
        if timeout_total_ms <= 0:
            raise JevError("timeout_total_ms doit être positif", code="JEV_CONFIG_INVALID")
        if max_retry_attempts < 0:
            raise JevError("max_retry_attempts doit être ≥ 0", code="JEV_CONFIG_INVALID")
        self._endpoint = endpoint
        self.__api_key = api_key.strip() if api_key else None  # jamais exposé
        self._model = model
        self._timeout_total_ms = timeout_total_ms
        self._max_retry_attempts = max_retry_attempts
        self._clock: Clock = clock or SystemClock()
        self._transport = transport
        self._connect_timeout_s = connect_timeout_s
        self._read_timeout_s = read_timeout_s
        self._max_retry_after_s = max_retry_after_seconds
        self._backoff_s = backoff_seconds
        self._sleep: SleepFn = sleep or asyncio.sleep
        self.last_model_effective: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial mais garanti sans secret
        return f"JevClient(endpoint={self._endpoint!r}, model={self._model!r}, key={'set' if self.has_api_key else 'missing'})"

    @property
    def has_api_key(self) -> bool:
        return bool(self.__api_key)

    @property
    def model(self) -> str:
        return self._model

    @property
    def timeout_total_ms(self) -> int:
        return self._timeout_total_ms

    # --- appel principal -----------------------------------------------------------------------------

    async def evaluate(self, request: JevRequest, questions: Mapping[str, JevQuestionSpec]) -> JevCallResult:
        """Envoie la requête et rend des réponses validées, ou lève un ``JevError`` typé."""
        if request.model != self._model:
            raise JevError(
                "la requête ne porte pas le modèle configuré",
                code="JEV_CONFIG_INVALID",
                requested=request.model,
                configured=self._model,
            )
        if not self.__api_key:
            raise JevError(
                f"clé {API_KEY_ENV} absente : aucun appel possible",
                code="JEV_API_KEY_MISSING",
                retryable=False,
            )
        body = request.payload()
        started_at = self._clock.now_utc()
        start_ns = self._clock.monotonic_ns()
        deadline_ns = start_ns + self._timeout_total_ms * 1_000_000
        attempts: list[_Attempt] = []
        last_error: JevError | None = None
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._read_timeout_s, connect=self._connect_timeout_s),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        ) as http:
            for number in range(1, self._max_retry_attempts + 2):
                remaining_s = (deadline_ns - self._clock.monotonic_ns()) / 1e9
                if remaining_s <= 0:
                    last_error = JevError(
                        "deadline totale dépassée avant tentative",
                        code="JEV_DEADLINE_EXCEEDED",
                        attempts=len(attempts),
                        retryable=False,
                    )
                    break
                attempt = _Attempt(number=number)
                attempts.append(attempt)
                try:
                    result = await self._attempt(http, body, questions, attempt, remaining_s)
                except JevError as exc:
                    last_error = exc
                    attempt.error_code = exc.code
                    attempt.retryable = bool(exc.context.get("retryable"))
                    log.warning(
                        "jev_attempt_failed",
                        attempt=number,
                        status=attempt.status_code,
                        code=exc.code,
                        retryable=attempt.retryable,
                    )
                    if not attempt.retryable or number > self._max_retry_attempts:
                        break
                    wait_s = self._wait_before_retry(attempt, deadline_ns)
                    if wait_s is None:
                        last_error = JevError(
                            "rejeu impossible dans la deadline restante",
                            code=exc.code,
                            attempts=len(attempts),
                            retryable=False,
                            retry_after_s=attempt.retry_after_s,
                        )
                        break
                    if wait_s > 0:
                        await self._sleep(wait_s)
                    continue
                finished_at = self._clock.now_utc()
                latency_ms = (self._clock.monotonic_ns() - start_ns) / 1e6
                self.last_model_effective = result.model_effective
                log.info(
                    "jev_call_ok",
                    attempts=number,
                    latency_ms=round(latency_ms, 3),
                    model_requested=self._model,
                    model_effective=result.model_effective,
                    usage=result.raw_usage,
                    unknown_answer_ids=list(result.unknown_answer_ids),
                )
                return JevCallResult(
                    parsed=result,
                    attempts=number,
                    latency_ms=latency_ms,
                    status_code=attempt.status_code or 200,
                    started_at=started_at,
                    finished_at=finished_at,
                )
        assert last_error is not None
        last_error.context.setdefault("attempts", len(attempts))
        last_error.context["latency_ms"] = (self._clock.monotonic_ns() - start_ns) / 1e6
        raise last_error

    # --- une tentative --------------------------------------------------------------------------------

    async def _attempt(
        self,
        http: httpx.AsyncClient,
        body: dict[str, Any],
        questions: Mapping[str, JevQuestionSpec],
        attempt: _Attempt,
        remaining_s: float,
    ) -> ParsedResponse:
        # Les timeouts de socket sont bornés par la deadline murale restante.
        socket_timeout = httpx.Timeout(
            min(self._read_timeout_s, remaining_s), connect=min(self._connect_timeout_s, remaining_s)
        )
        try:
            async with asyncio.timeout(remaining_s):
                response = await http.post(
                    self._endpoint,
                    json=body,
                    headers={"Authorization": f"Bearer {self.__api_key}"},
                    timeout=socket_timeout,
                )
        except TimeoutError as exc:
            raise JevError("délai d'attente dépassé", code="JEV_TIMEOUT", retryable=True) from exc
        except httpx.TimeoutException as exc:
            raise JevError("délai de socket dépassé", code="JEV_TIMEOUT", retryable=True) from exc
        except httpx.TransportError as exc:
            raise JevError(
                f"erreur de transport : {type(exc).__name__}", code="JEV_TRANSPORT_ERROR", retryable=True
            ) from exc
        attempt.status_code = response.status_code
        if response.status_code == 200:
            try:
                return parse_response(response.content, questions=questions, expected_model=self._model)
            except JevContractError as exc:
                exc.context["retryable"] = False
                raise
        self._raise_for_status(response, attempt)

    def _raise_for_status(self, response: httpx.Response, attempt: _Attempt) -> NoReturn:
        status = response.status_code
        detail = _safe_detail(response)
        if status == 401:
            raise JevError("clé d'API refusée par le fournisseur", code="JEV_AUTH_REJECTED", retryable=False)
        if status == 400:
            error_type = detail.get("error_type") if isinstance(detail, Mapping) else None
            if error_type == "max_tokens_exceeded":
                raise JevError(
                    "état trop volumineux pour le modèle (non rejouable)",
                    code="JEV_STATE_TOO_LARGE",
                    retryable=False,
                    error_type=error_type,
                )
            raise JevError(
                "requête refusée (400)", code="JEV_BAD_REQUEST", retryable=False, error_type=error_type
            )
        if status == 422:
            raise JevError("schéma de requête refusé (422)", code="JEV_SCHEMA_REJECTED", retryable=False)
        if status == 429:
            attempt.retry_after_s = parse_retry_after(
                response.headers.get("Retry-After"), now=self._clock.now_utc()
            )
            raise JevError(
                "limite de débit atteinte (429)",
                code="JEV_RATE_LIMITED",
                retryable=True,
                retry_after_s=attempt.retry_after_s,
            )
        if status in RETRYABLE_STATUS or status >= 500:
            attempt.retry_after_s = parse_retry_after(
                response.headers.get("Retry-After"), now=self._clock.now_utc()
            )
            raise JevError(
                f"fournisseur indisponible ({status})",
                code="JEV_UPSTREAM_UNAVAILABLE",
                retryable=True,
                status=status,
            )
        raise JevError(
            f"réponse HTTP inattendue ({status})", code="JEV_HTTP_UNEXPECTED", retryable=False, status=status
        )

    def _wait_before_retry(self, attempt: _Attempt, deadline_ns: int) -> float | None:
        """Attente avant rejeu, bornée par ``max_retry_after_seconds`` et la deadline ; ``None`` = pas de rejeu."""
        remaining_s = (deadline_ns - self._clock.monotonic_ns()) / 1e9
        if attempt.retry_after_s is not None:
            if attempt.retry_after_s > self._max_retry_after_s or attempt.retry_after_s >= remaining_s:
                return None
            wait_s = attempt.retry_after_s
        else:
            wait_s = self._backoff_s * attempt.number
        if wait_s >= remaining_s:
            return None
        return max(0.0, wait_s)


def _safe_detail(response: httpx.Response) -> Mapping[str, Any]:
    """``detail`` d'une réponse d'erreur, sans jamais lever ni journaliser le corps brut."""
    try:
        data = parse_json_strict(response.content[:65536])
    except JevContractError:
        return {}
    if isinstance(data, Mapping):
        detail = data.get("detail")
        if isinstance(detail, Mapping):
            return detail
        if isinstance(detail, str):
            return {"message": detail[:200]}
    return {}


def percentile(values: list[float], fraction: float) -> float | None:
    """Percentile par interpolation linéaire (p50/p95 de latence) ; ``None`` sans échantillon."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
