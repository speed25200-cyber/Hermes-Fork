import numpy as np
import pandas as pd
import pytest

from hermes.data.intrabar import intrabar_aggregates


def _m1(n=90, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 1e-3, n)))
    qv = rng.uniform(1e5, 2e5, n)
    return pd.DataFrame(
        {"close": close, "volume": qv / close, "quote_volume": qv, "taker_buy_quote": qv * rng.uniform(0.3, 0.7, n)},
        index=idx,
    )


def test_realised_measures_match_definitions():
    m1 = _m1()
    agg = intrabar_aggregates(m1, "15m")
    assert list(agg.index) == list(pd.date_range("2024-01-01", periods=6, freq="15min", tz="UTC"))
    r = np.log(m1["close"]).diff()
    b = slice("2024-01-01 00:15", "2024-01-01 00:29")
    rb = r.loc[b]
    assert np.isclose(agg["ib_rv"].iloc[1], (rb**2).sum())
    assert np.isclose(agg["ib_vwap"].iloc[1], m1["quote_volume"].loc[b].sum() / m1["volume"].loc[b].sum())
    assert np.isclose(agg["ib_upfrac"].iloc[1], (rb > 0).mean())
    last = m1.loc["2024-01-01 00:27":"2024-01-01 00:29"]  # last 20% of a 15-minute bar = 3 minutes
    flow = (2 * last["taker_buy_quote"] - last["quote_volume"]).sum() / last["quote_volume"].sum()
    assert np.isclose(agg["ib_flow_last"].iloc[1], flow)
    assert (agg["ib_bv"] >= 0).all() and agg["ib_rskew"].abs().max() <= 10


def test_intrabar_is_causal():
    """Changing minutes after a bar's close never changes that bar's aggregates."""
    m1 = _m1()
    a = intrabar_aggregates(m1, "15m")
    m2 = m1.copy()
    m2.loc["2024-01-01 00:45":, "close"] *= 1.05
    m2.loc["2024-01-01 00:45":, "taker_buy_quote"] *= 0.5
    b = intrabar_aggregates(m2, "15m")
    pd.testing.assert_frame_equal(a.iloc[:3], b.iloc[:3])


def test_one_minute_base_bar_is_rejected():
    with pytest.raises(ValueError):
        intrabar_aggregates(_m1(), "1m")
