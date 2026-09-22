import numpy as np
import pandas as pd
import pytest

from hermes.backtest.engine import SignalBundle, run_backtest
from hermes.config import RiskConfig, load_config
from hermes.data.universe import universe_mask
from hermes.features.library import build_features
from hermes.risk.overlay import RiskOverlay


@pytest.fixture(scope="module")
def setup(small_panel):
    cfg = load_config(None, **{"data.universe.top_n": 10, "data.universe.min_history_days": 5})
    mask = universe_mask(small_panel, cfg.data.universe, 24)
    feats = build_features(small_panel, mask, cfg.features, 24)
    return cfg, mask, feats


def test_no_signal_no_trade(small_panel, setup):
    cfg, mask, feats = setup
    score = pd.DataFrame(np.nan, index=mask.index, columns=mask.columns)
    sig = SignalBundle(score, pd.Series(0.0, index=mask.index))
    bt = run_backtest(small_panel, mask, feats.aux, sig, cfg, start=mask.index[24 * 30])
    assert bt.stats["turnover"].sum() == 0
    assert np.allclose(bt.returns, 0)


def test_perfect_foresight_is_profitable_and_costs_charged(small_panel, setup):
    cfg, mask, feats = setup
    fwd = (small_panel["close"].shift(-8) / small_panel["close"] - 1).where(mask)
    sig = SignalBundle(fwd, pd.Series(0.05, index=mask.index))
    bt = run_backtest(small_panel, mask, feats.aux, sig, cfg, start=mask.index[24 * 30], end=mask.index[-10])
    s = bt.summary(cfg.bars_per_year)
    assert s["sharpe_daily"] > 3
    assert s["fees_annual"] > 0 and s["turnover_annual"] > 0
    # Accounting identity: net return = gross P&L + funding - costs (per bar, first order).
    st = bt.stats
    approx = st["gross_pnl"] + st["funding"] - st["fees"] - st["spread"] - st["impact"]
    assert np.allclose(approx, bt.returns, atol=1e-6)


def test_drawdown_budget_and_halt(tmp_path):
    cfg = RiskConfig(drawdown_soft=0.1, drawdown_hard=0.2, kill_switch_file=tmp_path / "KILL")
    ov = RiskOverlay(cfg)
    t = pd.Timestamp("2024-01-01", tz="UTC")
    ov.observe(t, 100.0)
    assert ov.budget(95.0) == 1.0
    assert 0 < ov.budget(85.0) < 1
    ov.observe(t, 79.0)
    assert ov.state.halted and ov.budget(79.0) == 0.0


def test_daily_loss_reduce_only(tmp_path):
    cfg = RiskConfig(daily_loss_limit=0.03, kill_switch_file=tmp_path / "KILL")
    ov = RiskOverlay(cfg)
    t = pd.Timestamp("2024-01-01 00:00", tz="UTC")
    ov.observe(t, 100.0)
    ov.observe(t + pd.Timedelta(hours=5), 96.0)
    cur = np.array([0.1, -0.1, 0.0])
    tgt = np.array([0.2, 0.05, 0.1])
    w, info = ov.apply(t, 96.0, tgt, cur, np.eye(3) * 1e-6, 24)
    assert info.get("reduce_only") == 1.0
    assert np.allclose(w, [0.1, 0.0, 0.0])
    # Next UTC day, the breaker resets.
    ov.observe(t + pd.Timedelta(days=1), 96.0)
    assert not ov.reduce_only(96.0)


def test_kill_switch(tmp_path):
    cfg = RiskConfig(kill_switch_file=tmp_path / "KILL")
    ov = RiskOverlay(cfg)
    (tmp_path / "KILL").touch()
    t = pd.Timestamp("2024-01-01", tz="UTC")
    ov.observe(t, 100.0)
    w, _ = ov.apply(t, 100.0, np.array([0.1, -0.1]), np.zeros(2), np.eye(2) * 1e-6, 24)
    assert np.allclose(w, 0) and ov.state.halted
