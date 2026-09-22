"""The live decision path, offline: synthetic panel, trained bundle, paper broker."""

import asyncio

import numpy as np
import pytest

from hermes.data.synthetic import make_synthetic_panel
from hermes.execution.broker import PaperBroker
from hermes.live.engine import LiveEngine
from hermes.live.state import StateStore
from hermes.research.dataset import build_dataset
from hermes.research.run import train_final


@pytest.mark.slow
def test_live_decisions_on_paper(cfg_small, tmp_path):
    panel = make_synthetic_panel(n_assets=12, n_bars=24 * 150, seed=21)
    ds = build_dataset(panel.iloc(slice(0, 24 * 120)), cfg_small)
    bundle = train_final(ds, cfg_small, promoted=False, evaluation={})
    bundle.prior_ic = 0.03  # pretend validation found an edge, to exercise sizing
    bundle.meta["cost_scale"] = 0.3  # and a persistent signal (costs amortised over ~3 horizons)
    store = StateStore(tmp_path / "state")
    broker = PaperBroker(tmp_path / "acc.json", 10_000, 0.0002, 0.0005)
    eng = LiveEngine(cfg_small, bundle, None, broker, store, mode="paper")

    async def cycle(t):
        window = panel.iloc(slice(t - 24 * 100, t))
        broker.set_prices(window["close"].iloc[-1].dropna().to_dict())
        pos = await broker.positions()
        eq = await broker.equity()
        d = eng.decide(window, {s: p.notional for s, p in pos.items()}, eq)
        await broker.rebalance(d.targets)
        return d, await broker.equity()

    decisions = []
    for t in range(24 * 120, 24 * 120 + 30):
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
    panel = make_synthetic_panel(n_assets=10, n_bars=24 * 110, seed=22)
    ds = build_dataset(panel.iloc(slice(0, 24 * 100)), cfg_small)
    bundle = train_final(ds, cfg_small, promoted=False, evaluation={})
    bundle.prior_ic = 0.05
    eng = LiveEngine(
        cfg_small, bundle, None, PaperBroker(tmp_path / "a.json", 1e4, 0, 0), StateStore(tmp_path / "s"), mode="live"
    )
    d = eng.decide(panel.iloc(slice(24 * 10, 24 * 110)), {}, 10_000.0)
    assert all(v == 0 for v in d.targets.values())
    assert any("not promoted" in n for n in d.notes)
