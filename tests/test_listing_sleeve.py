"""New-listing short sleeve: calendar, entries and exits, sizing, stops, and its netting with the book."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from hermes.config import ListingSleeveConfig
from hermes.live.listing_sleeve import HEDGE, ListingSleeve
from hermes.live.state import StateStore

NOW = pd.Timestamp("2026-09-25 12:00", tz="UTC")


class Cal:
    """Binance calendar stub: perpetual launches and first spot candles."""

    def __init__(self, launches, spot):
        self.launches, self.spot, self.calls = launches, spot, 0

    async def perp_listings(self):
        self.calls += 1
        return {s: int(t.value // 1_000_000) for s, t in self.launches.items()}

    async def spot_first_open(self, symbol):
        return self.spot.get(symbol)


def _sleeve(tmp_path, **kw):
    return ListingSleeve(ListingSleeveConfig(enabled=True, **kw), StateStore(tmp_path / "s"))


def test_calendar_keeps_new_tokens_in_their_window(tmp_path):
    cal = Cal(
        {
            "NEWUSDT": NOW - pd.Timedelta(hours=73),  # second tranche (+72 h) due now; no spot market: new token
            "SPOTNEWUSDT": NOW - pd.Timedelta(hours=25),  # first tranche due; spot opened 10 days before: new
            "OLDTOKUSDT": NOW - pd.Timedelta(hours=73),  # spot for a year: not a new token
            "EARLYUSDT": NOW - pd.Timedelta(hours=20),  # not yet 24 h old
            "STALEUSDT": NOW - pd.Timedelta(days=30),  # window long gone
        },
        {
            "SPOTNEWUSDT": NOW - pd.Timedelta(days=10),
            "OLDTOKUSDT": NOW - pd.Timedelta(days=400),
        },
    )
    sl = _sleeve(tmp_path)
    asyncio.run(sl.refresh(cal, NOW))
    assert sl.due(NOW) == [("NEWUSDT", 1), ("SPOTNEWUSDT", 0)]
    assert "STALEUSDT" not in sl.listings and sl.listings["OLDTOKUSDT"]["new_token"] is False
    asyncio.run(sl.refresh(cal, NOW + pd.Timedelta(hours=1)))
    assert cal.calls == 1  # cached for six hours
    assert sl.due(NOW + pd.Timedelta(hours=5)) == [("EARLYUSDT", 0)]  # the others' windows have passed
    assert sl.due(NOW + pd.Timedelta(hours=48)) == [("SPOTNEWUSDT", 1)]
    assert sl.symbols_needed(NOW) == sorted(["NEWUSDT", "SPOTNEWUSDT", HEDGE])
    assert sl.summary({})["calendar"] == {"refreshed_at": NOW.isoformat(), "listings": 4}
    sl.backoff(NOW + pd.Timedelta(hours=7))  # a failed refresh spaces out the retries, not the last success
    reloaded = ListingSleeve(sl.cfg, sl.store)
    assert sl.refreshed_at == NOW.isoformat() and reloaded.refreshed_at == NOW.isoformat()


def test_missed_windows_are_never_caught_up(tmp_path):
    """A first deploy, a restart or a late OKX listing must not open tranches outside their few hours."""
    sl = _sleeve(tmp_path)
    sl.listings = {"AUSDT": {"launch": (NOW - pd.Timedelta(hours=160)).isoformat(), "new_token": True}}
    assert sl.due(NOW) == []
    sl.listings = {"AUSDT": {"launch": (NOW - pd.Timedelta(hours=72.5)).isoformat(), "new_token": True}}
    tg = sl.targets(NOW, {"AUSDT": 1.0, HEDGE: 50_000.0}, 10_000.0, set(), {})  # not on OKX when due
    assert tg == {} and sl.coverage == {"AUSDT": False}
    tg = sl.targets(NOW + pd.Timedelta(hours=2), {"AUSDT": 1.0, HEDGE: 50_000.0}, 10_000.0, {"AUSDT"}, {})
    assert tg["AUSDT"] < 0  # listed later but still inside the window: enters
    assert sl.due(NOW + pd.Timedelta(hours=3)) == []


def test_entry_sizing_hedge_stop_exit_costs_and_persistence(tmp_path):
    sl = _sleeve(tmp_path, slots=5, leverage=1.0, stop=0.5, hedge_beta=1.0)
    t0 = NOW - pd.Timedelta(hours=73)  # the +72 h tranche is due (the +24 h one was missed long ago)
    sl.listings = {s: {"launch": t0.isoformat(), "new_token": True} for s in ("AUSDT", "BUSDT", "CUSDT")}
    prices = {"AUSDT": 2.0, "BUSDT": 10.0, "CUSDT": 1.0, HEDGE: 100_000.0}
    vol = {"AUSDT": 0.062, "BUSDT": 0.5}  # A: half the reference vol -> full size; B: 4x -> the 0.25 floor
    sl.set_nav(10_000.0)
    tg = sl.targets(NOW, prices, nav=10_000.0, tradable={"AUSDT", "BUSDT"}, vol_daily=vol)
    # A tranche is half a listing's share: A 10k x 1 / 5 / 2; B the same x 0.25. C is not on OKX: skipped.
    assert tg["AUSDT"] == pytest.approx(-1_000.0) and tg["BUSDT"] == pytest.approx(-250.0)
    assert tg[HEDGE] == pytest.approx(1_250.0) and "CUSDT" not in tg
    assert sl.stop_fractions(prices) == pytest.approx({"AUSDT": 0.5, "BUSDT": 0.5})
    assert sl.coverage == {"AUSDT": True, "BUSDT": True, "CUSDT": False}
    moved = dict(prices, AUSDT=1.0)
    assert sl.holdings(moved)["AUSDT"] == pytest.approx(-500.0)  # quantities stay fixed
    again = ListingSleeve(sl.cfg, sl.store)  # a restart reloads the trades and the NAV
    assert [(t.symbol, t.tranche) for t in again.open] == [("AUSDT", 1), ("BUSDT", 1)] and again._nav == 10_000.0
    again.on_stops({"BUSDT": 15.0}, prices, NOW + pd.Timedelta(hours=1))
    b_costs = 250 * 0.0015 + 250 * 0.0006 + 25 * 15.0 * 0.0015 + 0.0025 * 100_000 * 0.0006
    assert again.done[-1]["pnl"] == pytest.approx(-25.0 * 5.0 - b_costs, abs=0.01)  # rounded to cents
    later = t0 + pd.Timedelta(hours=168)
    assert again.targets(later, moved, 10_000.0, {"AUSDT"}, {}) == {}
    a_costs = 1_000 * 0.0015 + 1_000 * 0.0006 + 500 * 1.0 * 0.0015 + 0.01 * 100_000 * 0.0006
    assert again.done[-1]["pnl"] == pytest.approx(500.0 - a_costs, abs=0.01)
    assert again.summary(moved)["closed_pnl"] == pytest.approx(round(-125 - b_costs, 2) + round(500 - a_costs, 2))


def test_short_notional_cap_slots_and_no_stop(tmp_path):
    sl = _sleeve(tmp_path, slots=2, leverage=0.5, stop=0.0)
    t0 = NOW - pd.Timedelta(hours=25)
    sl.listings = {s: {"launch": t0.isoformat(), "new_token": True} for s in ("AUSDT", "BUSDT", "CUSDT")}
    prices = {"AUSDT": 1.0, "BUSDT": 1.0, "CUSDT": 1.0, HEDGE: 50_000.0}
    tg = sl.targets(NOW, prices, 1_000.0, {"AUSDT", "BUSDT", "CUSDT"}, {}, entries=True)
    # Unknown vol -> scale 0.5: 1000 x 0.5 / 2 slots / 2 tranches x 0.5 = 62.5; two listings only.
    assert sorted(tg) == sorted(["AUSDT", "BUSDT", HEDGE]) and tg["AUSDT"] == pytest.approx(-62.5)
    assert sl.stop_fractions(prices) == {"AUSDT": 100.0, "BUSDT": 100.0}  # no stop, not the brokers' 15 %
    assert sl.targets(NOW, prices, 1_000.0, {"CUSDT"}, {}, entries=False).get("CUSDT") is None


def test_funding_close_all_and_reconcile(tmp_path):
    sl = _sleeve(tmp_path, stop=0.5)
    sl.listings = {"AUSDT": {"launch": (NOW - pd.Timedelta(hours=25)).isoformat(), "new_token": True}}
    prices = {"AUSDT": 1.0, HEDGE: 50_000.0}
    sl.set_nav(1_000.0)
    sl.targets(NOW, prices, 1_000.0, {"AUSDT"}, {})
    t = sl.open[0]
    sl.accrue_funding({"AUSDT": -0.01, HEDGE: 0.0001}, prices)  # shorts pay a negative rate; the hedge pays too
    assert t.funding == pytest.approx(t.qty * 1.0 * 0.01 - t.hedge_qty * 50_000.0 * 0.0001)
    sl.reconcile({"AUSDT": -50.0}, prices, NOW)
    assert len(sl.open) == 1
    sl.close_all(prices, NOW, "halt")  # a halt books the trades (the kill rule counts them)
    assert not sl.open and sl.done[-1]["reason"] == "halt" and sl.done[-1]["funding"] == pytest.approx(t.funding, 0.01)
    sl.listings["BUSDT"] = {"launch": (NOW - pd.Timedelta(hours=25)).isoformat(), "new_token": True}
    sl.targets(NOW, dict(prices, BUSDT=1.0), 1_000.0, {"BUSDT"}, {})
    sl.reconcile({}, prices, NOW)  # flattened elsewhere
    assert not sl.open and sl.done[-1]["reason"] == "gone"


def test_kill_rule_stops_new_entries(tmp_path):
    """Fixed in advance: the last 25 closed trades losing on average (or > 10 % of the sleeve's cap) end entries."""
    sl = _sleeve(tmp_path, kill_trades=3, kill_loss=0.10, leverage=1.0)
    win = {"symbol": "W", "tranche": 0, "notional": 100.0, "pnl": 10.0, "nav": 1_000.0, "reason": "end"}
    lose = dict(win, pnl=-20.0)
    sl.done = [win, win, dict(lose, pnl=-15.0)]  # mean return > 0, small loss: keeps trading
    assert not sl.suspended()
    sl.done = [win, lose, lose]  # mean return < 0
    assert sl.suspended()
    big = dict(win, notional=1_000.0, pnl=-150.0)  # mean return > 0 but -110 USDT of a 1000 cap (> 10 %)
    sl.done = [big, dict(win, pnl=30.0), dict(win, pnl=10.0)]
    assert sl.suspended()
    sl.done = [dict(t, nav=0.0) for t in (win, win, dict(lose, pnl=-15.0))]  # NAV unknown: judged on returns
    assert not sl.suspended()
    sl.done = [big, dict(win, pnl=30.0), dict(win, pnl=10.0)]
    sl.listings = {"AUSDT": {"launch": (NOW - pd.Timedelta(hours=25)).isoformat(), "new_token": True}}
    assert sl.targets(NOW, {"AUSDT": 1.0, HEDGE: 50_000.0}, 1_000.0, {"AUSDT"}, {}) == {}


@pytest.mark.slow
def test_engine_nets_the_sleeve_with_the_book(cfg_small, tmp_path):
    """The book is decided on the account net of the sleeve; the broker ends with book + sleeve per contract."""
    from hermes.live.engine import LiveEngine
    from test_live import FakeFeed, _engine

    panel, bundle, broker, store, cfg = _engine(cfg_small, tmp_path)
    cfg = cfg.model_copy(
        update={"live": cfg.live.model_copy(update={"listing_sleeve": ListingSleeveConfig(enabled=True)})}
    )

    class Feed(FakeFeed):
        async def perp_listings(self):
            last = self.panel.index[self.t - 1]
            return {"S11USDT": int((last - pd.Timedelta(hours=25)).value // 1_000_000)}

        async def spot_first_open(self, symbol):
            return None

    feed = Feed(panel, 96 * 45 + 40, 96 * 30)
    eng = LiveEngine(cfg, bundle, feed, broker, store, mode="paper")
    d = asyncio.run(eng.step())
    assert d is not None and eng.sleeve is not None
    assert [(t.symbol, t.tranche) for t in eng.sleeve.open] == [("S11USDT", 0)]  # the +24 h tranche
    prices = {s: float(v) for s, v in feed.panel["close"].iloc[feed.t - 1].dropna().items()}
    held = eng.sleeve.holdings(prices)
    pos = {s: p.notional for s, p in asyncio.run(broker.positions()).items()}
    for s in ("S11USDT", HEDGE):
        assert pos.get(s, 0.0) == pytest.approx(d.targets.get(s, 0.0) + held[s], rel=0.02, abs=5.0)
    assert np.isclose(held["S11USDT"], -held[HEDGE], rtol=1e-6)
    import json

    status = json.loads((tmp_path / "state" / "status.json").read_text())
    assert status["listing_sleeve"]["open"][0]["symbol"] == "S11USDT"
