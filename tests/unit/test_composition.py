"""Câblage des processus : ce qui doit refuser de démarrer, et ce qui doit décider (§52, §55, §62).

Ces tests ne mesurent aucune performance. Ils vérifient que la composition tient ses trois promesses :
un rôle non autorisé ne reçoit jamais d'identifiants d'échange, LIVE ne démarre pas sans autorisation,
et une boucle câblée produit une décision journalisée — NO_TRADE comprise.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from okxq.config import load_config
from okxq.config.modes import Mode
from okxq.domain.clocks import SimulatedClock
from okxq.domain.errors import ConfigError
from okxq.persistence.db import make_engine, make_session_factory
from okxq.persistence.models import Base, Decision
from okxq.runtime.composition import (
    OKX_CREDENTIAL_VARS,
    ROLES,
    BookCostComponents,
    NoModelPredictor,
    ShadowGateway,
    assert_credentials_separation,
    assert_mode_allows_process,
    assert_role,
    build_decision_loop,
    build_runtime,
    build_startup_sequence,
    portfolio_limits_from_config,
    run_process,
    stable_version,
)

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def cfg():
    return load_config(ROOT / "tests" / "fixtures" / "configs" / "smoke.fixture.yaml")


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


# --- séparation des secrets ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [r for r in ROLES if r not in ("gateway", "all")])
def test_a_role_that_must_never_hold_exchange_keys_refuses_to_start(cfg, role, monkeypatch) -> None:
    """Un secret présent dans un processus est lisible par tout ce qui y tourne : on s'arrête."""
    monkeypatch.setenv("OKX_API_KEY", "cle-injectee-par-erreur")
    with pytest.raises(ConfigError) as err:
        assert_credentials_separation(cfg, role)
    assert "OKX_API_KEY" in str(err.value.context.get("variables", ""))


def test_the_gateway_role_may_hold_exchange_keys(cfg, monkeypatch) -> None:
    for name in OKX_CREDENTIAL_VARS:
        monkeypatch.setenv(name, "valeur-de-test")
    assert_credentials_separation(cfg, "gateway") is None


def test_a_clean_environment_passes_for_every_role(cfg, monkeypatch) -> None:
    for name in OKX_CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    for role in ROLES:
        assert_credentials_separation(cfg, role) is None


def test_an_unknown_role_is_refused(cfg) -> None:
    with pytest.raises(ConfigError):
        assert_role("trader-fantome")


# --- garde LIVE ---------------------------------------------------------------------------------------


def test_live_refuses_to_start_when_it_was_never_authorized(cfg) -> None:
    """Le mode LIVE ne démarre pas parce qu'on l'a demandé, mais parce qu'il a été autorisé."""
    # `cfg.mode` est dérivé de `cfg.project.mode` : c'est là qu'il faut le poser, sinon le test
    # vérifierait une copie inchangée et passerait pour de mauvaises raisons.
    live = cfg.model_copy(
        update={"project": cfg.project.model_copy(update={"mode": Mode.LIVE, "live_enabled": False})}
    )
    assert live.mode is Mode.LIVE
    with pytest.raises(ConfigError) as err:
        assert_mode_allows_process(live)
    assert "LIVE" in str(err.value)


def test_paper_is_allowed_without_any_authorization(cfg) -> None:
    assert cfg.mode is Mode.PAPER
    assert assert_mode_allows_process(cfg) is None


# --- versions --------------------------------------------------------------------------------------


def test_a_version_changes_with_the_state_and_only_with_it() -> None:
    """Une version figée validerait une approbation sur un état périmé ; une version instable
    rejetterait des ordres valides. Les deux sont des défauts, et ce test les exclut."""
    a = stable_version("eq", "1000.00", T0.isoformat())
    assert a == stable_version("eq", "1000.00", T0.isoformat())
    assert a != stable_version("eq", "1000.01", T0.isoformat())
    assert a != stable_version("eq", "1000.00", (T0 + timedelta(seconds=1)).isoformat())


def test_portfolio_limits_come_from_the_same_values_as_the_risk_engine(cfg) -> None:
    limits = portfolio_limits_from_config(cfg)
    assert limits.gross_limit == cfg.risk.max_gross_equity_multiple
    assert limits.net_limit == cfg.risk.max_abs_net_equity_multiple
    assert limits.turnover_limit == cfg.execution.max_turnover_equity_fraction_per_decision
    # La version des contraintes lie la cible aux limites qui l'ont produite.
    assert limits.constraints_version.startswith("cons_")


# --- gateway SHADOW ---------------------------------------------------------------------------------


