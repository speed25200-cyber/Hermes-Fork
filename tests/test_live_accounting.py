"""Live observations, missed cycles and delayed settlements, without exchange access."""

import asyncio
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from hermes.data.live_feed import BinanceLiveFeed
from hermes.data.panel import Panel
from hermes.data.synthetic import make_synthetic_panel
from hermes.data.universe import universe_mask
from hermes.execution.broker import PaperBroker
from hermes.features.library import build_features
from hermes.live.engine import LiveEngine
from hermes.live.state import StateStore


def _engine(cfg, tmp_path, bundle=None):
    broker = PaperBroker(tmp_path / "paper.json", 10_000, 0, 0, half_spread=0, slippage=0)
    store = StateStore(tmp_path / "state")
    return LiveEngine(cfg, bundle or SimpleNamespace(), None, broker, store, "paper")


def _panel(close, low=None, funding=None):
    ix = pd.date_range("2026-09-01", periods=len(close), freq="30min", tz="UTC")
    c = pd.DataFrame({"BTCUSDT": close}, index=ix, dtype=float)
    fields = {name: c.copy() for name in ("open", "high", "low", "close")}
    if low is not None:
        fields["low"]["BTCUSDT"] = low
    for name in ("volume", "quote_volume", "trades", "taker_buy_quote"):
        fields[name] = c * 0 + 1
    fields["funding_rate"] = pd.DataFrame({"BTCUSDT": funding or [np.nan] * len(c)}, index=ix)
    return Panel(fields, bar="30m")


def _open(engine, dollars=1_000):
    engine.broker.set_prices({"BTCUSDT": 100})
    asyncio.run(engine.broker.rebalance({"BTCUSDT": dollars}))
    asyncio.run(engine.broker.protect({"BTCUSDT": 0.10}))


def test_missed_cycle_replays_stop_before_recovery_and_later_funding(cfg_small, tmp_path):
    engine = _engine(cfg_small, tmp_path)
    _open(engine)
    panel = _panel([100, 85, 105], low=[100, 80, 100], funding=[np.nan, np.nan, 0.01])
    engine.store.put("paper_market_until", panel.index[0].isoformat())
    fills = engine._advance_paper_account(panel)
    assert len(fills) == 1
    assert not engine.broker.qty
    assert engine.broker.funding_paid == 0
    assert all(f.ts == (panel.index[1] + panel.bar_delta).timestamp() for f in fills)
    assert engine._advance_paper_account(panel) == []


def test_late_funding_uses_settlement_exposure_after_close_and_restart(cfg_small, tmp_path):
    engine = _engine(cfg_small, tmp_path)
    _open(engine)
    panel = _panel([100, 110], funding=[np.nan, np.nan])
    engine._advance_paper_account(panel.iloc(slice(0, 1)))
    asyncio.run(engine.broker.rebalance({}))
    engine.store.close()
    engine = _engine(cfg_small, tmp_path)
    revised = panel.with_fields({"funding_rate": pd.DataFrame({"BTCUSDT": [0.01, np.nan]}, index=panel.index)})
    engine._advance_paper_account(revised)
    assert not engine.broker.qty
    assert engine.broker.funding_paid == pytest.approx(10.0)
    engine._advance_paper_account(revised)
    assert engine.broker.funding_paid == pytest.approx(10.0)
    corrected = revised.with_fields({"funding_rate": revised["funding_rate"] * 0.5})
    engine._advance_paper_account(corrected)
    assert engine.broker.funding_paid == pytest.approx(5.0)
    engine._advance_paper_account(revised)
    assert engine.broker.funding_paid == pytest.approx(10.0)


def test_each_skipped_bar_funding_uses_its_own_mark(cfg_small, tmp_path):
    engine = _engine(cfg_small, tmp_path)
    _open(engine)
    panel = _panel([100, 110, 120], funding=[np.nan, 0.01, 0.02])
    engine.store.put("paper_market_until", panel.index[0].isoformat())
    engine._advance_paper_account(panel)
    assert engine.broker.funding_paid == pytest.approx(11.0 + 24.0)


def test_delayed_quote_recovers_funding_from_historical_quantity(cfg_small, tmp_path):
    engine = _engine(cfg_small, tmp_path)
    _open(engine)
    panel = _panel([100], funding=[0.01])
    missing = panel.with_fields({"observed_close": panel["close"] * 0})
    engine._advance_paper_account(missing)
    assert engine.broker.funding_paid == 0
    asyncio.run(engine.broker.rebalance({}))
    recovered = panel.with_fields({"observed_close": panel["close"] * 0 + 1, "close": panel["close"] * 1.1})
    engine._advance_paper_account(recovered)
    assert not engine.broker.qty
    assert engine.broker.funding_paid == pytest.approx(11.0)


