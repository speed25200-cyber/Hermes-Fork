"""Client TypeSafe : deadline murale, rejeux bornés, usage journalisé, clé jamais exposée. Aucun réseau."""

import json
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import JevContractError, JevError
from okxq.jev.client import JevClient, parse_retry_after, percentile
from okxq.jev.schemas import JevRequest, load_question_set

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "jev"
QUESTIONS = load_question_set(ROOT / "configs" / "jev_questions.v1.json")
REQUEST = JevRequest.model_validate(json.loads((FIX / "request_reference.json").read_text(encoding="utf-8")))
RESPONSE = json.loads((FIX / "response_reference.json").read_text(encoding="utf-8"))
KEY = "sk-test-secret-key-0123456789"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class Recorder:
    def __init__(self, clock: SimulatedClock, script):
        self.clock = clock
        self.script = list(script)
        self.requests: list[httpx.Request] = []
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(request)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(step):
            return step(request)
        return step


def ok_response(_request=None) -> httpx.Response:
    return httpx.Response(200, json=RESPONSE)


def make_client(clock: SimulatedClock, handler, *, api_key=KEY, timeout_total_ms=1500, max_retry=1, sleeps=None):
    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)
        clock.advance_seconds(seconds)

    return JevClient(
        endpoint=ENDPOINT,
        api_key=api_key,
        model="jev-1.13.0",
        timeout_total_ms=timeout_total_ms,
        max_retry_attempts=max_retry,
        clock=clock,
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )


async def test_success_reports_effective_model_usage_and_sends_only_the_strict_state(clock):
    rec = Recorder(clock, [ok_response])
    client = make_client(clock, rec)
    with capture_logs() as logs:
        result = await client.evaluate(REQUEST, QUESTIONS.questions)
    assert result.model_effective == "jev-1.13.0" and result.attempts == 1 and result.status_code == 200
    assert result.usage["input_tokens"] == 812 and result.usage["output_tokens"] == 96
    assert client.last_model_effective == "jev-1.13.0"
    sent = json.loads(rec.requests[0].content)
    assert set(sent) == {"model", "state", "questions"}
    assert set(sent["state"]) == {"asset", "document", "prior_documents"}
    assert rec.requests[0].headers["Authorization"] == f"Bearer {KEY}"
    assert rec.requests[0].url == httpx.URL(ENDPOINT)
    ok_events = [e for e in logs if e["event"] == "jev_call_ok"]
    assert ok_events and ok_events[0]["model_effective"] == "jev-1.13.0" and ok_events[0]["usage"]["input_tokens"] == 812


async def test_api_key_never_appears_in_logs_errors_or_repr(clock):
    rec = Recorder(clock, [httpx.Response(401, json={"detail": "invalid key"})])
    client = make_client(clock, rec)
    with capture_logs() as logs, pytest.raises(JevError) as info:
        await client.evaluate(REQUEST, QUESTIONS.questions)
    assert info.value.code == "JEV_AUTH_REJECTED" and rec.calls == 1
    haystack = " ".join([str(info.value), repr(info.value.context), repr(client), json.dumps(logs, default=str)])
    assert KEY not in haystack
    assert "Bearer" not in haystack


async def test_missing_key_means_no_call_at_all(clock):
    rec = Recorder(clock, [ok_response])
    client = make_client(clock, rec, api_key=None)
    with pytest.raises(JevError) as info:
        await client.evaluate(REQUEST, QUESTIONS.questions)
    assert info.value.code == "JEV_API_KEY_MISSING" and rec.calls == 0 and client.has_api_key is False


async def test_total_deadline_is_wall_clock_and_prevents_a_second_attempt(clock):
    def slow_then_fail(_request):
        clock.advance_seconds(2.0)  # la tentative a consommé plus que timeout_total_ms=1500
        return httpx.Response(503)

    rec = Recorder(clock, [slow_then_fail])
    client = make_client(clock, rec, max_retry=3)
    with pytest.raises(JevError) as info:
        await client.evaluate(REQUEST, QUESTIONS.questions)
    assert rec.calls == 1
    assert info.value.code in ("JEV_UPSTREAM_UNAVAILABLE", "JEV_DEADLINE_EXCEEDED")
    assert info.value.context["attempts"] == 1
    assert info.value.context["latency_ms"] >= 2000


