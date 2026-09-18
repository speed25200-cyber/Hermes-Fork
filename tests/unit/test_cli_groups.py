"""Groupes CLI ``api``, ``backtest``, ``reports`` : ce qu'ils exposent et ce qu'ils refusent (§67).

Un inventaire de surface qui sous-déclare est pire que pas d'inventaire : on croit avoir tout vu.
Ces tests vérifient donc surtout que les commandes disent la vérité sur l'état du système.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from okxq.api.app import create_app, role_key_for
from okxq.api.auth import Role
from okxq.cli import app as cli
from okxq.cli_cmds.api_cmd import _flatten
from okxq.config import load_config
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import Base

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "tests" / "fixtures" / "configs" / "smoke.fixture.yaml"
SECRET = "secret-de-test-des-groupes-cli-0123456789"

#: Routes délibérément ouvertes. Une sonde de vivacité inatteignable sans clé ne sert à rien : un
#: orchestrateur ne redémarrerait jamais un processus mort.
PUBLIC_PATHS = frozenset({"/health/live", "/health/ready"})

#: Flux persistants : une requête authentifiée sur un flux d'événements ne se termine jamais, et le
#: client de test attendrait indéfiniment. Le refus anonyme est vérifié, l'acceptation authentifiée
#: ne peut pas l'être par une simple requête et relève du test d'interface.
STREAMING_PATHS = frozenset({"/api/flux"})

#: Pages et schéma : servis sans clé pour permettre l'authentification elle-même.
BOOTSTRAP_PATHS = frozenset(
    {"/", "/index.html", "/api/v1/docs", "/api/v1/openapi.json", "/docs/oauth2-redirect"}
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cfg():
    return load_config(CONFIG)


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _payload(output: str) -> dict:
    """Extrait le JSON d'une sortie qui peut être précédée de lignes de journal."""
    return json.loads(output[output.index("{") :])


# --- inventaire des routes ----------------------------------------------------------------------------


def test_the_route_inventory_does_not_under_report_the_exposed_surface(cfg) -> None:
    """FastAPI range les routeurs inclus dans des conteneurs : ne lire qu'un niveau donnait 6 routes
    là où il y en a une trentaine. Pour un inventaire de sécurité, c'est le pire des résultats."""
    application = create_app(cfg, operator_secret=SECRET, configure_logs=False)
    inventory = _flatten(application.routes)
    paths = {r["path"] for r in inventory}
    assert len(inventory) > 20, f"inventaire manifestement incomplet : {len(inventory)} routes"
    for expected in ("/api/v1/system/status", "/api/v1/decisions", "/api/v1/control/{command}"):
        assert expected in paths, f"{expected} absent de l'inventaire"
    # Le rôle est lu par introspection : il vaut None quand il n'est pas détectable, jamais une
    # valeur devinée.
    assert {r["role_minimal"] for r in inventory} <= {None, "reader", "operator", "admin"}


def test_every_route_that_is_not_a_health_probe_refuses_an_anonymous_caller(cfg, factory) -> None:
    """La vraie question n'est pas ce que l'inventaire annonce, mais ce que le serveur répond.

    Un client est créé par appel : le middleware délivre un cookie de session signé lors d'un appel
    authentifié, et un client réutilisé le présenterait ensuite en se croyant anonyme — le test
    passerait alors pour de mauvaises raisons.
    """
    application = create_app(cfg, session_factory=factory, operator_secret=SECRET, configure_logs=False)
    key = role_key_for(SECRET, Role.READER)
    checked = 0
    for route in _flatten(application.routes):
        path, methods = str(route["path"]), route["methods"]
        if "GET" not in methods or "{" in path or path in PUBLIC_PATHS:
            continue
        if path in BOOTSTRAP_PATHS:
            continue
        # Un client NEUF par appel, sur la même application : le cookie de session est porté par le
        # client, pas par l'application, et c'est lui qui rendrait un appel « anonyme » authentifié.
        anonymous = TestClient(application).get(path)
        assert anonymous.status_code == 403, f"{path} répond {anonymous.status_code} sans clé"
        checked += 1
        if path in STREAMING_PATHS:
            continue
        with_key = TestClient(application).get(path, headers={"Authorization": f"Bearer {key}"})
        assert with_key.status_code == 200, f"{path} refuse une clé de lecture valide"
    assert checked >= 10, f"trop peu de routes réellement éprouvées : {checked}"


def test_health_probes_stay_reachable_without_a_key(cfg, factory) -> None:
    application = create_app(cfg, session_factory=factory, operator_secret=SECRET, configure_logs=False)
    for path in sorted(PUBLIC_PATHS):
        assert TestClient(application).get(path).status_code == 200, f"{path} doit rester joignable sans clé"


# --- clés d'accès -------------------------------------------------------------------------------------


