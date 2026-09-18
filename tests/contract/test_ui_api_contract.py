"""Contrat entre l'interface conservée et l'API (§59).

L'interface lit des clés PRÉCISES dans les réponses JSON. Un renommage côté API n'est pas une erreur
Python : la page continue de s'afficher, mais chaque valeur devient « Non disponible ». C'est
exactement le défaut qu'a révélé le smoke navigateur (le bloc de risque était publié sous ``halts``
tandis que ``pages.js`` lisait ``risk``).

Ce test verrouille les deux côtés sans navigateur :

1. l'API répond bien, pour chaque endpoint appelé par l'interface, avec les clés attendues ;
2. ``pages.js`` référence bien ces mêmes noms.

Il tourne donc aussi là où aucun Chromium n'est disponible.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from okxq.api.app import create_app, role_key_for
from okxq.api.auth import Role
from okxq.config import load_config
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import Base, EvaluationReport, ExperimentRun, RiskState

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
SECRET = "secret-contrat-interface-0123456789"
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

# (endpoint, clé de premier niveau lue par l'interface, champs attendus dans ce bloc)
# Chaque entrée cite la ligne de `pages.js` qui la consomme.
BLOCKS: list[tuple[str, str, tuple[str, ...]]] = [
    # chargerRisque() : `etat.halts` alimente les cinq lignes de l'état de risque.
    (
        "/api/v1/system/status",
        "halts",
        ("halt_level", "halt_reason", "day_realized_loss", "high_water_mark_equity", "limits_version"),
    ),
]

# (endpoint, clés de premier niveau que l'interface lit directement sur la réponse)
TOP_LEVEL: list[tuple[str, tuple[str, ...]]] = [
    # chargerRisque()/majEntete() : mode et compte sont affichés en permanence.
    ("/api/v1/system/status", ("ok", "mode", "account_scope", "synthetic_data", "halts")),
    ("/api/v1/decisions", ("ok", "items")),
    ("/api/v1/experiments", ("ok", "items", "jev_variants")),
    ("/api/v1/models", ("ok", "items")),
    (
        "/api/v1/jev/status",
        (
            "ok",
            "enabled",
            "influence_mode",
            "available",
            "age_seconds",
            "latency_p95_ms",
            "cache_hit_ratio",
            "errors",
            "daily_spend_usd",
            "sources",
        ),
    ),
    ("/api/v1/jev/events", ("ok", "items")),
    ("/api/v1/risk/events", ("ok", "items")),
    ("/api/v1/data/quality", ("ok", "items")),
]


def _seed(factory: Any) -> None:
    """Le strict minimum pour que les blocs imbriqués soient RÉELLEMENT parcourus (pas sautés).

    Sans ``RiskState`` le bloc ``halts`` vaut null, et le test qui aurait dû attraper le renommage
    ``halts``/``risk`` se contenterait de sauter. Un test sauté n'est pas un test qui passe.
    """
    with factory() as session:
        session.add(
            RiskState(
                account_scope="smoke-fixture",
                halt_level="NONE",
                halt_reason=None,
                halt_since=None,
                utc_day=T0,
                day_start_equity=Decimal("10000"),
                day_realized_loss=Decimal("0"),
                high_water_mark_unit=Decimal("1"),
                high_water_mark_equity=Decimal("10000"),
                limits_version="limits-v1",
                updated_at=T0,
                version=1,
            )
        )
        session.add(
            ExperimentRun(
                run_id="run_1",
                plan_id="plan_1",
                plan_hash="h_plan",
                plan={"hypothesis": "le composant sémantique ajoute-t-il un avantage net ?"},
                status="DONE",
                started_at=T0,
                finished_at=T0,
                trials=[],
                final_test_consulted_at=None,
                code_commit="deadbeef",
                seed=25200,
            )
        )
        # Le rapport référence la campagne : il faut que la ligne parente existe AVANT son insertion,
        # sinon SQLite refuse la clé étrangère (l'ordre dans un même flush n'est pas garanti).
        session.flush()
        session.add(
            EvaluationReport(
                report_id="rep_1",
                run_id="run_1",
                model_id=None,
                kind="jev_ablation",
                period_start=T0,
                period_end=T0,
                independent=True,
                metrics={"variant": "sans_jev", "net_pnl": "-1.25"},
                created_at=T0,
            )
        )
        session.commit()


@pytest.fixture
def client() -> TestClient:
    cfg = load_config(ROOT / "tests" / "fixtures" / "configs" / "smoke.fixture.yaml")
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    _seed(factory)
    app = create_app(
        cfg,
        session_factory=factory,
        operator_secret=SECRET,
        frontend_dir=ROOT / "frontend",
        synthetic_data=True,
        configure_logs=False,
    )
    c = TestClient(app)
    # La clé se présente en Bearer (ou en paramètre de requête à la première ouverture de page).
    c.headers["Authorization"] = f"Bearer {role_key_for(SECRET, Role.READER)}"
    return c


def _payload(client: TestClient, endpoint: str) -> dict[str, Any]:
    response = client.get(endpoint)
    assert response.status_code == 200, f"{endpoint} → {response.status_code} {response.text[:200]}"
    body = response.json()
    assert isinstance(body, dict), f"{endpoint} ne renvoie pas un objet JSON"
    return body


@pytest.mark.parametrize(("endpoint", "keys"), TOP_LEVEL, ids=[e for e, _ in TOP_LEVEL])
def test_api_publishes_keys_read_by_the_interface(
    client: TestClient, endpoint: str, keys: tuple[str, ...]
) -> None:
    body = _payload(client, endpoint)
    missing = [k for k in keys if k not in body]
    assert not missing, f"{endpoint} : clés absentes {missing} (l'interface afficherait « Non disponible »)"


@pytest.mark.parametrize(("endpoint", "block", "fields"), BLOCKS, ids=[f"{e}:{b}" for e, b, _ in BLOCKS])
def test_nested_blocks_carry_the_expected_fields(
    client: TestClient, endpoint: str, block: str, fields: tuple[str, ...]
) -> None:
    body = _payload(client, endpoint)
    # La fixture sème les lignes nécessaires : un bloc null ici signifie que l'API ne relit plus
    # ces tables, ce qui est précisément le défaut à attraper.
    assert body[block] is not None, f"{endpoint}.{block} est null alors que la table est peuplée"
    missing = [f for f in fields if f not in body[block]]
    assert not missing, f"{endpoint}.{block} : champs absents {missing}"


def test_interface_reads_the_names_the_api_actually_serves() -> None:
    """L'autre moitié du contrat : `pages.js` doit citer les endpoints et les blocs réels."""
    source = (ROOT / "frontend" / "pages.js").read_text(encoding="utf-8")
    for endpoint, _ in TOP_LEVEL:
        assert f'"{endpoint}"' in source, f"{endpoint} n'est plus appelé par l'interface"
    for endpoint, block, _ in BLOCKS:
        assert re.search(rf"\betat\.{re.escape(block)}\b", source), (
            f"l'interface ne lit plus `etat.{block}` ({endpoint}) : le bloc a été renommé d'un seul côté"
        )


def test_jev_ablation_rows_carry_the_fields_the_interface_displays(client: TestClient) -> None:
    """Le panneau A/B lit `variant`, `net_pnl`, `period_start` et `independent` sur chaque ligne."""
    rows = _payload(client, "/api/v1/experiments")["jev_variants"]
    assert rows, "le rapport d'ablation semé n'apparaît pas : le `kind` n'est plus reconnu"
    for row in rows:
        missing = [f for f in ("variant", "net_pnl", "period_start", "independent") if f not in row]
        assert not missing, f"ligne d'ablation incomplète : {missing}"