def test_funding_retry_after_broker_save_is_not_charged_twice(cfg_small, tmp_path, monkeypatch):
    engine = _engine(cfg_small, tmp_path)
    _open(engine)
    panel = _panel([100])
    engine._advance_paper_account(panel)
    published = panel.with_fields({"funding_rate": panel["close"] * 0 + 0.01})
    original_put = engine.store.put

    def interrupt_after_charge(key, value):
        if key == "paper_funding_exposures" and engine.broker.funding_paid:
            raise RuntimeError("process interrupted after broker saved cash")
        original_put(key, value)

    monkeypatch.setattr(engine.store, "put", interrupt_after_charge)
    with pytest.raises(RuntimeError, match="process interrupted"):
        engine._accrue_paper_funding(published)
    assert engine.broker.funding_paid == pytest.approx(10.0)
    engine.store.close()
    restarted = _engine(cfg_small, tmp_path)
    # The provider changed its value while the process was down: finish the pending .01 before adding .01.
    revised = published.with_fields({"funding_rate": published["funding_rate"] * 2})
    restarted._accrue_paper_funding(revised)
    assert restarted.broker.funding_paid == pytest.approx(20.0)
    restarted._accrue_paper_funding(revised)
    assert restarted.broker.funding_paid == pytest.approx(20.0)


def test_interrupted_gap_replay_keeps_earlier_funding_exposure(cfg_small, tmp_path, monkeypatch):
    engine = _engine(cfg_small, tmp_path)
    _open(engine)
    panel = _panel([100, 110, 85, 105], low=[100, 100, 80, 100], funding=[np.nan, 0.01, 0.02, 0.03])
    engine._advance_paper_account(panel.iloc(slice(0, 1)))
    check = engine.broker.check_stops

    def interrupt_after_stop(high, low, open_):
        fills = check(high, low, open_)
        if fills:
            raise RuntimeError("process interrupted after stop saved")
        return fills

    monkeypatch.setattr(engine.broker, "check_stops", interrupt_after_stop)
    with pytest.raises(RuntimeError, match="after stop saved"):
        engine._advance_paper_account(panel)
    engine.store.close()
    restarted = _engine(cfg_small, tmp_path)
    restarted._advance_paper_account(panel)
    assert not restarted.broker.qty
    assert restarted.broker.funding_paid == pytest.approx(11.0)


def test_live_feed_preserves_observed_prices_before_gap_fill(monkeypatch):
    panel = _panel([100, 101])
    feed = BinanceLiveFeed(bar="30m")
    frame = pd.DataFrame({k: v["BTCUSDT"] for k, v in panel.fields.items()})
    feed.frames = {"BTCUSDT": frame, "ETHUSDT": frame.iloc[:1]}

    async def no_refresh(_symbol):
        pass

    monkeypatch.setattr(feed, "_refresh_symbol", no_refresh)
    result = asyncio.run(feed.update(["BTCUSDT", "ETHUSDT"]))
    asyncio.run(feed.close())
    assert result["close"].iloc[-1]["ETHUSDT"] == 100
    assert result["observed_close"].iloc[-1].to_dict() == {"BTCUSDT": 1.0, "ETHUSDT": 0.0}


def test_unobserved_live_quote_never_opens_or_changes_position(cfg_small, tmp_path):
    panel = make_synthetic_panel(n_assets=12, n_bars=96 * 45, bar="15m", seed=21)
    mask = universe_mask(panel, cfg_small.data.universe)
    features = build_features(panel, mask, cfg_small.features)
    bundle = SimpleNamespace(
        feature_names=features.names,
        score=lambda x, _groups: np.arange(len(x), dtype=float),
        prior_ic=0.05,
        meta={},
        market=None,
    )
    observed = panel["close"].notna().astype(float)
    symbol = mask.iloc[-1][mask.iloc[-1]].index[0]
    observed.loc[panel.index[-1], symbol] = 0
    panel = panel.with_fields({"observed_close": observed})
    engine = _engine(cfg_small, tmp_path, bundle)
    flat = engine.decide(panel, {}, 10_000)
    assert symbol not in flat.targets and symbol not in flat.scores
    assert flat.risk["missing_quotes"] == (observed.iloc[-1] == 0).sum()
    held = engine.decide(panel, {symbol: 500}, 10_000)
    assert symbol in held.hold and symbol not in held.targets
    assert held.risk["reduce_only"] == 1
    assert all(value == 0 for value in held.targets.values())
