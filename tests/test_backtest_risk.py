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
    cfg = load_config(None, **{"data.bar": "15m", "data.universe.top_n": 10, "data.universe.min_history_days": 3})
    mask = universe_mask(small_panel, cfg.data.universe)
    feats = build_features(small_panel, mask, cfg.features)
    return cfg, mask, feats


def test_no_signal_no_trade(small_panel, setup):
    cfg, mask, feats = setup
    score = pd.DataFrame(np.nan, index=mask.index, columns=mask.columns)
    sig = SignalBundle(score, pd.Series(0.0, index=mask.index))
    bt = run_backtest(small_panel, mask, feats.aux, sig, cfg, start=mask.index[96 * 20])
    assert bt.stats["turnover"].sum() == 0
    assert np.allclose(bt.returns, 0)


def test_perfect_foresight_is_profitable_and_costs_charged(small_panel, setup):
    cfg, mask, feats = setup
    fwd = (small_panel["close"].shift(-4) / small_panel["close"] - 1).where(mask)
    sig = SignalBundle(fwd, pd.Series(0.05, index=mask.index))
    bt = run_backtest(small_panel, mask, feats.aux, sig, cfg, start=mask.index[96 * 20], end=mask.index[-10])
    s = bt.summary(cfg.bars_per_year)
    assert s["sharpe_daily"] > 3
    assert s["fees_annual"] > 0 and s["turnover_annual"] > 0
    # Accounting identity: net return = gross P&L + funding - costs (per bar, first order).
    st = bt.stats
    approx = st["gross_pnl"] + st["funding"] - st["fees"] - st["spread"] - st["impact"]
    assert np.allclose(approx, bt.returns, atol=1e-6)
    assert np.allclose(st["pnl_long"] + st["pnl_short"], st["gross_pnl"], atol=1e-12)  # legs add up


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


def test_daily_loss_counts_the_first_bar_of_the_day(tmp_path):
    cfg = RiskConfig(daily_loss_limit=0.03, kill_switch_file=tmp_path / "KILL")
    ov = RiskOverlay(cfg)
    t = pd.Timestamp("2024-01-01 23:45", tz="UTC")
    ov.observe(t, 100.0)  # equity at the close of the day's last bar
    ov.observe(t + pd.Timedelta(minutes=15), 96.0)  # first bar of the next day loses 4%
    assert ov.state.day_start_equity == 100.0 and ov.reduce_only(96.0)


def test_cost_stress_scales_what_trades_pay_only(small_panel, setup):
    cfg, mask, feats = setup
    fwd = (small_panel["close"].shift(-4) / small_panel["close"] - 1).where(mask)
    sig = SignalBundle(fwd, pd.Series(0.05, index=mask.index))
    kw = {"start": mask.index[96 * 20], "end": mask.index[-10]}
    base = run_backtest(small_panel, mask, feats.aux, sig, cfg, **kw)
    stress = run_backtest(small_panel, mask, feats.aux, sig, cfg, cost_multiplier=2.0, **kw)
    # Same first book (the optimiser still sees the usual costs; later books differ only through equity) ...
    assert np.isclose(base.stats["turnover"].iloc[0], stress.stats["turnover"].iloc[0], rtol=1e-6)
    # ... and every trade pays twice as much.
    assert np.isclose(stress.stats["fees"].iloc[0], 2 * base.stats["fees"].iloc[0], rtol=1e-6)
    assert np.isclose(stress.stats["impact"].iloc[0], 2 * base.stats["impact"].iloc[0], rtol=1e-6)
    ratio = stress.stats["fees"].iloc[: 96 * 3].sum() / base.stats["fees"].iloc[: 96 * 3].sum()
    assert 1.7 < ratio < 2.3


def test_rebalancing_follows_the_clock_aligned_grid(small_panel, setup):
    from hermes.backtest.engine import is_rebalance_bar

    cfg, mask, feats = setup
    cfg4 = cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(update={"rebalance_every": 4}),
            "risk": cfg.risk.model_copy(update={"stop_loss_daily_sigmas": 0.0}),  # stops may exit between decisions
        }
    )
    fwd = (small_panel["close"].shift(-4) / small_panel["close"] - 1).where(mask)
    sig = SignalBundle(fwd, pd.Series(0.05, index=mask.index))
    bt = run_backtest(small_panel, mask, feats.aux, sig, cfg4, start=mask.index[96 * 20 + 1], end=mask.index[-10])
    grid = is_rebalance_bar(bt.stats.index, small_panel.bar_delta, 4)
    assert bt.stats["turnover"][~grid].eq(0).all() and bt.stats["turnover"][grid].gt(0).any()
    # Same grid whatever the backtest's first bar (research and live decide on the same bars).
    assert (bt.stats.index[grid].minute % 60).isin([0]).all()


def test_exhausted_drawdown_cushion_is_reported(tmp_path):
    cfg = RiskConfig(drawdown_soft=0.1, drawdown_hard=0.2, kill_switch_file=tmp_path / "KILL")
    ov = RiskOverlay(cfg)
    t = pd.Timestamp("2024-01-01", tz="UTC")
    ov.observe(t, 100.0)
    assert not ov.cushion_exhausted(85.0)  # 15% drawdown: half the budget left
    assert ov.cushion_exhausted(80.2) and not ov.state.halted  # nearly idle, yet never formally halted


