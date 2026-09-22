"""Causal feature library.

Every feature is a (time x symbol) frame whose row ``t`` depends only on bars ``<= t`` (known at ``close(t)``).
Features are expressed in **scale-free units** (returns divided by ex-ante volatility, z-scores, ratios,
ranks) so one model can be shared across contracts whose prices and volatilities differ by orders of
magnitude, and across time as volatility regimes change.

Families (see ``docs/RESEARCH.md`` for the evidence behind each):

* price: multi-horizon vol-normalised returns (momentum / reversal), idiosyncratic (market-residual)
  returns, trend slopes, position within the recent range, distance to extremes;
* risk: EWMA / Parkinson / Garman-Klass volatility, vol-of-vol regime, semi-variance asymmetry, skew,
  rolling beta;
* flow: signed aggressive order flow (taker imbalance) and its persistence, flow/return divergence;
* activity & liquidity: dollar-volume surprises, trade size, Amihud illiquidity, high-low spread proxy;
* carry & positioning: settled funding, funding trend and surprise, premium (basis) level/change, open
  interest change when available;
* market state: market return/vol, cross-sectional dispersion, breadth, BTC-vs-market, market-wide
  funding and flow (shared by all contracts);
* calendar: hour of day, day of week, bars to the next funding settlement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.special import ndtri

from hermes.config import FeatureConfig
from hermes.data.panel import Panel

EPS = 1e-12


def ewm_vol(returns: pd.DataFrame, halflife: int, min_periods: int | None = None) -> pd.DataFrame:
    mp = min_periods if min_periods is not None else max(halflife // 2, 10)
    return np.sqrt((returns**2).ewm(halflife=halflife, min_periods=mp, adjust=False).mean())


def cs_rank_gauss(df: pd.DataFrame, mask: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cross-sectional rank -> standard normal scores, computed among universe members only."""
    x = df.where(mask) if mask is not None else df
    r = x.rank(axis=1, method="average")
    n = x.notna().sum(axis=1)
    u = (r.sub(0.5)).div(n.replace(0, np.nan), axis=0)
    return pd.DataFrame(ndtri(u.clip(1e-6, 1 - 1e-6).to_numpy()), index=df.index, columns=df.columns).where(x.notna())


def cs_demean(df: pd.DataFrame, mask: pd.DataFrame | None = None) -> pd.DataFrame:
    x = df.where(mask) if mask is not None else df
    return x.sub(x.mean(axis=1), axis=0)