def test_the_shadow_gateway_cannot_send_anything() -> None:
    """SHADOW mesure des décisions AVANT d'en connaître le résultat : un envoi détruirait la mesure."""
    from okxq.domain.events import SubmissionOutcome
    from tests.unit.test_composition_helpers import approved_order

    gateway = ShadowGateway(clock=SimulatedClock(T0))
    result = asyncio.run(gateway.submit(approved_order()))
    assert result.outcome is SubmissionOutcome.NOT_SENT
    assert result.error_code == "SHADOW"
    report = asyncio.run(gateway.reconcile())
    assert report.ok and report.orders_checked == 0


# --- prédicteur par défaut ---------------------------------------------------------------------------


def test_without_a_validated_model_there_is_no_forecast_at_all() -> None:
    """Une prévision « neutre » à zéro se propagerait dans l'optimiseur comme une opinion."""
    from okxq.domain.reasons import ReasonCode

    predictor = NoModelPredictor()
    assert predictor.reason is ReasonCode.MODEL_UNAVAILABLE
    assert asyncio.run(predictor.predict(snapshot=None)) == []  # type: ignore[arg-type]


# --- assemblage et décision ---------------------------------------------------------------------------


def test_the_runtime_assembles_without_touching_the_network(cfg, factory) -> None:
    rt = build_runtime(cfg, role="all", clock=SimulatedClock(T0), session_factory=factory)
    assert rt.mode_is_paper if hasattr(rt, "mode_is_paper") else rt.cfg.mode is Mode.PAPER
    # Un halt persisté doit être rechargé, jamais remis à zéro par un démarrage.
    assert rt.kill_switch.state.account_scope == cfg.account.scope
    assert rt.limits.limits_version


def test_startup_refuses_to_authorize_entries_without_market_data(cfg, factory) -> None:
    """Aucune entrée avant d'avoir des données : c'est la dernière étape, et elle échoue à vide."""
    rt = build_runtime(cfg, role="all", clock=SimulatedClock(T0), session_factory=factory)
    report = asyncio.run(build_startup_sequence(rt).run())
    assert not report.entries_authorized
    steps = {s["step"]: s for s in report.steps}
    assert steps["validate_data"]["ok"] is False


def test_a_boundary_without_data_produces_a_journaled_no_trade(cfg, factory) -> None:
    """NO_TRADE est une décision valide, et elle doit être ÉCRITE : une décision non écrite n'est
    pas auditable, et « pourquoi n'a-t-on rien fait ? » resterait sans réponse."""
    rt = build_runtime(cfg, role="all", clock=SimulatedClock(T0), session_factory=factory)
    rt.gateway = ShadowGateway(clock=rt.clock)
    loop = build_decision_loop(rt, predictor=NoModelPredictor())
    record = asyncio.run(loop.run_once(T0, T0 + timedelta(seconds=3)))
    assert record.outcome == "NO_TRADE"
    # Sans donnée de marché, la porte opérationnelle bloque : le motif est explicite.
    assert "DATA_STALE" in record.reason_codes
    with factory() as session:
        rows = list(session.query(Decision).all())
    assert len(rows) == 1
    assert rows[0].outcome == "NO_TRADE"
    assert "DATA_STALE" in rows[0].reason_codes


def test_run_process_stops_cleanly_and_reports(cfg, factory) -> None:
    """``--max-minutes`` est un arrêt propre après N minutes, pas un mode dégradé."""
    report = asyncio.run(
        run_process(
            cfg,
            role="all",
            max_minutes=0.02,
            clock=SimulatedClock(T0),
            session_factory=factory,
        )
    )
    assert report["mode"] == "PAPER"
    assert report["role"] == "all"
    assert report["ok"] is True
    assert "startup" in report and "health" in report


def test_the_cost_adapter_refuses_to_estimate_without_a_two_sided_book(cfg, factory) -> None:
    """Un coût nul ferait passer n'importe quelle idée au-dessus de ses coûts."""
    from decimal import Decimal

    from okxq.domain.errors import CostModelError
    from okxq.domain.events import PositionSide
    from okxq.portfolio.costs import CostModel, FeeSchedule

    adapter = BookCostComponents(
        model=CostModel(
            cost_basis=cfg.strategy.cost_basis,
            fee_schedule=FeeSchedule(maker_rate=Decimal("0.0005"), taker_rate=Decimal("0.0005")),
        ),
        books=lambda: {},
        specs=lambda: {},
        horizon_s=60,
    )
    with pytest.raises(CostModelError):
        adapter.cost_components("BTC-USDT-SWAP", PositionSide.LONG, Decimal("1"), 60)


