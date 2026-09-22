"""Prediction targets.

The primary target is the **risk-adjusted residual forward return, net of funding**:

    y[i, t] = ( R[i, t->t+h] - beta[i, t] * R_mkt[t->t+h] ) / ( ivol[i, t] * sqrt(h) )

where ``R`` is the log return of holding the perpetual from ``close(t)`` to ``close(t+h)`` **minus the
funding paid** over that period (for a perp, funding is part of the return, not a detail), ``beta`` and
``ivol`` are ex-ante (known at ``t``). Removing the market makes the target about *relative* value, which is
far more predictable and diversifiable than direction; dividing by ex-ante volatility makes it
homoscedastic so that high-volatility coins do not dominate the loss.

The market is modelled separately (``market`` targets) and only allowed to drive net exposure if that
model independently passes validation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from hermes.config import LabelConfig
from hermes.data.panel import Panel
from hermes.features.library import FeatureSet


def forward_sum(x: pd.DataFrame | pd.Series, h: int) -> pd.DataFrame | pd.Series:
    """Sum of ``x`` over bars ``t+1 .. t+h`` (NaN when the window is incomplete)."""
    return x.rolling(h, min_periods=h).sum().shift(-h)


@dataclass
class Targets:
    residual: dict[int, pd.DataFrame]
    total: dict[int, pd.DataFrame]
    market: dict[int, pd.Series]
    horizon: int

    @property
    def primary(self) -> pd.DataFrame:
        return self.residual[self.horizon]


def build_targets(panel: Panel, feats: FeatureSet, mask: pd.DataFrame, cfg: LabelConfig) -> Targets:
    r1 = feats.aux["r1"]
    vol = feats.aux["vol"]
    ivol = feats.aux["ivol"]
    beta = feats.aux["beta"]
    mkt = feats.aux["mkt"]["mkt"]
    funding = panel["funding_rate"].fillna(0.0) if "funding_rate" in panel else r1 * 0.0
    net_r1 = r1 - funding.where(r1.notna())
    mkt_vol = np.sqrt((mkt**2).ewm(halflife=72, min_periods=24, adjust=False).mean())

    residual, total, market = {}, {}, {}
    for h in cfg.horizons:
        fwd = forward_sum(net_r1, h)
        fwd_m = forward_sum(mkt, h)
        if cfg.residualize == "beta":
            res = fwd - beta.mul(fwd_m, axis=0)
            scale = ivol
        elif cfg.residualize == "mean":
            res = fwd.sub(fwd.where(mask).mean(axis=1), axis=0)
            scale = ivol
        else:
            res = fwd
            scale = vol
        if cfg.vol_normalize:
            res = res / (scale * np.sqrt(h))
        residual[h] = res.clip(-cfg.clip_sigma, cfg.clip_sigma).where(mask)
        total[h] = fwd.where(mask)
        market[h] = (fwd_m / (mkt_vol * np.sqrt(h))).clip(-cfg.clip_sigma, cfg.clip_sigma)
    return Targets(residual=residual, total=total, market=market, horizon=cfg.primary_horizon)
