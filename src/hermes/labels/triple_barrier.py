"""Triple-barrier labelling and label-uniqueness weights (López de Prado, *Advances in Financial ML*, ch. 3-4).

Used for trade-level (event) models and meta-labelling: an entry at ``close(t)`` is labelled by which of
three barriers is touched first -- take-profit ``+pt * sigma``, stop ``-sl * sigma`` (sigma = ex-ante
volatility over the holding period), or the vertical barrier after ``h`` bars. High/low paths are used so
intrabar touches count; when both barriers are touched inside the same bar the stop is assumed first
(the conservative convention).

Overlapping labels are not independent; ``uniqueness_weights`` returns the average uniqueness of each
label, the standard correction for the effective sample size of overlapping outcomes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def triple_barrier(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    sigma: pd.Series,
    horizon: int,
    pt: float = 1.5,
    sl: float = 1.0,
    side: pd.Series | None = None,
) -> pd.DataFrame:
    """Label every bar of one contract. Returns columns ``label`` (+1/-1/0), ``ret`` and ``bars``.

    ``side`` (+1 long / -1 short) turns the output into a meta-label target: ``label=1`` iff the side's
    trade reached its take-profit, the natural target for a model that decides *whether* to act on a
    primary signal.
    """
    c = close.to_numpy(float)
    hi = high.to_numpy(float)
    lo = low.to_numpy(float)
    sg = sigma.to_numpy(float) * np.sqrt(horizon)
    sd = np.ones(len(c)) if side is None else side.to_numpy(float)
    n = len(c)
    label = np.full(n, np.nan)
    ret = np.full(n, np.nan)
    bars = np.full(n, np.nan)
    for t in range(n - 1):
        if not (np.isfinite(c[t]) and np.isfinite(sg[t]) and sg[t] > 0 and np.isfinite(sd[t]) and sd[t] != 0):
            continue
        end = min(n - 1, t + horizon)
        if end <= t:
            continue
        up = c[t] * np.exp(pt * sg[t])
        dn = c[t] * np.exp(-sl * sg[t])
        s = np.sign(sd[t])
        hit = 0
        k_hit = end
        for k in range(t + 1, end + 1):
            if not np.isfinite(hi[k]):
                break
            touch_up = hi[k] >= up
            touch_dn = lo[k] <= dn
            if s > 0:
                if touch_dn:
                    hit, k_hit = -1, k
                    break
                if touch_up:
                    hit, k_hit = 1, k
                    break
            else:
                if touch_up:
                    hit, k_hit = -1, k
                    break
                if touch_dn:
                    hit, k_hit = 1, k
                    break
        if hit == 0 and end - t < horizon:
            continue  # incomplete vertical barrier at the end of the sample
        if hit == 1:
            px = up if s > 0 else dn
        elif hit == -1:
            px = dn if s > 0 else up
        else:
            px = c[k_hit]
        r = np.log(px / c[t]) * s
        ret[t] = r
        bars[t] = k_hit - t
        if side is None:
            label[t] = hit if hit != 0 else np.sign(r) * 0.0
        else:
            label[t] = 1.0 if hit == 1 else 0.0
    return pd.DataFrame({"label": label, "ret": ret, "bars": bars}, index=close.index)


def uniqueness_weights(bars: pd.Series) -> pd.Series:
    """Average uniqueness of each label given its holding length (in bars) on a regular grid."""
    b = bars.to_numpy(float)
    n = len(b)
    conc = np.zeros(n + 1)
    valid = np.isfinite(b)
    for t in np.nonzero(valid)[0]:
        e = min(n, t + int(b[t]) + 1)
        conc[t + 1 : e] += 1  # label t is "alive" on bars t+1 .. t+bars
    w = np.full(n, np.nan)
    for t in np.nonzero(valid)[0]:
        e = min(n, t + int(b[t]) + 1)
        seg = conc[t + 1 : e]
        w[t] = float(np.mean(1.0 / seg)) if len(seg) else 1.0
    return pd.Series(w, index=bars.index)
