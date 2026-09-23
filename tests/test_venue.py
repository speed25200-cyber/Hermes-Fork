"""OKX listing calendar and the venue-restricted universe, offline (simulated OKX endpoints)."""

from datetime import date, timedelta

import httpx
import numpy as np
import pandas as pd

from hermes.config import UniverseConfig
from hermes.data.universe import daily_membership, universe_mask
from hermes.data.venue import OkxListing

LISTED_NOW = {
    "AAA-USDT-SWAP": ("1", date(2023, 3, 10)),
    "BB-USDT-SWAP": ("3", date(2022, 1, 1)),
    "BTC-USDT-SWAP": ("1", date(2020, 1, 1)),
    "ZZZ-USDT-SWAP": ("1", date(2022, 1, 1)),  # listTime predates a delisting and relisting
}
ARCHIVED = {"CCC-USDT-SWAP": (date(2022, 8, 3), date(2023, 1, 20))}  # listed then delisted by OKX
GAPS = {"ZZZ-USDT-SWAP": (date(2023, 1, 5), date(2023, 5, 20))}
PUBLISHED = date.today() - timedelta(days=2)  # archives appear with a lag


def _okx(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "public/instruments" in url:
        data = [
            {
                "instId": k,
                "instCategory": c,
                "listTime": str(int(pd.Timestamp(d, tz="UTC").timestamp() * 1000)),
                "state": "live",
            }
            for k, (c, d) in LISTED_NOW.items()
        ]
        return httpx.Response(200, json={"data": data})
    name = url.rsplit("/", 1)[-1]  # {inst}-trades-{YYYY-MM-DD}.zip
    inst, day = name[: name.index("-trades-")], date.fromisoformat(name[-14:-4])
    if day > PUBLISHED or (inst in GAPS and GAPS[inst][0] <= day <= GAPS[inst][1]):
        return httpx.Response(404)
    if inst in LISTED_NOW and LISTED_NOW[inst][0] == "1" and day >= LISTED_NOW[inst][1]:
        return httpx.Response(200)
    if inst in ARCHIVED and ARCHIVED[inst][0] <= day <= ARCHIVED[inst][1]:
        return httpx.Response(200)
    return httpx.Response(404)


def test_listing_calendar_locates_each_change_to_the_day(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_okx))
    okx = OkxListing(tmp_path, client=client, workers=4, seed=None)
    windows = {s: (date(2022, 6, 1), date(2023, 6, 30)) for s in ("AAAUSDT", "BBUSDT", "CCCUSDT", "DDDUSDT", "ZZZUSDT")}
    windows["AAAUSDT"] = (date(2022, 6, 1), date.today())  # up to today: the unpublished days are not delistings
    cal = okx.calendar(windows)
    first = lambda s: cal.index[cal[s]][0].date()  # noqa: E731
    last = lambda s: cal.index[cal[s]][-1].date()  # noqa: E731
    assert first("AAAUSDT") == date(2023, 3, 10)  # traded that day (archive): selectable from the next day
    assert not cal["BBUSDT"].any()  # an equity swap sharing the ticker is never the crypto contract
    assert (first("CCCUSDT"), last("CCCUSDT")) == ARCHIVED["CCC-USDT-SWAP"]  # delisted since: from the archives
    assert cal["CCCUSDT"].sum() == (ARCHIVED["CCC-USDT-SWAP"][1] - ARCHIVED["CCC-USDT-SWAP"][0]).days + 1
    assert not cal["DDDUSDT"].any()
    gap = cal["ZZZUSDT"]
    window = (gap.index >= pd.Timestamp("2022-06-01", tz="UTC")) & (gap.index <= pd.Timestamp("2023-06-30", tz="UTC"))
    off = gap.index[~gap & window]
    assert (off[0].date(), off[-1].date()) == GAPS["ZZZ-USDT-SWAP"]  # the gap listTime does not show
    assert cal["AAAUSDT"].iloc[-3:].all()  # today and the unpublished days: live on OKX now
    n = len(okx.probes)
    assert n < 500  # grid + bisection, not one probe per day
    # Probes are cached: a second calendar makes no request at all.
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    offline = OkxListing(tmp_path, client=down, seed=None)
    offline._catalog = okx.catalog()
    pd.testing.assert_frame_equal(offline.calendar(windows), cal)


def test_membership_only_selects_contracts_listed_on_the_venue_the_day_before():
    days = pd.date_range("2024-01-01", periods=120, freq="1D", tz="UTC")
    qv = pd.DataFrame({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0}, index=days)
    alive = qv > 0
    cfg = UniverseConfig(top_n=2, min_history_days=10, liquidity_lookback_days=10, reselect_every_days=1)
    listed = pd.DataFrame(True, index=days, columns=qv.columns)
    listed.loc[:, "A"] = False
    listed.loc[days[60] :, "A"] = True  # A lists on day 60: selectable from day 61
    m = daily_membership(qv, alive, cfg, listed)
    assert not m["A"].iloc[:61].any() and m["A"].iloc[61:].all()
    assert m["C"].iloc[20:61].all() and not m["C"].iloc[61:].any()  # C filled the slot while A was unlisted
    assert (m.sum(axis=1).iloc[20:] == 2).all()


