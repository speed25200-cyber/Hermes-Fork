"""``okxq jev validate-fixtures`` et ``okxq jev status``.

Aucune de ces commandes n'appelle le fournisseur : ``status`` hors ligne rend ``real_call: NOT_RUN``.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from okxq.cli_cmds._common import emit
from okxq.domain.errors import JevContractError, JevError
from okxq.jev.entity_mapping import EntityRegistry
from okxq.jev.quality import load_corpus
from okxq.jev.schemas import JevRequest, load_question_set, parse_json_strict, parse_response

app = typer.Typer(help="JEV (TypeSafe) : validation des fixtures de contrat et état du worker.")

DEFAULT_FIXTURES = Path("tests/fixtures/jev")
DEFAULT_QUESTIONS = Path("configs/jev_questions.v1.json")
DEFAULT_STATUS_FILE = Path("runtime/jev_status.json")


def _check(checks: list[dict[str, str]], name: str, fn: Any) -> Any:
    try:
        detail = fn()
    except (JevError, ValidationError, OSError, ValueError) as exc:
        checks.append({"check": name, "status": "fail", "detail": str(exc)[:500]})
        return None
    checks.append({"check": name, "status": "ok", "detail": "" if detail is None else str(detail)})
    return detail


@app.command("validate-fixtures")
def validate_fixtures(
    fixtures_dir: Path = typer.Option(DEFAULT_FIXTURES, "--fixtures-dir", help="Dossier des fixtures JEV."),
    questions: Path = typer.Option(DEFAULT_QUESTIONS, "--questions", help="Jeu de questions JEV."),
) -> None:
    """Valide request/response de référence, réponses invalides, corpus et registre. Sortie JSON, code 0/1."""
    checks: list[dict[str, str]] = []
    try:
        question_set = load_question_set(questions)
    except JevError as exc:
        checks.append({"check": "question_set", "status": "fail", "detail": str(exc)[:500]})
        emit({"ok": False, "real_call": "NOT_RUN", "checks": checks})
        sys.exit(1)
    checks.append(
        {
            "check": "question_set",
            "status": "ok",
            "detail": (
                f"{question_set.question_set_id} model={question_set.model} "
                f"questions={len(question_set.questions)} hash={question_set.question_set_hash[:16]}"
            ),
        }
    )

    def check_request() -> str:
        raw = parse_json_strict((fixtures_dir / "request_reference.json").read_bytes())
        request = JevRequest.model_validate(raw)
        if request.model != question_set.model:
            raise JevError("modèle de la requête de référence ≠ jeu de questions")
        if request.questions != question_set.questions:
            raise JevError("questions de la requête de référence ≠ jeu de questions v1")
        return f"payload_hash={request.payload_hash()[:16]}"

    def check_response() -> str:
        raw = (fixtures_dir / "response_reference.json").read_bytes()
        parsed = parse_response(raw, questions=question_set.questions, expected_model=question_set.model)
        return f"model_effective={parsed.model_effective} answers={len(parsed.answers)}"

    def check_invalid() -> str:
        cases = parse_json_strict((fixtures_dir / "invalid_responses.json").read_bytes())
        accepted: list[str] = []
        for case in cases:
            try:
                parse_response(
                    case["response"], questions=question_set.questions, expected_model=question_set.model
                )
                accepted.append(str(case["name"]))
            except JevContractError:
                continue
        nan_raw = (fixtures_dir / "response_invalid_nan.txt").read_bytes()
        try:
            parse_response(nan_raw, questions=question_set.questions, expected_model=question_set.model)
            accepted.append("nan_literal")
        except JevContractError:
            pass
        if accepted:
            raise JevError(f"réponses invalides ACCEPTÉES à tort : {accepted}")
        return f"{len(cases) + 1} réponses invalides refusées"

    def check_corpus() -> str:
        cases = load_corpus(fixtures_dir / "corpus.jsonl", question_set=question_set)
        tags = sorted({t for c in cases for t in c.tags})
        return f"{len(cases)} cas, étiquettes={tags}"

    def check_registry() -> str:
        registry = EntityRegistry.load(fixtures_dir / "entity_registry.json")
        return f"version={registry.version} records={len(registry.records)}"

    _check(checks, "request_reference", check_request)
    _check(checks, "response_reference", check_response)
    _check(checks, "invalid_responses", check_invalid)
    _check(checks, "corpus", check_corpus)
    _check(checks, "entity_registry", check_registry)
    failed = [c for c in checks if c["status"] == "fail"]
    emit({"ok": not failed, "real_call": "NOT_RUN", "checks": checks})
    if failed:
        sys.exit(1)


@app.command("status")
def status(
    status_file: Path = typer.Option(
        DEFAULT_STATUS_FILE, "--status-file", help="Instantané écrit par le worker."
    ),
    max_age_seconds: int = typer.Option(120, "--max-age-seconds", help="Au-delà, l'instantané est périmé."),
) -> None:
    """État du worker JEV s'il tourne ; hors ligne : ``NOT_RUN`` pour l'appel réel."""
    if not status_file.exists():
        emit(
            {
                "worker": "not_running",
                "available": False,
                "reasons": ["WORKER_NOT_RUNNING"],
                "real_call": "NOT_RUN",
                "status_file": str(status_file),
            }
        )
        return
    try:
        snapshot = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        emit(
            {
                "worker": "unreadable",
                "available": False,
                "reasons": ["STATUS_FILE_INVALID"],
                "detail": str(exc),
            }
        )
        sys.exit(1)
    generated = snapshot.get("generated_at")
    stale = True
    if isinstance(generated, str):
        try:
            age = (datetime.now(tz=UTC) - datetime.fromisoformat(generated)).total_seconds()
            stale = age > max_age_seconds
        except ValueError:
            stale = True
    emit({"worker": "stale" if stale else "running", "snapshot": snapshot})