# --- collecte de données ------------------------------------------------------------------------------


def test_subscriptions_cover_the_book_and_every_channel_a_decision_needs(cfg) -> None:
    """Le carnet seul ne suffit pas : sans marque, funding ni volume, on ne peut ni chiffrer une
    position ni mesurer l'éligibilité d'un instrument."""
    from okxq.runtime.composition import COLLECTED_CHANNELS, subscriptions_for

    args = subscriptions_for(cfg, ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    channels = {a.channel for a in args}
    assert cfg.market_data.orderbook_channel in channels
    assert set(COLLECTED_CHANNELS) <= channels
    assert {a.inst_id for a in args} == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}


def test_the_collector_refuses_a_private_endpoint_url() -> None:
    """Le collecteur ne détient aucun identifiant : sur un canal privé il serait inutile, et son
    échec doit être immédiat plutôt que silencieux."""
    from okxq.data.ws_client import public_ws_url

    assert public_ws_url("wss://ws.okx.com:8443/ws/v5/public").endswith("/public")
    with pytest.raises(ConfigError):
        public_ws_url("wss://ws.okx.com:8443/ws/v5/private")
    with pytest.raises(ConfigError):
        public_ws_url("ws://ws.okx.com:8443/ws/v5/public")  # TLS n'est pas optionnel


def test_a_collection_failure_degrades_to_no_trade_without_stopping_the_process(
    cfg, factory, monkeypatch
) -> None:
    """Sans données, la plateforme doit continuer à tourner et décider NO_TRADE.

    Une plateforme qui s'arrête à la première coupure réseau ne peut plus ni réduire une position ni
    rapporter son état : c'est exactement quand on en a le plus besoin. C'est le cas observé quand
    l'accès sortant à OKX est refusé.
    """
    from okxq.domain.errors import ExchangeError
    from okxq.runtime import composition

    async def refuse(_cfg):
        raise ExchangeError("erreur réseau : 403 Forbidden")

    monkeypatch.setattr(composition, "discover_instruments", refuse)
    report = asyncio.run(
        composition.run_process(
            cfg, role="all", max_minutes=0.02, clock=SimulatedClock(T0), session_factory=factory
        )
    )
    assert report["ok"] is True, "le processus doit survivre à une collecte impossible"
    assert report["collector"]["ok"] is False
    assert "403" in report["collector"]["reason"]
    assert report["collector"]["instruments"] == 0
    # La santé dit FAULT sur les données de marché : l'absence est visible, pas maquillée.
    assert report["health"]["modules"]["market_data"]["status"] == "FAULT"


def test_T44_a_restart_never_resets_a_persisted_halt_or_daily_loss(cfg, factory) -> None:
    """T44 : pertes du jour et niveau de halt survivent au redémarrage.

    C'est la garantie la plus importante du Risk Engine : un processus qu'on relance ne doit pas
    repartir avec une ardoise vierge, sinon il suffirait de redémarrer pour effacer une limite
    journalière atteinte — le kill switch deviendrait décoratif.
    """
    from okxq.risk.kill_switch import HaltLevel

    premier = build_runtime(cfg, role="all", clock=SimulatedClock(T0), session_factory=factory)
    premier.kill_switch.request(HaltLevel.HARD_HALT, "perte journalière atteinte pendant le test")
    assert premier.kill_switch.level is HaltLevel.HARD_HALT

    # Nouveau runtime sur la MÊME base : c'est l'équivalent exact d'un redémarrage de processus.
    second = build_runtime(cfg, role="all", clock=SimulatedClock(T0), session_factory=factory)
    assert second.kill_switch.level is HaltLevel.HARD_HALT, "un redémarrage a effacé le halt"
    assert second.kill_switch.state.halt_reason
    # Et la séquence de démarrage refuse d'autoriser les entrées tant que le halt tient. On rend la
    # validation des données satisfaite pour isoler la cause : sinon la séquence s'arrêterait avant,
    # et le test passerait sans rien dire du halt.
    second.gateway = ShadowGateway(clock=second.clock)
    second.market.last_available_at = T0
    report = asyncio.run(build_startup_sequence(second).run())
    assert not report.entries_authorized
    steps = {s["step"]: s for s in report.steps}
    assert steps["validate_data"]["ok"] is True, "la donnée devait être considérée présente"
    assert steps["authorize_entries"]["ok"] is False
    assert "HARD_HALT" in str(steps["authorize_entries"]["detail"])
