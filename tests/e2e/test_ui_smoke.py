"""Smoke de l'interface conservée dans un vrai navigateur (§59).

Ce test répond à des questions qu'aucun test Python ne peut trancher : la page se charge-t-elle sans
erreur JavaScript, le mode est-il visible, la bascule de langue fonctionne-t-elle, les nouvelles vues
se rendent-elles, et une commande critique demande-t-elle bien une confirmation montrant le compte et
le mode ?

Il saute proprement (avec son motif) si Chromium n'est pas disponible : un test non exécuté reste
NOT_RUN, jamais PASS.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from okxq.api.app import create_app, role_key_for
from okxq.api.auth import Role
from okxq.config import load_config
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import (
    AccountSnapshot,
    Base,
    DataQualityEvent,
    Decision,
    PositionSnapshot,
    RiskEventRow,
    RiskState,
)

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
SECRET = "secret-interface-de-test-0123456789"
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _seed(factory) -> None:
    """Quelques lignes réelles : la page doit montrer des données, pas des zéros inventés."""
    with factory() as session:
        session.add(
            AccountSnapshot(
                account_scope="smoke-fixture",
                equity_version="eq_1",
                as_of=T0,
                source="ledger",
                cash_collateral=Decimal("10000"),
                unrealized_pnl=Decimal("12.5"),
                equity=Decimal("10012.5"),
                available_margin=Decimal("9000"),
                used_margin=Decimal("1000"),
                external_cashflow_cum=Decimal("10000"),
                unit_value=Decimal("1.00125"),
                raw={},
            )
        )
        session.add(
            PositionSnapshot(
                account_scope="smoke-fixture",
                inst_id="BTC-USDT-SWAP",
                as_of=T0,
                source="ledger",
                signed_base_qty=Decimal("0.01"),
                signed_contracts=Decimal("1"),
                average_entry_price=Decimal("65000"),
                mark_price=Decimal("65250"),
                liquidation_price=None,
                margin=Decimal("130"),
                leverage=Decimal("5"),
                protection={"stop_loss": "64000", "confirmed": True, "stop_mode": "INIT"},
                version=1,
            )
        )
        session.add(
            Decision(
                decision_id="dec_1",
                mode="PAPER",
                snapshot_id="snap_1",
                cutoff_at=T0,
                started_at=T0,
                finished_at=T0 + timedelta(seconds=1),
                outcome="NO_TRADE",
                reason_codes=["EDGE_BELOW_COSTS"],
                model_id="m_test",
                universe_version="uni_1",
                equity_version="eq_1",
                inputs_hash="h",
                forecasts=[],
                edges=[],
                rejected_alternatives=[
                    {
                        "instrument": "BTC-USDT-SWAP",
                        "side": "LONG",
                        "reason": "EDGE_BELOW_COSTS",
                        "net_edge": "-0.0002",
                        "uncertainty_penalty": "0.0001",
                    }
                ],
                timings_ms={"features": 12, "inference": 5, "optimizer": 8},
                software_version="0.1.0",
                trace_id="trc_1",
            )
        )
        session.add(
            RiskState(
                account_scope="smoke-fixture",
                halt_level="NONE",
                halt_reason=None,
                halt_since=None,
                utc_day=T0,
                day_start_equity=Decimal("10000"),
                day_realized_loss=Decimal("0"),
                high_water_mark_unit=Decimal("1.00125"),
                high_water_mark_equity=Decimal("10012.5"),
                limits_version="limits-v1",
                updated_at=T0,
                version=1,
            )
        )
        session.add(
            RiskEventRow(
                event_id="rev_1",
                created_at=T0,
                severity="WARN",
                reason_code="DATA_STALE",
                affected_scope="BTC-USDT-SWAP",
                evidence={"age_ms": 3200},
                requested_action="SOFT_HALT",
                observed_result="entrées suspendues",
            )
        )
        session.add(
            DataQualityEvent(
                occurred_at=T0,
                inst_id="BTC-USDT-SWAP",
                channel="books",
                kind="sequence_gap",
                severity="WARN",
                details={"expected": 100, "got": 140},
            )
        )
        session.commit()


@pytest.fixture
def server() -> Iterator[tuple[str, str]]:
    import uvicorn

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
    port = _free_port()
    # `log_config=None` : uvicorn ne doit PAS reconfigurer le logging du processus. Sa configuration
    # par défaut repose sur dictConfig et couperait la propagation vers notre handler masqué — ce qui
    # vaut aussi en exploitation, où le masquage des secrets ne peut dépendre d'un ordre d'import.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="off", log_config=None
    )
    server_obj = uvicorn.Server(config)
    thread = threading.Thread(target=server_obj.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server_obj.started and time.time() < deadline:
        time.sleep(0.05)
    if not server_obj.started:
        pytest.skip("serveur de test non démarré : NOT_RUN")
    key = role_key_for(SECRET, Role.OPERATOR)
    yield f"http://127.0.0.1:{port}", key
    server_obj.should_exit = True
    thread.join(timeout=10)


def _chromium_executable() -> str | None:
    """Binaire Chromium déjà présent, s'il ne correspond pas à la révision attendue par Playwright.

    L'environnement fournit un Chromium préinstallé dont la révision peut différer de celle que la
    version épinglée de Playwright irait télécharger. On le lance alors explicitement plutôt que de
    tenter un téléchargement : aucun réseau n'est requis pour ce smoke.
    """
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    candidates = [root / "chromium", *sorted(root.glob("chromium-*/chrome-linux/chrome"), reverse=True)]
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _launch(pw: Any) -> Any:
    """Lance Chromium ; saute le test avec son motif si aucun binaire n'est utilisable (NOT_RUN)."""
    attempts: list[str] = []
    # Sans bac à sable : le conteneur n'accorde pas les capacités de namespaces utilisateur.
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    executable = _chromium_executable()
    for path in (executable, None):
        try:
            return pw.chromium.launch(args=args, **({"executable_path": path} if path else {}))
        except Exception as exc:
            attempts.append(f"{path or 'révision Playwright par défaut'} : {exc}")
    pytest.skip("Chromium indisponible : " + " | ".join(attempts))


