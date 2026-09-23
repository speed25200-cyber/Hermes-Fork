"""The live decision path, offline: synthetic panel, trained bundle, paper broker."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from hermes.config import with_overrides
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
    assert any("non promu" in n for n in d.notes)


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
    # Pre-registered incubation checks: raw and one-horizon-old rank ICs, and the daily regime variables.
    for name in ("ic_raw", "ic_lag", "btc_dd90", "mkt_ret30", "xs_ac1"):
        v = store.get_series(name, panel.index[0])
        assert not v.empty and np.isfinite(v.to_numpy(dtype=float)).all(), name
    assert (store.get_series("btc_dd90", panel.index[0]) <= 0).all()


@pytest.mark.slow
def test_step_with_style_free_target_and_style_neutral_book(cfg_small, tmp_path):
    # The configuration of the best research candidates: the live cycle must run end to end with it, and its
    # realised IC is measured against the same style-free target the model was trained on.
    cfg = with_overrides(cfg_small, {"labels.residualize": "style", "portfolio.style_neutral": True})
    panel, bundle, broker, store, cfg = _engine(cfg, tmp_path)
    t = 96 * 45 + 40
    feed = FakeFeed(panel, t, 96 * 30)
    eng = LiveEngine(cfg, bundle, feed, broker, store, mode="paper")
    d = asyncio.run(eng.step())
    assert d is not None and d.targets
    for k in range(1, 12):
        feed.t = t + k
        asyncio.run(eng.step())
    ic = store.get_series("ic", panel.index[0])
    assert not ic.empty and np.isfinite(ic.to_numpy(dtype=float)).all()


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


def test_restart_re_registers_cached_candidates_with_the_broker(cfg_small, tmp_path):
    class RegBroker(PaperBroker):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.registered: set[str] = set()

        def register(self, symbols):
            self.registered |= set(symbols)
            return list(symbols)

    store = StateStore(tmp_path / "s")
    store.put("candidates", ["BTCUSDT", "ETHUSDT"])
    store.put("candidates_day", pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"))  # same-day restart
    store.put("traded_symbols", ["1000PEPEUSDT"])
    broker = RegBroker(tmp_path / "a.json", 1e4, 0, 0)

    class Feed:
        async def top_symbols(self, n):
            raise AssertionError("cached for today")

    eng = LiveEngine(cfg_small, type("B", (), {"meta": {}})(), Feed(), broker, store, mode="paper")
    asyncio.run(eng.refresh_candidates([]))
    assert {"BTCUSDT", "ETHUSDT", "1000PEPEUSDT"} <= broker.registered


def test_changing_capital_fraction_keeps_the_drawdown(cfg_small, tmp_path):
    store = StateStore(tmp_path / "s")
    broker = PaperBroker(tmp_path / "a.json", 1e4, 0, 0)
    bundle = type("B", (), {"meta": {}})()
    eng = LiveEngine(cfg_small, bundle, None, broker, store, mode="paper")
    ts = pd.Timestamp("2026-01-01 10:00", tz="UTC")
    eng.overlay.observe(ts, eng.strategy_nav(10_000.0))  # frac 1: NAV = equity
    eng.overlay.observe(ts, eng.strategy_nav(9_500.0))  # 5% drawdown
    eng._save_risk_state()  # as decide() does each bar
    cfg2 = cfg_small.model_copy(update={"live": cfg_small.live.model_copy(update={"capital_fraction": 0.25})})
    eng2 = LiveEngine(cfg2, bundle, None, broker, store, mode="paper")
    nav = eng2.strategy_nav(9_500.0)
    assert nav == pytest.approx(2_375.0)
    assert eng2.overlay.drawdown(nav) == pytest.approx(0.05)  # rescaled, not a 76% drawdown and a halt
    nav2 = eng2.strategy_nav(9_405.0)  # account -1% = strategy -4%
    assert nav2 == pytest.approx(2_375.0 * 0.96)


def test_drift_warning_and_implementation_shortfall(cfg_small, tmp_path):
    from hermes.execution.broker import Fill
    from hermes.live.engine import Decision
    from hermes.models.drift import feature_profile

    r = np.random.default_rng(0)
    names = ["a", "b"]
    bundle = type("B", (), {"meta": {"feature_profile": feature_profile(r.normal(size=(20_000, 2)), names)}})()
    bundle.feature_names = names
    eng = LiveEngine(
        cfg_small, bundle, None, PaperBroker(tmp_path / "a.json", 1e4, 0, 0), StateStore(tmp_path / "s"), "paper"
    )
    ts = pd.Timestamp("2026-01-01", tz="UTC")
    d = Decision(ts=str(ts), equity=1.0, stale=False, n_members=0, ic_est=0.0)
    for k in range(40):
        x = np.column_stack([r.normal(size=30), r.normal(2.0, 1.0, size=30)])  # feature b has moved
        eng._check_drift(ts + pd.Timedelta(minutes=15 * k), x, d)
    assert d.risk["psi_drifted"] >= 1 and any("dérive" in n for n in d.notes)
    fills = [
        Fill("BTCUSDT", "buy", 1.0, 101.0, 0.0, True, notional=101.0),
        Fill("ETHUSDT", "sell", -1.0, 9.9, 0.0, False, notional=9.9),
    ]
    bps = eng._shortfall_bps(fills, pd.Series({"BTCUSDT": 100.0, "ETHUSDT": 10.0}))
    assert bps == pytest.approx(100.0)  # both paid 1% against the decision price


@pytest.mark.slow
def test_held_contract_without_daily_history_is_left_alone(cfg_small, tmp_path):
    from hermes.data.live_feed import DailyHistory

    panel, bundle, broker, store, cfg = _engine(cfg_small, tmp_path)
    feed = FakeFeed(panel, 96 * 45 + 40, 96 * 30)
    eng = LiveEngine(cfg, bundle, feed, broker, store, mode="paper")
    window = asyncio.run(feed.update(panel.symbols))
    daily = asyncio.run(feed.daily(panel.symbols))
    held = panel.symbols[0]
    broken = DailyHistory(*(x.drop(columns=held) for x in (daily.quote_volume, daily.alive, daily.close)))
    d = eng.decide(window, {held: 1_000.0}, 10_000.0, daily=broken)
    assert held in d.hold and held not in d.targets


@pytest.mark.slow
def test_live_sizes_the_restandardised_smoothed_score(cfg_small, tmp_path):
    panel, bundle, broker, store, cfg = _engine(cfg_small, tmp_path)
    cfg = cfg.model_copy(update={"portfolio": cfg.portfolio.model_copy(update={"signal_halflife": 2.0})})
    eng = LiveEngine(cfg, bundle, None, broker, store, mode="paper")
    seen = {}
    real_target = eng.constructor.target

    def spy(inp, capital):
        z = inp.score[np.isfinite(inp.score)]
        seen["std"] = float(np.std(z, ddof=1)) if len(z) > 2 else float("nan")
        return real_target(inp, capital)

    eng.constructor.target = spy
    t = 96 * 45 + 40
    for k in range(12):  # enough bars for the smoothing to shrink the raw dispersion
        eng.decide(panel.iloc(slice(t + k - 96 * 30, t + k)), {}, 10_000.0)
    assert 0.8 < seen["std"] < 1.2  # z-scored across members as in the backtest, not a shrunk average


def test_paper_trades_only_what_okx_lists(cfg_small, tmp_path):
    # The paper broker accepts any symbol; with venue okx the engine must still restrict itself to OKX's
    # crypto swaps, the universe research validated and the OKX broker would trade.
    cfg = with_overrides(cfg_small, {"data.universe.venue": "okx"})
    broker = PaperBroker(tmp_path / "acc.json", 10_000, 0.0002, 0.0005)
    eng = LiveEngine(cfg, type("B", (), {"meta": {}})(), None, broker, StateStore(tmp_path / "s"), mode="paper")
    today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    eng._venue = (today, {"BTC-USDT-SWAP", "PEPE-USDT-SWAP"})
    assert eng.tradable(["BTCUSDT", "MYXUSDT", "1000PEPEUSDT", "USDCUSDT"]) == ["BTCUSDT", "1000PEPEUSDT"]
    cfg_any = with_overrides(cfg_small, {"data.universe.venue": "any"})
    eng_any = LiveEngine(cfg_any, type("B", (), {"meta": {}})(), None, broker, StateStore(tmp_path / "t"), "paper")
    assert eng_any.tradable(["BTCUSDT", "MYXUSDT"]) == ["BTCUSDT", "MYXUSDT"]


@pytest.mark.slow
def test_regime_gate_is_the_same_in_research_and_live(cfg_small, tmp_path):
    from hermes.portfolio.alpha import regime_scale

    cfg = with_overrides(cfg_small, {"portfolio.regime_gate_drawdown": 0.01, "portfolio.regime_gate_lookback_days": 20})
    panel, bundle, broker, store, cfg = _engine(cfg, tmp_path)
    t = 96 * 45 + 40
    feed = FakeFeed(panel, t, 96 * 30)  # the feed moves the history to the present
    eng = LiveEngine(cfg, bundle, feed, broker, store, mode="paper")
    d = asyncio.run(eng.step())
    day = pd.Timestamp(d.ts).floor("D")
    # Research reads the whole panel (today's partial close included); the gate of a day only uses the day before.
    research = regime_scale(feed.panel["close"]["BTCUSDT"].iloc[:t].resample("1D").last(), 0.01, 20, 0.5)
    assert d.risk["regime_scale"] == research[day]
    gated = research[research < 1].index
    assert len(gated) and (research.loc[gated] == 0.5).all()  # the synthetic BTC does cross the threshold


@pytest.mark.slow
def test_status_details_positions_for_the_dashboard(cfg_small, tmp_path):
    import json

    panel, bundle, broker, store, cfg = _engine(cfg_small, tmp_path)
    t = 96 * 45 + 40
    feed = FakeFeed(panel, t, 96 * 30)
    eng = LiveEngine(cfg, bundle, feed, broker, store, mode="paper")
    asyncio.run(eng.step())
    st = json.loads((tmp_path / "state" / "status.json").read_text())
    det = st["positions_detail"]
    assert det and {p["symbol"] for p in det} == set(st["positions"])
    for p in det:
        side = 1 if p["side"] == "long" else -1
        assert np.sign(p["notional"]) == side and p["entry"] > 0 and p["mark"] > 0
        assert p["stop"] is not None and side * (p["mark"] - p["stop"]) > 0  # the stop is on the losing side
        assert abs(p["upnl"] - p["notional"] * (1 - p["entry"] / p["mark"])) < 0.02
        assert p["opened"] and p["weight"] is not None
    assert st["account"]["initial"] == cfg.live.paper_initial_equity and st["strategy"]["bar"] == cfg.data.bar
    first = {p["symbol"]: p["opened"] for p in det}
    feed.t = t + 1
    asyncio.run(eng.step())
    again = json.loads((tmp_path / "state" / "status.json").read_text())["positions_detail"]
    for p in again:  # a position keeps its opening time while it keeps its side
        if p["symbol"] in first:
            assert p["opened"] == first[p["symbol"]]
    kinds = {r[0] for r in store.db.execute("SELECT DISTINCT kind FROM fills")}
    assert kinds == {"trade"}


class _ExchangeBroker:
    """An exchange-like broker (not the paper one): stops fire on the exchange, outside the engine."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.mark.slow