def rolling_z(df: pd.DataFrame, window: int, min_periods: int | None = None) -> pd.DataFrame:
    mp = min_periods or max(window // 3, 10)
    mu = df.rolling(window, min_periods=mp).mean()
    sd = df.rolling(window, min_periods=mp).std()
    return (df - mu) / (sd + EPS)


def market_return(returns: pd.DataFrame, mask: pd.DataFrame | None = None) -> pd.Series:
    """Equal-weight return of universe members (the tradable 'market' factor)."""
    x = returns.where(mask) if mask is not None else returns
    return x.mean(axis=1)


def rolling_beta(returns: pd.DataFrame, mkt: pd.Series, halflife: int) -> pd.DataFrame:
    """EWMA beta of each contract on the market; causal, shrunk toward 1 when history is short."""
    m = mkt.to_numpy()[:, None]
    r = returns.to_numpy()
    cov = (
        pd.DataFrame(r * m, index=returns.index, columns=returns.columns)
        .ewm(halflife=halflife, min_periods=24, adjust=False)
        .mean()
    )
    var = pd.Series(mkt**2).ewm(halflife=halflife, min_periods=24, adjust=False).mean()
    beta = cov.div(var + EPS, axis=0)
    # Observations in a fixed trailing window (not since the first bar): the shrinkage weight must not
    # depend on how much history happens to be loaded, or live and research features would diverge.
    n_obs = returns.notna().astype(float).rolling(4 * halflife, min_periods=1).sum()
    w = (n_obs / (n_obs + halflife)).clip(0, 1)
    return (w * beta + (1 - w) * 1.0).clip(-1.0, 4.0)


@dataclass
class FeatureSet:
    frames: dict[str, pd.DataFrame]
    market: dict[str, pd.Series] = field(default_factory=dict)
    aux: dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return list(self.frames) + list(self.market)

    def stack(
        self, mask: pd.DataFrame, rows: np.ndarray | pd.Index | None = None, dtype: type = np.float32
    ) -> tuple[np.ndarray, pd.MultiIndex]:
        """Long format (time, symbol) matrix restricted to universe members."""
        index = mask.index if rows is None else (rows if isinstance(rows, pd.Index) else mask.index[rows])
        m = mask.loc[index].to_numpy()
        t_idx, s_idx = np.nonzero(m)
        cols = []
        for name in self.frames:
            cols.append(self.frames[name].loc[index].to_numpy(dtype=dtype)[t_idx, s_idx])
        for name in self.market:
            cols.append(self.market[name].loc[index].to_numpy(dtype=dtype)[t_idx])
        X = np.column_stack(cols) if cols else np.empty((len(t_idx), 0), dtype=dtype)
        mi = pd.MultiIndex.from_arrays([index[t_idx], mask.columns[s_idx]], names=["time", "symbol"])
        return X, mi


def build_features(panel: Panel, mask: pd.DataFrame, cfg: FeatureConfig, bars_per_day: int) -> FeatureSet:
    """Compute the full feature set. ``mask`` (universe membership) is only used for cross-sectional
    statistics, so that the market and the ranks are defined over what was actually tradable."""
    close = panel["close"]
    high, low, open_ = panel["high"], panel["low"], panel["open"]
    qv = panel["quote_volume"]
    logc = np.log(close)
    r1 = logc.diff()
    vol = ewm_vol(r1, cfg.vol_halflife)
    vol_safe = vol.where(vol > 0).ffill()

    F: dict[str, pd.DataFrame] = {}
    M: dict[str, pd.Series] = {}

    # ---------------- market factor & residuals ----------------
    mkt = market_return(r1, mask).fillna(0.0)
    beta = rolling_beta(r1, mkt, halflife=cfg.vol_halflife * 2)
    resid1 = r1 - beta.mul(mkt, axis=0)
    ivol = ewm_vol(resid1, cfg.vol_halflife).where(lambda v: v > 0).ffill()
    mkt_vol = np.sqrt((mkt**2).ewm(halflife=cfg.vol_halflife, min_periods=24, adjust=False).mean())

    # ---------------- price: momentum / reversal ----------------
    cum_resid = resid1.fillna(0.0).cumsum().where(close.notna())
    for w in cfg.return_windows:
        rw = logc - logc.shift(w)
        F[f"ret_{w}"] = (rw / (vol_safe * np.sqrt(w))).clip(-8, 8)
        F[f"iret_{w}"] = ((cum_resid - cum_resid.shift(w)) / (ivol * np.sqrt(w))).clip(-8, 8)
    for fast, slow in ((6, 24), (24, 96), (72, 288)):
        ema_f = logc.ewm(span=fast, adjust=False, min_periods=fast).mean()
        ema_s = logc.ewm(span=slow, adjust=False, min_periods=slow).mean()
        F[f"trend_{fast}_{slow}"] = ((ema_f - ema_s) / (vol_safe * np.sqrt(slow))).clip(-8, 8)
    for w in (24, 168):
        hh = high.rolling(w, min_periods=w // 2).max()
        ll = low.rolling(w, min_periods=w // 2).min()
        F[f"range_pos_{w}"] = ((close - ll) / (hh - ll + EPS)).clip(0, 1) - 0.5
        F[f"dist_high_{w}"] = (np.log(hh / close) / (vol_safe * np.sqrt(w))).clip(0, 10)
        F[f"dist_low_{w}"] = (np.log(close / ll) / (vol_safe * np.sqrt(w))).clip(0, 10)

    # ---------------- risk ----------------
    park = (np.log(high / low) ** 2) / (4 * np.log(2))
    gk = 0.5 * np.log(high / low) ** 2 - (2 * np.log(2) - 1) * np.log(close / open_) ** 2
    for w in cfg.vol_windows:
        rv = np.sqrt((r1**2).rolling(w, min_periods=w // 2).mean())
        F[f"rv_ratio_{w}"] = np.log((rv + EPS) / (vol_safe + EPS)).clip(-3, 3)
        F[f"park_ratio_{w}"] = np.log(
            (np.sqrt(park.rolling(w, min_periods=w // 2).mean()) + EPS) / (vol_safe + EPS)
        ).clip(-3, 3)
    F["gk_vol_24"] = np.log(np.sqrt(gk.clip(lower=0).rolling(24, min_periods=12).mean()) + 1e-8)
    F["vol_level"] = np.log(vol_safe * np.sqrt(bars_per_day * 365) + 1e-8)
    F["vol_cs"] = F["vol_level"]  # will be cross-sectionally ranked below
    F["ivol_share"] = (ivol / (vol_safe + EPS)).clip(0, 3)
    F["beta"] = beta
    up = (r1.clip(lower=0) ** 2).rolling(168, min_periods=48).sum()
    dn = (r1.clip(upper=0) ** 2).rolling(168, min_periods=48).sum()
    F["semivar_asym"] = ((up - dn) / (up + dn + EPS)).clip(-1, 1)
    F["skew_168"] = r1.rolling(168, min_periods=72).skew().clip(-5, 5)
    F["kurt_168"] = np.log1p(r1.rolling(168, min_periods=72).kurt().clip(-2, 50) + 2)
    F["volvol_168"] = (np.log(vol_safe).diff(24).rolling(168, min_periods=72).std()).clip(0, 3)

    # ---------------- flow ----------------
    tbq = panel["taker_buy_quote"]
    signed = 2.0 * tbq - qv
    for w in cfg.flow_windows:
        num = signed.rolling(w, min_periods=max(1, w // 2)).sum()
        den = qv.rolling(w, min_periods=max(1, w // 2)).sum()
        F[f"flow_{w}"] = (num / (den + EPS)).clip(-1, 1)
    imb1 = (signed / (qv + EPS)).clip(-1, 1)
    F["flow_z_168"] = rolling_z(F["flow_24"], 168).clip(-5, 5)
    # Lag-1 autocorrelation of the bar imbalance (persistent flow = informed / meta-order splitting).
    F["flow_persist"] = (imb1 * imb1.shift(1)).rolling(72, min_periods=24).mean() / (
        (imb1**2).rolling(72, min_periods=24).mean() + EPS
    )
    # Flow that price has not followed (absorption): relative flow minus what the residual move implies.
    F["flow_ret_div_24"] = F["flow_24"].sub(F["flow_24"].where(mask).mean(axis=1), axis=0) - 0.1 * F["iret_24"].clip(
        -5, 5
    )

    # ---------------- activity & liquidity ----------------
    lqv = np.log(qv + 1.0)
    for w in (24, 168):
        F[f"dvol_surprise_{w}"] = (
            lqv.rolling(w // 4 or 1, min_periods=1).mean() - lqv.rolling(w * 4, min_periods=w).mean()
        ).clip(-5, 5)
    trades = panel["trades"]
    F["trade_size"] = np.log((qv.rolling(24, min_periods=6).sum() + 1) / (trades.rolling(24, min_periods=6).sum() + 1))
    F["amihud_168"] = np.log((r1.abs() / (qv + 1.0)).rolling(168, min_periods=48).mean() * 1e9 + 1e-6)
    F["log_dollar_volume"] = lqv.rolling(24 * 7, min_periods=24).mean()
    hl = np.log(high / low)
    F["hl_spread_proxy"] = np.log(hl.rolling(24, min_periods=6).median() / (vol_safe + EPS) + 1e-6).clip(-5, 5)
    rng_ = (high - low).replace(0, np.nan)
    F["clv_24"] = (((close - low) - (high - close)) / rng_).rolling(24, min_periods=6).mean().clip(-1, 1)

    # ---------------- carry & positioning ----------------
    if "funding_rate" in panel:
        fr = panel["funding_rate"]
        last_fr = fr.ffill(limit=bars_per_day * 2)
        F["funding_last"] = (last_fr * 1e4).clip(-100, 100)
        for w in cfg.funding_windows:
            F[f"funding_sum_{w}"] = (fr.fillna(0).rolling(w, min_periods=1).sum() * 1e4).clip(-300, 300)
        F["funding_z"] = rolling_z(last_fr, 24 * 30, min_periods=24 * 3).clip(-5, 5)
        F["funding_chg_24"] = ((last_fr - last_fr.shift(24)) * 1e4).clip(-100, 100)
    if "premium" in panel and panel["premium"].notna().any().any():
        pr = panel["premium"]
        F["premium"] = (pr * 1e4).clip(-200, 200)
        F["premium_ema_24"] = (pr.ewm(span=24, adjust=False, min_periods=6).mean() * 1e4).clip(-200, 200)
        F["premium_chg_8"] = ((pr - pr.shift(8)) * 1e4).clip(-200, 200)
        F["premium_z"] = rolling_z(pr, 24 * 14, min_periods=48).clip(-5, 5)
    if "oi_value" in panel and panel["oi_value"].notna().any().any():
        oi = np.log(panel["oi_value"].where(panel["oi_value"] > 0))
        for w in (8, 24, 72):
            F[f"oi_chg_{w}"] = (oi - oi.shift(w)).clip(-2, 2)
        F["oi_turnover"] = np.log((qv.rolling(24, min_periods=6).sum() + 1) / (panel["oi_value"] + 1)).clip(-10, 10)
        F["oi_price_24"] = F["oi_chg_24"] * np.sign(F["ret_24"])
    for name in ("ls_top", "ls_account"):
        if name in panel and panel[name].notna().any().any():
            x = np.log(panel[name].where(panel[name] > 0))
            F[f"{name}_z"] = rolling_z(x, 24 * 14, min_periods=48).clip(-5, 5)
            F[f"{name}_chg_24"] = (x - x.shift(24)).clip(-2, 2)

    # ---------------- cross-sectional transforms ----------------
    if cfg.cross_sectional:
        cs_sources = [
            "ret_1",
            "ret_4",
            "ret_24",
            "ret_72",
            "ret_168",
            "ret_336",
            "iret_24",
            "iret_168",
            "flow_4",
            "flow_24",
            "flow_72",
            "vol_cs",
            "dvol_surprise_24",
            "amihud_168",
            "beta",
            "funding_sum_24",
            "funding_sum_168",
            "premium",
            "premium_chg_8",
            "trend_24_96",
            "range_pos_168",
        ]
        for name in cs_sources:
            if name in F:
                F[f"cs_{name}"] = cs_rank_gauss(F[name], mask)
        F.pop("vol_cs", None)

    # ---------------- market state (shared) ----------------
    if cfg.market_features:
        member_r = r1.where(mask)
        for w in (1, 4, 24, 72, 168):
            mk = mkt.rolling(w, min_periods=1).sum()
            M[f"mkt_ret_{w}"] = (mk / (mkt_vol * np.sqrt(w) + EPS)).clip(-8, 8)
        M["mkt_vol"] = np.log(mkt_vol * np.sqrt(bars_per_day * 365) + 1e-8)
        M["mkt_vol_ratio"] = np.log(
            (np.sqrt((mkt**2).rolling(24, min_periods=12).mean()) + EPS) / (mkt_vol + EPS)
        ).clip(-3, 3)
        disp = member_r.std(axis=1)
        M["dispersion"] = np.log(disp.rolling(24, min_periods=6).mean() + 1e-8)
        M["breadth_24"] = (F["ret_24"].where(mask) > 0).mean(axis=1) - 0.5
        M["mkt_flow_24"] = F["flow_24"].where(mask).mean(axis=1)
        if "funding_sum_24" in F:
            M["mkt_funding_24"] = F["funding_sum_24"].where(mask).mean(axis=1)
        if "premium" in F:
            M["mkt_premium"] = F["premium"].where(mask).mean(axis=1)
        if "BTCUSDT" in close.columns:
            btc = r1["BTCUSDT"].fillna(0)
            M["btc_minus_mkt_24"] = ((btc - mkt).rolling(24, min_periods=6).sum() / (mkt_vol * np.sqrt(24) + EPS)).clip(
                -8, 8
            )
    if cfg.time_features:
        close_time = close.index + panel.bar_delta
        hour = close_time.hour + close_time.minute / 60.0
        dow = close_time.dayofweek
        M["hour_sin"] = pd.Series(np.sin(2 * np.pi * hour / 24), index=close.index)
        M["hour_cos"] = pd.Series(np.cos(2 * np.pi * hour / 24), index=close.index)
        M["dow_sin"] = pd.Series(np.sin(2 * np.pi * dow / 7), index=close.index)
        M["dow_cos"] = pd.Series(np.cos(2 * np.pi * dow / 7), index=close.index)
        M["to_funding"] = pd.Series((8 - (close_time.hour % 8)) % 8 / 8.0, index=close.index)

    frames = {k: v.astype("float32") for k, v in F.items()}
    market = {k: v.astype("float32") for k, v in M.items()}
    aux = {"vol": vol_safe, "ivol": ivol, "beta": beta, "mkt": mkt.to_frame("mkt"), "r1": r1}
    return FeatureSet(frames=frames, market=market, aux=aux)