def test_interface_loads_without_js_errors_and_shows_mode(server: tuple[str, str]) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright absent : smoke navigateur NOT_RUN")
    base, key = server
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = _launch(pw)
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"{base}/?key={key}", wait_until="networkidle")

        # Le mode est visible EN PERMANENCE, et le bandeau de données synthétiques est affiché.
        page.wait_for_function(
            "() => document.getElementById('t-run-mode').textContent.trim() !== '…'", timeout=10000
        )
        assert page.inner_text("#t-run-mode").strip() == "PAPER"
        assert page.get_attribute("#j-run-mode", "data-mode") == "PAPER"
        assert page.is_visible("#bandeau-synthetique")

        # Les cinq onglets existent et la page Décisions se rend avec ses lignes.
        for nav in ("nav-marche", "nav-decisions", "nav-recherche", "nav-jev", "nav-risque"):
            assert page.is_visible(f"#{nav}")
        page.click("#nav-decisions")
        page.wait_for_selector("#dec-liste table, #dec-liste .vide", timeout=10000)
        assert "NO_TRADE" in page.inner_text("#dec-liste")
        assert "EDGE_BELOW_COSTS" in page.inner_text("#dec-rejets")

        # Une métrique inconnue s'écrit « Non disponible », jamais zéro.
        page.click("#nav-jev")
        page.wait_for_selector("#jev-tuiles .tuile", timeout=10000)
        assert "Non disponible" in page.inner_text("#page-jev")

        # La page Risque montre l'état et exige une confirmation contextuelle.
        page.click("#nav-risque")
        page.wait_for_selector("#risq-etat .t-ligne", timeout=10000)
        assert "NONE" in page.inner_text("#risq-etat")
        assert "DATA_STALE" in page.inner_text("#risq-incidents")
        page.click("#b-flatten")
        page.wait_for_selector(".voile-confirm", timeout=5000)
        boite = page.inner_text(".boite-confirm")
        assert "smoke-fixture" in boite and "PAPER" in boite  # compte ET mode sous les yeux
        page.click(".boite-confirm [data-non]")
        assert not page.is_visible(".voile-confirm")

        # La bascule de langue re-rend la page sans erreur.
        page.click("#nav-marche")
        page.click("#nav-langue button[data-langue='en']")
        page.wait_for_timeout(300)
        assert page.inner_text("#nav-decisions").strip() == "Decisions"
        page.click("#nav-langue button[data-langue='sq']")
        page.wait_for_timeout(300)
        assert page.inner_text("#nav-decisions").strip() == "Vendimet"
        browser.close()
    assert errors == [], f"erreurs JavaScript : {errors}"
