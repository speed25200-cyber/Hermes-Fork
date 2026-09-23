"""The live decision path, offline: synthetic panel, trained bundle, paper broker."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from hermes.data.synthetic import make_synthetic_panel
from hermes.execution.broker import PaperBroker
from hermes.live.engine import LiveEngine
from hermes.live.state import StateStore
from hermes.research.dataset import build_dataset
from hermes.research.run import train_final


@pytest.mark.slow
def test_live_decisions_on_paper(cfg_small, tmp_path):
    panel = make_synthetic_panel(n_assets=12, n_bars=96 * 60, bar="15m", seed=21)
    ds = build_dataset(panel.iloc(slice(0, 96 * 45)), cfg_small)
    bundle = train_final(ds, cfg_small, promoted=False, evaluation={})
    bundle.prior_ic = 0.03  # pretend validation found an edge, to exercise sizing
    bundle.meta["cost_scale"] = 0.3  # and a persistent signal (costs amortised over ~3 horizons)
    store = StateStore(tmp_path / "state")
    broker = PaperBroker(tmp_path / "acc.json", 10_000, 0.0002, 0.0005)
    eng = LiveEngine(cfg_small, bundle, None, broker, store, mode="paper")

    async def cycle(t):
        window = panel.iloc(slice(t - 96 * 30, t))
        broker.set_prices(window["close"].iloc[-1].dropna().to_dict())
        pos = await broker.positions()
        eq = await broker.equity()
        d = eng.decide(window, {s: p.notional for s, p in pos.items()}, eq)
        await broker.rebalance(d.targets)
        return d, await broker.equity()

    decisions = []
    for t in range(96 * 45, 96 * 45 + 30):
        decisions.append(asyncio.run(cycle(t)))
    d0 = decisions[0][0]
    assert d0.n_members >= 5 and not d0.stale
    assert d0.targets, "a positive prior IC must produce a book"
    gross = sum(abs(v) for v in d0.targets.values()) / d0.equity
    assert gross <= cfg_small.portfolio.gross_max + 1e-9
    net_beta = abs(sum(d0.weights.values()))
    assert net_beta < 0.25
    # Consecutive decisions trade only part of the book (cost-aware no-trade region).
    turn = [
        sum(
            abs(decisions[i][0].weights.get(s, 0) - decisions[i - 1][0].weights.get(s, 0))
            for s in set(decisions[i][0].weights) | set(decisions[i - 1][0].weights)
        )
        for i in range(1, 30)
    ]
    assert np.median(turn) < gross
    assert not store.score_history(panel.index[0]).empty


@pytest.mark.slow
def test_live_refuses_unpromoted_bundle_in_live_mode(cfg_small, tmp_path):
    panel = make_synthetic_panel(n_assets=10, n_bars=96 * 50, bar="15m", seed=22)
    ds = build_dataset(panel.iloc(slice(0, 96 * 40)), cfg_small)
    bundle = train_final(ds, cfg_small, promoted=False, evaluation={})
    bundle.prior_ic = 0.05
    eng = LiveEngine(
        cfg_small, bundle, None, PaperBroker(tmp_path / "a.json", 1e4, 0, 0), StateStore(tmp_path / "s"), mode="live"
    )
    d = eng.decide(panel.iloc(slice(96 * 10, 96 * 50)), {}, 10_000.0)
    assert all(v == 0 for v in d.targets.values())
    assert any("not promoted" in n for n in d.notes)


class FakeFeed:
    """Serves successive windows of a synthetic panel like the live feed (closed bars, daily history)."""

    def __init__(self, panel, t, window):
        from hermes.data.live_feed import BinanceLiveFeed
        from hermes.data.panel import Panel

        # Move the synthetic history so that bar t-1 is the last closed bar right now (fresh data).
        shift = BinanceLiveFeed.last_closed_bar(panel.bar) - panel.index[t - 1]
        self.panel = Panel({k: v.set_axis(v.index + shift) for k, v in panel.fields.items()}, bar=panel.bar)
        self.t, self.window = t, window
        self.calls = 0

    async def top_symbols(self, n):
        return self.panel.symbols[:n]

    async def update(self, symbols):
        self.calls += 1
        return self.panel.iloc(slice(self.t - self.window, self.t)).subset(symbols)

    async def daily(self, symbols):
        from hermes.data.live_feed import DailyHistory

        p = self.panel.iloc(slice(0, self.t)).subset(symbols)
        today = p.index[-1].floor("D")
        qv = p["quote_volume"].resample("1D").sum()
        close = p["close"].resample("1D").last()
        keep = qv.index < today  # closed daily bars only, as Binance returns them
        return DailyHistory(qv[keep], close[keep].notna(), close[keep])


def _engine(cfg, tmp_path, panel_bars=96 * 45, mode="paper", frac=None):
    panel = make_synthetic_panel(n_assets=12, n_bars=96 * 60, bar="15m", seed=21)
    ds = build_dataset(panel.iloc(slice(0, panel_bars)), cfg)
    bundle = train_final(ds, cfg, promoted=True, evaluation={})
    bundle.prior_ic = 0.03
    bundle.meta["cost_scale"] = 0.3
    if frac is not None:
        cfg = cfg.model_copy(update={"live": cfg.live.model_copy(update={"capital_fraction": frac})})
    broker = PaperBroker(tmp_path / "acc.json", 10_000, 0.0002, 0.0005)
    store = StateStore(tmp_path / "state")
    return panel, bundle, broker, store, cfg


@pytest.mark.slow
def test_step_runs_a_full_cycle_with_daily_history_ending_yesterday(cfg_small, tmp_path):
    panel, bundle, broker, store, cfg = _engine(cfg_small, tmp_path)
    t = 96 * 45 + 40  # 10:00 on the day: the daily history has no row for today
    feed = FakeFeed(panel, t, 96 * 30)
    eng = LiveEngine(cfg, bundle, feed, broker, store, mode="paper")
    d = asyncio.run(eng.step())
    assert d is not None and d.n_members >= 5, "today's bars must map to a universe computed from yesterday"
    assert d.targets and "cost_scale" in d.risk
    assert (tmp_path / "state" / "status.json").exists()
    assert not store.equity_curve().empty
    # The realised IC is persisted as it becomes known (months of memory beyond the base-bar window).
    for k in range(1, 12):
        feed.t = t + k
        asyncio.run(eng.step())
    assert not store.get_series("ic", panel.index[0]).empty


@pytest.mark.slow
def test_capital_fraction_units_round_trip(cfg_small, tmp_path):
    panel, bundle, broker, store, cfg = _engine(cfg_small, tmp_path, frac=0.25)
    eng = LiveEngine(cfg, bundle, None, broker, store, mode="paper")
    t = 96 * 45 + 40
    window = panel.iloc(slice(t - 96 * 30, t))
    d1 = eng.decide(window, {}, 10_000.0)
    assert d1.targets
    gross1 = sum(abs(v) for v in d1.targets.values())
    assert gross1 <= 0.25 * 10_000 * cfg.portfolio.gross_max + 1e-6
    # Holding exactly the previous targets under 'reductions only' (stale data) must keep them, not cut 75%.
    later = window.index[-1] + pd.Timedelta(hours=2)
    d2 = eng.decide(window, dict(d1.targets), 10_000.0, now=later)
    assert d2.stale
    for s, v in d1.targets.items():
        assert abs(d2.targets.get(s, 0.0)) <= abs(v) + 1e-9
    kept = sum(abs(d2.targets.get(s, 0.0)) for s in d1.targets)
    assert kept > 0.5 * gross1


@pytest.mark.slow
def test_kill_switch_flattens_without_touching_the_feed(cfg_small, tmp_path):
    panel, bundle, broker, store, cfg = _engine(cfg_small, tmp_path)
    kill = tmp_path / "KILL"
    cfg = cfg.model_copy(update={"risk": cfg.risk.model_copy(update={"kill_switch_file": str(kill)})})
    feed = FakeFeed(panel, 96 * 45 + 40, 96 * 30)
    eng = LiveEngine(cfg, bundle, feed, broker, store, mode="paper")
    asyncio.run(eng.step())
    assert asyncio.run(broker.positions())
    kill.write_text("stop")
    calls = feed.calls
    assert asyncio.run(eng.step()) is None
    assert feed.calls == calls, "the kill switch must act before (and without) market data"
    assert not asyncio.run(broker.positions())
    assert eng.overlay.state.halted
