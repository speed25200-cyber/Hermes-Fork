"""Intrabar microstructure: 1-minute klines aggregated into each base bar (15m, 30m, ...).

For a base bar ``t`` every statistic uses only the 1-minute bars whose close falls inside ``t`` -- known at
``close(t)``, like every other field of the panel. Measures (Andersen-Bollerslev realised variance,
Barndorff-Nielsen-Shephard bipower variation, Amaya et al. realised skewness, and order-flow/VWAP
descriptors):

* ``ib_rv``        realised variance, sum of squared 1-minute log returns;
* ``ib_bv``        bipower variation (pi/2 * sum |r_i||r_{i-1}|), robust to jumps; ``rv - bv`` ~ jump part;
* ``ib_rskew``     realised skewness sqrt(n) * sum r^3 / rv^1.5;
* ``ib_flow_last`` taker imbalance over the last 20% of the bar (pressure going into the close);
* ``ib_flow_std``  dispersion of the minute-by-minute imbalance;
* ``ib_vwap``      volume-weighted average price of the bar;
* ``ib_upfrac``    share of up minutes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hermes.config import BAR_MINUTES
from hermes.data.panel import BAR_TO_OFFSET

IB_COLUMNS = ("ib_rv", "ib_bv", "ib_rskew", "ib_flow_last", "ib_flow_std", "ib_vwap", "ib_upfrac")


def intrabar_aggregates(m1: pd.DataFrame, bar: str) -> pd.DataFrame:
    """Aggregate a 1-minute kline frame (open-time index, OHLCV + ``quote_volume`` + ``taker_buy_quote``)."""
    if BAR_MINUTES[bar] <= 1:
        raise ValueError("intrabar aggregates need a base bar longer than one minute")
    if m1.empty:
        return pd.DataFrame(columns=list(IB_COLUMNS), dtype="float64")
    m1 = m1[~m1.index.duplicated(keep="last")].sort_index()
    close = m1["close"].astype("float64")
    r = np.log(close).diff()
    # A return is attributed to the minute in which it ends; the first minute of a bar carries the jump
    # from the previous bar's last close, which is inside the new bar's life.
    key = m1.index.floor(BAR_TO_OFFSET[bar])
    qv = m1["quote_volume"].astype("float64")
    vol = m1["volume"].astype("float64")
    tbq = m1["taker_buy_quote"].astype("float64")
    imb = ((2 * tbq - qv) / qv.where(qv > 0)).clip(-1, 1)
    minutes = BAR_MINUTES[bar]
    k_last = max(1, round(minutes * 0.2))
    pos = (m1.index.hour * 60 + m1.index.minute) % minutes
    last = pos >= minutes - k_last
    frame = pd.DataFrame(
        {
            "r2": r**2,
            "r3": r**3,
            "bp": (r.abs() * r.abs().shift(1)),
            "up": (r > 0).astype("float64").where(r.notna()),
            "n": r.notna().astype("float64"),
            "qv": qv,
            "vol": vol,
            "imb": imb,
            "signed_last": (2 * tbq - qv).where(last, 0.0),
            "qv_last": qv.where(last, 0.0),
        },
        index=m1.index,
    )
    g = frame.groupby(key)
    s = g.sum(min_count=1)
    rv = s["r2"]
    n = s["n"].replace(0, np.nan)
    out = pd.DataFrame(
        {
            "ib_rv": rv,
            "ib_bv": (np.pi / 2) * s["bp"],
            "ib_rskew": (np.sqrt(n) * s["r3"] / rv.where(rv > 0) ** 1.5).clip(-10, 10),
            "ib_flow_last": (s["signed_last"] / s["qv_last"].where(s["qv_last"] > 0)).clip(-1, 1),
            "ib_flow_std": g["imb"].std(),
            "ib_vwap": s["qv"] / s["vol"].where(s["vol"] > 0),
            "ib_upfrac": s["up"] / n,
        }
    )
    out.index.name = None
    return out.astype("float64")