def test_catastrophe_stops_cap_a_crash_like_the_exchange_would(small_panel, setup):
    from hermes.data.panel import Panel

    cfg, mask, feats = setup
    members = [c for c in mask.columns[mask.iloc[96 * 25].to_numpy()] if c not in ("BTCUSDT", "ETHUSDT")]
    sym = members[0]  # a member at the crash date with its own (residual) risk
    T0 = 96 * 26
    fields = {k: v.copy() for k, v in small_panel.fields.items()}
    f = pd.Series(1.0, index=fields["close"].index)
    f.iloc[T0 : T0 + 5] = 0.9 ** np.arange(1, 6)
    f.iloc[T0 + 5 :] = 0.9**5
    prev = f.shift(1).fillna(1.0)
    c = fields["close"][sym].copy()
    fields["close"][sym] = c * f
    fields["open"][sym] = c.shift(1).fillna(c) * prev  # no gap: each bar opens at the previous close
    fields["high"][sym] = np.maximum(fields["open"][sym], fields["close"][sym])
    fields["low"][sym] = np.minimum(fields["open"][sym], fields["close"][sym])
    panel = Panel(fields, bar=small_panel.bar)
    score = pd.DataFrame(0.0, index=mask.index, columns=mask.columns).where(mask)
    score[sym] = score[sym] + 3.0  # always the top long
    sig = SignalBundle(score, pd.Series(0.05, index=mask.index))
    kw = {"start": mask.index[96 * 21], "end": mask.index[T0 + 10]}
    runs = {}
    for k in (4.0, 0.0):
        c2 = cfg.model_copy(update={"risk": cfg.risk.model_copy(update={"stop_loss_daily_sigmas": k})})
        runs[k] = run_backtest(panel, mask, feats.aux, sig, c2, **kw)
    with_stop, without = runs[4.0], runs[0.0]
    assert with_stop.stats["stops"].sum() >= 1 and without.stats["stops"].sum() == 0
    crash = slice(T0 - 96 * 21, T0 - 96 * 21 + 6)
    assert with_stop.stats["pnl_long"].iloc[crash].sum() > without.stats["pnl_long"].iloc[crash].sum()
    st = with_stop.stats  # accounting still closes, stop exits included
    approx = st["gross_pnl"] + st["funding"] - st["fees"] - st["spread"] - st["impact"]
    assert np.allclose(approx, with_stop.returns, atol=1e-6)


def test_worst_case_stop_fill_exits_at_the_bar_extreme(small_panel, setup):
    # A flash crash wicks 40 % below the previous close inside one bar and closes only 5 % down: the default
    # fill is the stop price, the stress fill is the bar's low -- never better than the default.
    from hermes.data.panel import Panel

    cfg, mask, feats = setup
    members = [c for c in mask.columns[mask.iloc[96 * 25].to_numpy()] if c not in ("BTCUSDT", "ETHUSDT")]
    sym = members[0]
    T0 = 96 * 26
    fields = {k: v.copy() for k, v in small_panel.fields.items()}
    prev_close = fields["close"][sym].iloc[T0 - 1]
    fields["open"].loc[fields["open"].index[T0], sym] = prev_close
    fields["low"].loc[fields["low"].index[T0], sym] = prev_close * 0.6
    fields["close"].loc[fields["close"].index[T0], sym] = prev_close * 0.95
    fields["high"].loc[fields["high"].index[T0], sym] = prev_close
    panel = Panel(fields, bar=small_panel.bar)
    score = pd.DataFrame(0.0, index=mask.index, columns=mask.columns).where(mask)
    score[sym] = score[sym] + 3.0
    sig = SignalBundle(score, pd.Series(0.05, index=mask.index))
    kw = {"start": mask.index[96 * 21], "end": mask.index[T0 + 3]}
    base = run_backtest(panel, mask, feats.aux, sig, cfg, **kw)
    worst = run_backtest(panel, mask, feats.aux, sig, cfg, stop_fill="extreme", **kw)
    k = T0 - 96 * 21
    assert base.stats["stops"].iloc[k] >= 1 and worst.stats["stops"].iloc[k] >= 1
    assert worst.stats["pnl_long"].iloc[k] < base.stats["pnl_long"].iloc[k] - 1e-4
    assert np.allclose(base.returns.iloc[:k], worst.returns.iloc[:k])  # identical until the crash bar


def test_decisions_fill_at_the_next_vwap(small_panel, setup):
    # vwap_first[t+1] = close[t] * (1 + p): every decision is filled p away from its decision close.
    cfg, mask, feats = setup
    score = feats.frames["iret_120m"].where(mask) if "iret_120m" in feats.frames else None
    score = score if score is not None else next(iter(feats.frames.values())).where(mask)
    sig = SignalBundle(score, pd.Series(0.05, index=mask.index))
    kw = {"start": mask.index[96 * 20]}
    runs = {}
    for p in (0.0, 0.01):
        panel = small_panel.with_fields({"vwap_first": small_panel["close"].shift(1) * (1 + p)})
        runs[p] = run_backtest(panel, mask, feats.aux, sig, cfg, **kw)
    base = run_backtest(small_panel, mask, feats.aux, sig, cfg, **kw)
    assert np.allclose(runs[0.0].returns, base.returns)  # filling at the decision close changes nothing
    st = runs[0.01].stats
    assert st["slippage"].abs().sum() > 0 and base.stats["slippage"].abs().sum() == 0
    # Slippage of bar k comes from the trades of bar k-1: at most p times their turnover.
    assert (st["slippage"].abs().iloc[1:].to_numpy() <= 0.01 * st["turnover"].iloc[:-1].to_numpy() + 1e-9).all()
    approx = st["gross_pnl"] + st["funding"] - st["fees"] - st["spread"] - st["impact"]
    assert np.allclose(approx, runs[0.01].returns, atol=1e-6)  # slippage is part of the price P&L
    # A "VWAP" outside its bar's range is a corrupted archive row: ignored (fill at the close), never paid.
    bad = small_panel.with_fields({"vwap_first": small_panel["high"] * 1.65})
    assert np.allclose(run_backtest(bad, mask, feats.aux, sig, cfg, **kw).returns, base.returns)
