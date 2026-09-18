"""T66 : aucun secret ne peut apparaître dans un journal, quelle que soit la voie d'entrée."""

from __future__ import annotations

import io
import json
import logging

import pytest
from okxq.runtime.logging import (
    MASK,
    SecretMasker,
    bind_context,
    clear_context,
    configure_logging,
    context_scope,
    get_logger,
    get_masker,
)

OKX_SECRET = "3F1C9A7E2B6D4C8A9E0F1B2C3D4E5F60"
OKX_KEY = "3c1b2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
TYPESAFE = "ts-live-abcdef0123456789"


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    monkeypatch.setenv("OKX_API_SECRET", OKX_SECRET)
    monkeypatch.setenv("OKX_API_KEY", OKX_KEY)
    monkeypatch.setenv("TYPESAFE_API_KEY", TYPESAFE)
    monkeypatch.setenv("DATABASE_URL", "postgresql://okxq:motdepasse-secret@db:5432/okxq")
    get_masker().clear()
    stream = io.StringIO()
    configure_logging(level="DEBUG", json_output=True, mode="PAPER", stream=stream)
    clear_context()
    yield stream
    clear_context()
    get_masker().clear()


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_T66_injected_secret_never_appears_in_structlog_output(journal: io.StringIO) -> None:
    log = get_logger("okxq.test")
    log.info(
        "connexion",
        headers={"OK-ACCESS-KEY": OKX_KEY, "OK-ACCESS-SIGN": "sig==", "OK-ACCESS-PASSPHRASE": "pp"},
        note=f"la clé est {OKX_KEY} et le secret {OKX_SECRET}",
        nested={"config": {"api_secret": "xyz", "liste": [TYPESAFE, "ok"]}},
        url="postgresql://okxq:motdepasse-secret@db:5432/okxq",
        auth="Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
        assignment="OKX_API_SECRET=SHOULDNOTLEAK123456",
    )
    out = journal.getvalue()
    for secret in (OKX_SECRET, OKX_KEY, TYPESAFE, "motdepasse-secret", "SHOULDNOTLEAK123456", "sig=="):
        assert secret not in out
    assert "abcdefghijklmnopqrstuvwxyz0123" not in out
    assert MASK in out
    rec = _lines(journal)[0]
    assert rec["mode"] == "PAPER"
    assert rec["software_version"]
    assert rec["level"] == "info"
    assert rec["ts"]


def test_T66_stdlib_logs_from_uvicorn_are_masked_too(journal: io.StringIO) -> None:
    logging.getLogger("uvicorn.error").warning("requête refusée pour %s avec secret %s", OKX_KEY, OKX_SECRET)
    logging.getLogger("uvicorn.access").info("GET /?key=%s 200", TYPESAFE)
    out = journal.getvalue()
    assert OKX_KEY not in out
    assert OKX_SECRET not in out
    assert TYPESAFE not in out
    assert MASK in out


def test_T66_secret_inside_exception_text_is_masked(journal: io.StringIO) -> None:
    log = get_logger("okxq.test")
    try:
        raise RuntimeError(f"échec OKX avec passphrase {OKX_SECRET}")
    except RuntimeError:
        log.exception("erreur")
    assert OKX_SECRET not in journal.getvalue()


def test_correlation_context_is_propagated(journal: io.StringIO) -> None:
    log = get_logger("okxq.test")
    bind_context(trace_id="tr_1", decision_id="dec_1")
    with context_scope(intent_id="int_1", order_id="ord_1"):
        log.info("dans le contexte")
    log.info("hors contexte")
    recs = _lines(journal)
    assert recs[0]["trace_id"] == "tr_1"
    assert recs[0]["decision_id"] == "dec_1"
    assert recs[0]["intent_id"] == "int_1"
    assert recs[0]["order_id"] == "ord_1"
    assert "intent_id" not in recs[1]
    assert recs[1]["trace_id"] == "tr_1"


def test_masker_keeps_hashes_and_ids_readable() -> None:
    m = SecretMasker()
    payload_hash = "a" * 64
    out = m.mask({"payload_hash": payload_hash, "intent_id": "int_01hxyz", "contracts": "10"})
    assert out["payload_hash"] == payload_hash
    assert out["intent_id"] == "int_01hxyz"
    assert out["contracts"] == "10"