def test_the_keys_command_never_prints_a_key_by_default(runner, tmp_path, monkeypatch) -> None:
    """Une clé écrite sur la sortie standard finit dans l'historique du shell et dans les journaux :
    elle cesse d'être un secret au moment où on la lit."""
    monkeypatch.setenv("OPERATOR_AUTH_SECRET", SECRET)
    out = tmp_path / "cles.txt"
    result = runner.invoke(cli, ["api", "keys", "--out", str(out)])
    assert result.exit_code == 0, result.output
    payload = _payload(result.output)
    derived = {role: role_key_for(SECRET, Role(role)) for role in ("reader", "operator", "admin")}
    for role, key in derived.items():
        assert key not in result.output, f"la clé {role} a été affichée"
        assert key in out.read_text(encoding="utf-8"), f"la clé {role} manque dans le fichier"
    # Le fichier est en 0600 : une clé lisible par le groupe sur un hôte partagé n'est plus une clé.
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert payload["permissions"] == "0o600"
    # Les empreintes permettent de comparer sans divulguer.
    for role, key in derived.items():
        assert payload["empreintes"][role] == key[:8] + "…"


def test_the_keys_command_refuses_without_an_operator_secret(runner, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPERATOR_AUTH_SECRET", raising=False)
    result = runner.invoke(cli, ["api", "keys", "--out", str(tmp_path / "cles.txt")])
    assert result.exit_code == 1
    assert "OPERATOR_AUTH_SECRET" in result.output


# --- backtest -----------------------------------------------------------------------------------------


def test_the_stress_multipliers_are_not_configurable() -> None:
    """Un stress qu'on peut assouplir jusqu'à obtenir le verdict souhaité ne teste plus rien."""
    from okxq.cli_cmds.backtest_cmd import STRESS_MULTIPLIERS

    assert STRESS_MULTIPLIERS[0] == "1", "le premier multiplicateur est la référence"
    assert [int(m) for m in STRESS_MULTIPLIERS] == sorted(int(m) for m in STRESS_MULTIPLIERS)
    assert len(STRESS_MULTIPLIERS) >= 3


def test_stress_only_hardens_cost_assumptions(cfg) -> None:
    """Durcir aussi les limites de risque mélangerait deux questions et rendrait le verdict illisible."""
    from decimal import Decimal

    from okxq.cli_cmds.backtest_cmd import _stressed

    hardened = _stressed(cfg, Decimal("2"))
    assert hardened.risk == cfg.risk, "les limites de risque ne doivent pas bouger"
    assert hardened.mode is cfg.mode
    assert (
        hardened.execution.max_order_participation_fraction < cfg.execution.max_order_participation_fraction
    )
    assert hardened.execution.max_depth_consumption_fraction < cfg.execution.max_depth_consumption_fraction


def test_backtest_refuses_an_unknown_scenario(runner, tmp_path) -> None:
    dataset = tmp_path / "jeu"
    dataset.mkdir()
    result = runner.invoke(
        cli,
        ["backtest", "run", "--dataset", str(dataset), "--config", str(CONFIG), "--scenario", "magique"],
    )
    assert result.exit_code == 1
    assert "scénario inconnu" in result.output


# --- rapports -----------------------------------------------------------------------------------------


def test_reports_write_null_and_never_zero_for_an_absent_measure(cfg, factory) -> None:
    """Un rapport qui écrit 0 là où il n'a pas mesuré transforme une ignorance en affirmation."""
    from okxq.cli_cmds.reports_cmd import decisions_payload, equity_payload, risk_payload

    decisions = decisions_payload(cfg, factory, 1)
    assert decisions["decisions"] == 0, "le compte d'éléments, lui, est bien zéro"
    assert decisions["duree_mediane_ms"] is None, "aucune décision ⇒ aucune durée médiane"
    assert decisions["duree_max_ms"] is None

    risk = risk_payload(cfg, factory, 1)
    # `null` et non « NONE » : une absence d'état persisté n'est pas un état sain.
    assert risk["etat"] is None

    equity = equity_payload(cfg, factory, 1)
    assert equity["equite"] is None
    assert equity["positions_ouvertes"] == []
    assert equity["executions"]["frais"] is None, "aucune exécution ⇒ frais non mesurés, pas nuls"


def test_the_daily_report_reuses_the_same_builders_as_the_individual_reports() -> None:
    """Deux chemins de calcul pour un même chiffre finissent par diverger, et c'est alors la version
    fausse qu'on cite."""
    from okxq.cli_cmds.reports_cmd import (
        BUILDERS,
        decisions_payload,
        equity_payload,
        risk_payload,
    )

    assert BUILDERS == {
        "decisions": decisions_payload,
        "risque": risk_payload,
        "equite": equity_payload,
    }
