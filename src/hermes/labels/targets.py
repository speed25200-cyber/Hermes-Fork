"""Prediction targets.

The primary target is the **risk-adjusted residual forward return, net of funding**:

    y[i, t] = ( R[i, t->t+h] - beta[i, t] * R_mkt[t->t+h] ) / ( ivol[i, t] * sqrt(h) )

where ``R`` is the log return of holding the perpetual from ``close(t)`` to ``close(t+h)`` **minus the
funding paid** over that period (for a perp, funding is part of the return, not a detail), ``beta`` and
``ivol`` are ex-ante (known at ``t``). Removing the market makes the target about *relative* value, which is
far more predictable and diversifiable than direction; dividing by ex-ante volatility makes it
homoscedastic so that high-volatility coins do not dominate the loss.

With ``residualize: style`` the vol-normalised beta residual is further projected, bar by bar and across the
members, off an intercept and the two style exposures the style-neutral book removes (log 14-day dollar volume
and log residual volatility, both known at ``t``): the model then learns only the part of the cross-section a
style-neutral book can hold, instead of spending its capacity on style premia it will not be allowed to bet on.

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


def project_out(y: pd.DataFrame, exposures: list[pd.DataFrame], min_members: int = 8) -> pd.DataFrame:
    """Per-row OLS residual of ``y`` on ``[1, *exposures]`` over the cells where everything is finite.

    Rows with fewer than ``min_members`` usable cells become NaN (no reliable cross-sectional fit)."""
    Y = y.to_numpy(dtype=np.float64)
    Xs = [e.reindex(index=y.index, columns=y.columns).to_numpy(dtype=np.float64) for e in exposures]
    ok = np.isfinite(Y)
    for x in Xs:
        ok &= np.isfinite(x)
    n = ok.sum(axis=1)
    cols = [np.ones_like(Y)]
    for x in Xs:
        xz = np.where(ok, x, 0.0)
        mean = xz.sum(axis=1, keepdims=True) / np.maximum(n, 1)[:, None]
        cols.append(np.where(ok, x - mean, 0.0))  # demeaned: well conditioned, same residual
    X = np.stack(cols, axis=-1) * ok[..., None]
    Yz = np.where(ok, Y, 0.0)
    M = np.einsum("tni,tnj->tij", X, X)
    b = np.einsum("tni,tn->ti", X, Yz)
    good = n >= max(min_members, X.shape[-1] + 2)
    out = np.full_like(Y, np.nan)
    if good.any():
        k = X.shape[-1]
        ridge = 1e-12 * np.maximum(np.trace(M[good], axis1=1, axis2=2), 1e-12)[:, None, None] * np.eye(k)
        coef = np.linalg.solve(M[good] + ridge, b[good][..., None])[..., 0]
        fit = np.einsum("tnk,tk->tn", X[good], coef)
        out[good] = np.where(ok[good], Y[good] - fit, np.nan)
    return pd.DataFrame(out, index=y.index, columns=y.columns)


def style_frames(panel: Panel, ivol: pd.DataFrame) -> list[pd.DataFrame]:
    """Log 14-day average dollar volume and log residual volatility, as the style-neutral book sees them."""
    bpd = max(1, round(pd.Timedelta("1D") / panel.bar_delta))
    adv = panel["quote_volume"].astype("float64").rolling(bpd * 14, min_periods=bpd).mean()
    return [np.log(adv.where(adv > 0)), np.log(ivol.where(ivol > 0))]


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
    mkt_vol = (
        feats.aux["mkt_vol"]["mkt_vol"]
        if "mkt_vol" in feats.aux
        else np.sqrt((mkt**2).ewm(halflife=72, min_periods=24, adjust=False).mean())
    )

    styles = [x.where(mask) for x in style_frames(panel, ivol)] if cfg.residualize == "style" else []
    residual, total, market = {}, {}, {}
    for h in cfg.horizons:
        fwd = forward_sum(net_r1, h)
        fwd_m = forward_sum(mkt, h)
        if cfg.residualize in ("beta", "style"):
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
        if styles:
            res = project_out(res.where(mask), styles)
        residual[h] = res.clip(-cfg.clip_sigma, cfg.clip_sigma).where(mask)
        total[h] = fwd.where(mask)
        market[h] = (fwd_m / (mkt_vol * np.sqrt(h))).clip(-cfg.clip_sigma, cfg.clip_sigma)
    return Targets(residual=residual, total=total, market=market, horizon=cfg.primary_horizon)