async def test_retries_are_bounded_by_max_retry_attempts(clock):
    rec = Recorder(clock, [httpx.Response(503)])
    client = make_client(clock, rec, max_retry=2, timeout_total_ms=60_000)
    with pytest.raises(JevError) as info:
        await client.evaluate(REQUEST, QUESTIONS.questions)
    assert rec.calls == 3 and info.value.context["attempts"] == 3
    assert info.value.code == "JEV_UPSTREAM_UNAVAILABLE"


async def test_retry_after_is_honoured_when_bounded_and_refused_otherwise(clock):
    sleeps: list[float] = []
    rec = Recorder(clock, [httpx.Response(429, headers={"Retry-After": "1"}), ok_response])
    client = make_client(clock, rec, timeout_total_ms=10_000, sleeps=sleeps)
    result = await client.evaluate(REQUEST, QUESTIONS.questions)
    assert result.attempts == 2 and sleeps == [1.0]
    rec2 = Recorder(clock, [httpx.Response(429, headers={"Retry-After": "60"}), ok_response])
    client2 = make_client(clock, rec2, timeout_total_ms=120_000, max_retry=3)
    with pytest.raises(JevError) as info:
        await client2.evaluate(REQUEST, QUESTIONS.questions)
    assert info.value.code == "JEV_RATE_LIMITED" and rec2.calls == 1


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (400, {"detail": {"error_type": "max_tokens_exceeded"}}, "JEV_STATE_TOO_LARGE"),
        (400, {"detail": {"error_type": "other"}}, "JEV_BAD_REQUEST"),
        (422, {"detail": "schema"}, "JEV_SCHEMA_REJECTED"),
        (401, {}, "JEV_AUTH_REJECTED"),
        (418, {}, "JEV_HTTP_UNEXPECTED"),
    ],
)
async def test_non_retryable_statuses_fail_after_one_call(clock, status, body, code):
    rec = Recorder(clock, [httpx.Response(status, json=body)])
    client = make_client(clock, rec, max_retry=3)
    with pytest.raises(JevError) as info:
        await client.evaluate(REQUEST, QUESTIONS.questions)
    assert info.value.code == code and rec.calls == 1
    assert info.value.context.get("retryable") is False


async def test_invalid_body_is_a_contract_error_and_never_retried(clock):
    bad = json.loads(json.dumps(RESPONSE))
    bad["model"] = "jev-2.0.0"
    rec = Recorder(clock, [httpx.Response(200, json=bad)])
    client = make_client(clock, rec, max_retry=3)
    with pytest.raises(JevContractError):
        await client.evaluate(REQUEST, QUESTIONS.questions)
    assert rec.calls == 1


async def test_timeout_and_transport_errors_are_retried_then_succeed(clock):
    def boom(request):
        raise httpx.ReadTimeout("lent", request=request)

    rec = Recorder(clock, [boom, ok_response])
    client = make_client(clock, rec, timeout_total_ms=10_000)
    result = await client.evaluate(REQUEST, QUESTIONS.questions)
    assert result.attempts == 2 and rec.calls == 2


async def test_request_model_must_match_client_model(clock):
    rec = Recorder(clock, [ok_response])
    client = make_client(clock, rec)
    other = REQUEST.model_copy(update={"model": "jev-1.12.0"})
    with pytest.raises(JevError) as info:
        await client.evaluate(other, QUESTIONS.questions)
    assert info.value.code == "JEV_CONFIG_INVALID" and rec.calls == 0


def test_client_refuses_insecure_or_absurd_configuration(clock):
    with pytest.raises(JevError):
        JevClient(endpoint="http://api.typesafe.ai/v1/systemone", api_key=KEY, model="m", timeout_total_ms=10, max_retry_attempts=0)
    with pytest.raises(JevError):
        JevClient(endpoint=ENDPOINT, api_key=KEY, model="m", timeout_total_ms=0, max_retry_attempts=0)


def test_retry_after_parsing_and_percentiles(clock):
    now = clock.now_utc()
    assert parse_retry_after("7", now=now) == 7.0
    assert parse_retry_after(None, now=now) is None
    assert parse_retry_after("garbage", now=now) is None
    assert parse_retry_after("Thu, 18 Sep 2026 12:00:05 GMT", now=now) == pytest.approx(5.0)
    assert percentile([], 0.5) is None
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
