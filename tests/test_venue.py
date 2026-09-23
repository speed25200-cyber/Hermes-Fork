"""OKX listing calendar and the venue-restricted universe, offline (simulated OKX endpoints)."""

from datetime import date

import httpx
import numpy as np
import pandas as pd

from hermes.config import UniverseConfig
from hermes.data.universe import daily_membership, universe_mask
from hermes.data.venue import OkxListing

LISTED_NOW = {"AAA-USDT-SWAP": ("1", date(2023, 3, 10)), "BB-USDT-SWAP": ("3", date(2022, 1, 1))}
ARCHIVED = {"CCC-USDT-SWAP": (date(2022, 8, 3), date(2023, 1, 20))}  # listed then delisted by OKX


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
    if inst in LISTED_NOW and LISTED_NOW[inst][0] == "1" and day >= LISTED_NOW[inst][1]:
        return httpx.Response(200)
    if inst in ARCHIVED and ARCHIVED[inst][0] <= day <= ARCHIVED[inst][1]:
        return httpx.Response(200)
    return httpx.Response(404)


def test_listing_calendar_locates_each_change_to_the_day(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_okx))
    okx = OkxListing(tmp_path, client=client, workers=4)
    windows = {s: (date(2022, 6, 1), date(2023, 6, 30)) for s in ("AAAUSDT", "BBUSDT", "CCCUSDT", "DDDUSDT")}
    cal = okx.calendar(windows)
    first = lambda s: cal.index[cal[s]][0].date()  # noqa: E731
    last = lambda s: cal.index[cal[s]][-1].date()  # noqa: E731
    assert first("AAAUSDT") == date(2023, 3, 10)  # traded that day (archive): selectable from the next day
    assert not cal["BBUSDT"].any()  # an equity swap sharing the ticker is never the crypto contract
    assert (first("CCCUSDT"), last("CCCUSDT")) == ARCHIVED["CCC-USDT-SWAP"]  # delisted since: from the archives
    assert cal["CCCUSDT"].sum() == (ARCHIVED["CCC-USDT-SWAP"][1] - ARCHIVED["CCC-USDT-SWAP"][0]).days + 1
    assert not cal["DDDUSDT"].any()
    n = len(okx.probes)
    assert n < 400  # weekly grid + bisection, not one probe per day
    # Probes are cached: a second calendar makes no request at all.
    offline = OkxListing(tmp_path, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
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