def test_exchange_side_stops_are_recorded_in_the_history(cfg_small, tmp_path):
    import json

    from hermes.live.dashboard import reconstruct_trades

    panel, bundle, broker, store, cfg = _engine(cfg_small, tmp_path)
    ex = _ExchangeBroker(broker)
    t = 96 * 45 + 40
    feed = FakeFeed(panel, t, 96 * 30)
    eng = LiveEngine(cfg, bundle, feed, ex, store, mode="paper")
    broker.set_prices(feed.panel["close"].iloc[t - 1].dropna().to_dict())  # an exchange marks its own prices
    asyncio.run(eng.step())
    held = {p["symbol"]: p for p in json.loads((tmp_path / "state" / "status.json").read_text())["positions_detail"]}
    victim = next(iter(held))
    broker.qty.pop(victim)  # the exchange's stop closed it between two cycles
    broker.stops.pop(victim, None)
    feed.t = t + 1
    broker.set_prices(feed.panel["close"].iloc[t].dropna().to_dict())
    asyncio.run(eng.step())
    rows = [
        dict(zip(("id", "ts", "symbol", "side", "qty", "price", "fee", "maker", "notional", "kind", "px_model"), r))
        for r in store.db.execute(
            "SELECT id, ts, symbol, side, qty, price, fee, maker, notional, kind, px_model FROM fills"
        )
    ]
    stop = [r for r in rows if r["kind"] == "stop"]
    assert len(stop) == 1 and stop[0]["symbol"] == victim
    assert abs(stop[0]["px_model"] - held[victim]["stop"]) < 1e-9  # priced at the stop's trigger
    closed = [c for c in reconstruct_trades(rows)["closed"] if c["symbol"] == victim]
    assert closed and closed[-1]["exit_kind"] == "stop"
    # Reopened by this cycle's rebalance: a new position, with a new opening time.
    now = {p["symbol"]: p for p in json.loads((tmp_path / "state" / "status.json").read_text())["positions_detail"]}
    if victim in now:
        assert now[victim]["opened"] != held[victim]["opened"]