def test_universe_mask_reads_the_panel_venue_field(small_panel):
    cfg = UniverseConfig(top_n=10, min_history_days=3)
    base = universe_mask(small_panel, cfg)
    top = base.sum().idxmax()
    listed = pd.DataFrame(1.0, index=small_panel.index, columns=small_panel.symbols)
    listed[top] = 0.0
    m = universe_mask(small_panel.with_fields({"venue_listed": listed}), cfg)
    assert not m[top].any()
    assert np.array_equal(m.sum(axis=1).to_numpy()[-100:], base.sum(axis=1).to_numpy()[-100:])


def test_an_unpublished_archive_is_not_cached_as_a_delisting(tmp_path, monkeypatch):
    import hermes.data.venue as venue

    today = date(2025, 3, 10)
    monkeypatch.setattr(venue, "_utc_today", lambda: today)
    published = {"until": date(2025, 3, 8)}  # the archive of the 9th is late

    def okx(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "public/instruments" in url:
            return httpx.Response(
                200, json={"data": [{"instId": "BTC-USDT-SWAP", "instCategory": "1", "listTime": "0"}]}
            )
        day = date.fromisoformat(url.rsplit("/", 1)[-1][-14:-4])
        return httpx.Response(200 if day <= published["until"] else 404)

    (tmp_path / "venue").mkdir()
    (tmp_path / "venue" / "okx_probes.json").write_text('{"BTC-USDT-SWAP|2025-03-05": false}')  # older bug
    okx = OkxListing(tmp_path, client=httpx.Client(transport=httpx.MockTransport(okx)), workers=2, seed=None)
    assert "BTC-USDT-SWAP|2025-03-05" not in okx.probes  # a cached "BTC absent" is a leftover: dropped
    assert okx.archive_end() == date(2025, 3, 8)
    assert "BTC-USDT-SWAP|2025-03-09" not in okx.probes  # "not published yet" is never remembered
    published["until"] = date(2025, 3, 9)
    assert okx.archive_end() == date(2025, 3, 9)


def test_offline_catalog_uses_the_newest_snapshot_and_distrusts_unknown_contracts(tmp_path, monkeypatch):
    import json

    import hermes.data.venue as venue

    monkeypatch.setattr(venue, "_utc_today", lambda: date(2025, 6, 30))
    seed = tmp_path / "seed"
    seed.mkdir()
    old = {"fetched": "2025-01-01", "data": [{"instId": "BTC-USDT-SWAP", "instCategory": "1", "listTime": "0"}]}
    (seed / "instruments.json").write_text(json.dumps(old))
    (tmp_path / "venue").mkdir()
    newer = [{"instId": "BTC-USDT-SWAP", "instCategory": "1", "listTime": "0", "state": "live"}]
    (tmp_path / "venue" / "okx_instruments_2025-03-01.json").write_text(json.dumps(newer))
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    okx = OkxListing(tmp_path, client=down, workers=2, seed=seed)
    weeks = pd.date_range("2025-02-05", "2025-06-18", freq="7D")  # the anchored grid (Wednesdays)
    okx.probes.update({f"NEW-USDT-SWAP|{d.date()}": True for d in weeks})
    okx.probes.update({f"BTC-USDT-SWAP|{d}": True for d in ("2025-06-25", "2025-02-05")})
    okx.catalog()
    assert okx.catalog_date == date(2025, 3, 1)  # the newest of the cached and committed snapshots
    cal = okx.calendar({"NEWUSDT": (date(2025, 2, 5), date(2025, 6, 20))})
    assert cal.loc["2025-02-20", "NEWUSDT"]  # traded, and known to the list of March 1st
    assert not cal.loc["2025-06-18", "NEWUSDT"]  # after that list, its category is unknown: not selectable


def test_committed_snapshot_answers_offline_for_research_windows(tmp_path):
    # OKX unreachable (the snapshot's purpose): the calendar must come entirely from the committed probes,
    # on the anchored grid, for any window a configuration can ask for -- and keep the known transitions.
    from hermes.data.venue import SEED

    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    okx = OkxListing(tmp_path, client=down, workers=2, seed=SEED)
    # Binance lives clipped to a configuration starting 2022-06-01 (windows start between grid days).
    end = date(2026, 9, 1)
    windows = {s: (date(2022, 6, 1), end) for s in ("BTCUSDT", "ETHUSDT", "ZECUSDT", "XMRUSDT")}
    windows |= {"ENAUSDT": (date(2024, 4, 2), end), "TONUSDT": (date(2024, 3, 1), date(2026, 6, 23))}
    windows |= {"JUPUSDT": (date(2024, 1, 31), end), "WLDUSDT": (date(2023, 7, 24), end)}
    cal = okx.calendar(windows)
    on = lambda s, d: bool(cal.loc[d, s])  # noqa: E731
    assert cal["BTCUSDT"].loc["2022-06-01":"2026-09-01"].all()
    assert on("ZECUSDT", "2023-12-19") and not on("ZECUSDT", "2024-06-15") and on("ZECUSDT", "2025-11-07")
    assert on("XMRUSDT", "2023-06-01") and not on("XMRUSDT", "2024-06-01")
    assert not on("ENAUSDT", "2025-06-01") and on("ENAUSDT", "2025-10-01")
    assert cal["JUPUSDT"].any()
