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

from hermes.config import BAR_MINUTES, FeatureConfig, bars_for
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


def _uniq_bars(minutes: tuple[int, ...], bar: str) -> list[tuple[int, int]]:
    """(effective minutes, bars) pairs with duplicate bar counts removed.

    A window is rounded to whole bars (at least one); the name carries the minutes it really spans, so a
    15-minute window on 30-minute bars becomes ``..._30m`` and merges with an explicit 30-minute window.
    """
    out, seen = [], set()
    for m in minutes:
        b = bars_for(m, bar)
        if b not in seen:
            seen.add(b)
            out.append((b * BAR_MINUTES[bar], b))
    return out


FUNDING_INTERVALS = np.array([1.0, 2.0, 4.0, 8.0])


def funding_interval_hours(funding_rate: pd.DataFrame, bars_per_day: int) -> pd.DataFrame:
    """Settlement interval of each contract, in hours (1, 2, 4 or 8), from the settlements seen so far.

    ``funding_rate`` holds one value per settlement (NaN elsewhere). At each settlement the gap to the previous
    one is measured; the interval is the smallest gap of the last 24 hours (a missing settlement does not double
    it), snapped down to 1/2/4/8 h and carried forward. 8 h before the first gap is known (Binance's default).
    """
    hours = ((funding_rate.index - funding_rate.index[0]).total_seconds() / 3600.0).to_numpy()
    t = pd.DataFrame(
        np.where(funding_rate.notna().to_numpy(), hours[:, None], np.nan),
        index=funding_rate.index,
        columns=funding_rate.columns,
    )
    gap = (t - t.ffill().shift(1)).where(funding_rate.notna())
    g = gap.rolling(bars_per_day, min_periods=1).min().where(funding_rate.notna()).to_numpy()
    k = np.searchsorted(FUNDING_INTERVALS, np.where(np.isfinite(g), g, 8.0) + 1e-6, side="right") - 1
    snapped = np.where(np.isfinite(g), FUNDING_INTERVALS[np.clip(k, 0, 3)], np.nan)
    out = pd.DataFrame(snapped, index=funding_rate.index, columns=funding_rate.columns)
    return out.ffill().fillna(8.0)


def build_features(panel: Panel, mask: pd.DataFrame, cfg: FeatureConfig) -> FeatureSet:
    """Compute the full feature set for the panel's timeframe. ``mask`` (universe membership) is only used
    for cross-sectional statistics, so that the market and the ranks are defined over what was tradable.

    Every window is defined in minutes (``FeatureConfig``) and converted to bars; feature names carry the
    minutes (``ret_60m``), so a name means the same thing whatever the bar size."""
    bar = panel.bar
    B = lambda minutes: bars_for(minutes, bar)  # noqa: E731
    bars_per_day = 1440 // BAR_MINUTES[bar]
    # Storage may be float32; every computation runs in float64.
    close = panel["close"].astype("float64")
    high, low, open_ = (panel[k].astype("float64") for k in ("high", "low", "open"))
    qv = panel["quote_volume"].astype("float64")
    logc = np.log(close)
    r1 = logc.diff()
    hl_vol = max(2, B(cfg.vol_halflife_minutes))
    vol = ewm_vol(r1, hl_vol)
    vol_safe = vol.where(vol > 0).ffill()
    day, week = B(cfg.day_minutes), B(cfg.long_minutes)

    F: dict[str, pd.DataFrame] = {}
    M: dict[str, pd.Series] = {}

    # ---------------- market factor & residuals ----------------
    mkt = market_return(r1, mask).fillna(0.0)
    beta = rolling_beta(r1, mkt, halflife=hl_vol * 2)
    resid1 = r1 - beta.mul(mkt, axis=0)
    ivol = ewm_vol(resid1, hl_vol).where(lambda v: v > 0).ffill()
    mkt_vol = np.sqrt((mkt**2).ewm(halflife=hl_vol, min_periods=min(24, hl_vol), adjust=False).mean())

    # ---------------- price: momentum / reversal ----------------
    cum_resid = resid1.fillna(0.0).cumsum().where(close.notna())
    ret_names: dict[int, str] = {}
    for m, w in _uniq_bars(cfg.return_minutes, bar):
        rw = logc - logc.shift(w)
        F[f"ret_{m}m"] = (rw / (vol_safe * np.sqrt(w))).clip(-8, 8)
        F[f"iret_{m}m"] = ((cum_resid - cum_resid.shift(w)) / (ivol * np.sqrt(w))).clip(-8, 8)
        ret_names[m] = f"ret_{m}m"
    bm = BAR_MINUTES[bar]
    for fm, sm in cfg.trend_pairs_minutes:
        fast, slow = B(fm), B(sm)
        name = f"trend_{fast * bm}m_{slow * bm}m"
        if slow <= fast or name in F:
            continue
        ema_f = logc.ewm(span=fast, adjust=False, min_periods=fast).mean()
        ema_s = logc.ewm(span=slow, adjust=False, min_periods=slow).mean()
        F[name] = ((ema_f - ema_s) / (vol_safe * np.sqrt(slow))).clip(-8, 8)
    for m, w in _uniq_bars(cfg.range_minutes, bar):
        hh = high.rolling(w, min_periods=max(1, w // 2)).max()
        ll = low.rolling(w, min_periods=max(1, w // 2)).min()
        F[f"range_pos_{m}m"] = ((close - ll) / (hh - ll + EPS)).clip(0, 1) - 0.5
        F[f"dist_high_{m}m"] = (np.log(hh / close) / (vol_safe * np.sqrt(w))).clip(0, 10)
        F[f"dist_low_{m}m"] = (np.log(close / ll) / (vol_safe * np.sqrt(w))).clip(0, 10)

    # ---------------- risk ----------------
    park = (np.log(high / low) ** 2) / (4 * np.log(2))
    gk = 0.5 * np.log(high / low) ** 2 - (2 * np.log(2) - 1) * np.log(close / open_) ** 2
    for m, w in _uniq_bars(cfg.vol_minutes, bar):
        mp = min(w, max(2, w // 2))
        rv = np.sqrt((r1**2).rolling(w, min_periods=mp).mean())
        F[f"rv_ratio_{m}m"] = np.log((rv + EPS) / (vol_safe + EPS)).clip(-3, 3)
        F[f"park_ratio_{m}m"] = np.log((np.sqrt(park.rolling(w, min_periods=mp).mean()) + EPS) / (vol_safe + EPS)).clip(
            -3, 3
        )
    F["gk_vol_day"] = np.log(np.sqrt(gk.clip(lower=0).rolling(day, min_periods=max(2, day // 2)).mean()) + 1e-8)
    F["vol_level"] = np.log(vol_safe * np.sqrt(bars_per_day * 365) + 1e-8)
    F["ivol_share"] = (ivol / (vol_safe + EPS)).clip(0, 3)
    F["beta"] = beta
    mp_w = max(3, week // 3)
    up = (r1.clip(lower=0) ** 2).rolling(week, min_periods=mp_w).sum()
    dn = (r1.clip(upper=0) ** 2).rolling(week, min_periods=mp_w).sum()
    F["semivar_asym"] = ((up - dn) / (up + dn + EPS)).clip(-1, 1)
    F["skew_long"] = r1.rolling(week, min_periods=mp_w).skew().clip(-5, 5)
    F["kurt_long"] = np.log1p(r1.rolling(week, min_periods=mp_w).kurt().clip(-2, 50) + 2)
    F["volvol_long"] = (np.log(vol_safe).diff(day).rolling(week, min_periods=mp_w).std()).clip(0, 3)

    # ---------------- flow ----------------
    tbq = panel["taker_buy_quote"].astype("float64")
    signed = 2.0 * tbq - qv
    flow_names = []
    for m, w in _uniq_bars(cfg.flow_minutes, bar):
        num = signed.rolling(w, min_periods=max(1, w // 2)).sum()
        den = qv.rolling(w, min_periods=max(1, w // 2)).sum()
        F[f"flow_{m}m"] = (num / (den + EPS)).clip(-1, 1)
        flow_names.append(f"flow_{m}m")
    imb1 = (signed / (qv + EPS)).clip(-1, 1)
    flow_day = F[flow_names[-1]]
    F["flow_z_long"] = rolling_z(flow_day, week).clip(-5, 5)
    # Lag-1 autocorrelation of the bar imbalance (persistent flow = informed / meta-order splitting).
    pw = max(8, day)
    F["flow_persist"] = (imb1 * imb1.shift(1)).rolling(pw, min_periods=pw // 3).mean() / (
        (imb1**2).rolling(pw, min_periods=pw // 3).mean() + EPS
    )
    # Flow that price has not followed (absorption): relative flow minus what the residual move implies.
    iret_day = F[f"i{ret_names[max(m for m in ret_names if B(m) <= day)]}"]
    F["flow_ret_div"] = flow_day.sub(flow_day.where(mask).mean(axis=1), axis=0) - 0.1 * iret_day.clip(-5, 5)

    # ---------------- activity & liquidity ----------------
    lqv = np.log(qv + 1.0)
    for name, w in (("dvol_surprise_day", day), ("dvol_surprise_long", week)):
        F[name] = (lqv.rolling(max(1, w // 4), min_periods=1).mean() - lqv.rolling(w * 4, min_periods=w).mean()).clip(
            -5, 5
        )
    trades = panel["trades"].astype("float64")
    mpd = max(2, day // 4)
    F["trade_size"] = np.log(
        (qv.rolling(day, min_periods=mpd).sum() + 1) / (trades.rolling(day, min_periods=mpd).sum() + 1)
    )
    F["amihud_long"] = np.log((r1.abs() / (qv + 1.0)).rolling(week, min_periods=mp_w).mean() * 1e9 + 1e-6)
    F["log_dollar_volume"] = np.log(qv.rolling(week, min_periods=day).sum() / max(week / bars_per_day, 1e-9) + 1.0)
    hl = np.log(high / low)
    F["hl_spread_proxy"] = np.log(hl.rolling(day, min_periods=mpd).median() / (vol_safe + EPS) + 1e-6).clip(-5, 5)
    rng_ = (high - low).replace(0, np.nan)
    F["clv_day"] = (((close - low) - (high - close)) / rng_).rolling(day, min_periods=mpd).mean().clip(-1, 1)

    # ---------------- intrabar microstructure (1-minute aggregates) ----------------
    if cfg.intrabar and "ib_rv" in panel and panel["ib_rv"].notna().any().any():
        bar_var = (vol_safe**2).clip(lower=1e-12)
        ib_rv = panel["ib_rv"]
        F["ib_rv_ratio"] = np.log((ib_rv + 1e-12) / bar_var).clip(-5, 5)
        F["ib_jump"] = ((ib_rv - panel["ib_bv"]) / (ib_rv + 1e-12)).clip(-1, 1)
        F["ib_rskew"] = panel["ib_rskew"].clip(-5, 5)
        F["ib_flow_last"] = panel["ib_flow_last"].clip(-1, 1)
        F["ib_flow_std"] = panel["ib_flow_std"].clip(0, 2)
        F["ib_vwap_dev"] = (np.log(close / panel["ib_vwap"]) / vol_safe).clip(-8, 8)
        F["ib_upfrac"] = panel["ib_upfrac"] - 0.5
        F["ib_rv_ratio_day"] = F["ib_rv_ratio"].rolling(day, min_periods=mpd).mean()
        F["ib_rskew_day"] = F["ib_rskew"].rolling(day, min_periods=mpd).mean()

    # ---------------- carry & positioning ----------------
    interval = None
    if "funding_rate" in panel:
        fr = panel["funding_rate"]
        last_fr = fr.ffill(limit=bars_per_day * 2)
        if cfg.funding_per_8h:
            # Since late 2023 Binance settles more and more contracts every 4 h (or 1-2 h): the last settled rate
            # is put on an 8-hour basis so it means the same thing across contracts and years. Sums over a
            # window stay per settlement: they are the cash actually paid.
            interval = funding_interval_hours(fr, bars_per_day).astype("float64")
            last_fr = last_fr * (8.0 / interval)
            F["funding_short_interval"] = (interval < 8.0).astype("float64")
        F["funding_last"] = (last_fr * 1e4).clip(-100, 100)
        for m, w in _uniq_bars(cfg.funding_minutes, bar):
            F[f"funding_sum_{m}m"] = (fr.fillna(0).rolling(w, min_periods=1).sum() * 1e4).clip(-300, 300)
        zw = B(cfg.zscore_minutes)
        F["funding_z"] = rolling_z(last_fr, zw, min_periods=max(10, zw // 10)).clip(-5, 5)
        F["funding_chg_day"] = ((last_fr - last_fr.shift(day)) * 1e4).clip(-100, 100)
    if "premium" in panel and panel["premium"].notna().any().any():
        pr = panel["premium"]
        zw = B(cfg.zscore_minutes)
        F["premium"] = (pr * 1e4).clip(-200, 200)
        F["premium_ema_day"] = (pr.ewm(span=day, adjust=False, min_periods=max(1, day // 4)).mean() * 1e4).clip(
            -200, 200
        )
        F["premium_chg_8h"] = ((pr - pr.shift(B(480))) * 1e4).clip(-200, 200)
        F["premium_z"] = rolling_z(pr, zw, min_periods=max(10, zw // 10)).clip(-5, 5)

    # Positioning (Binance "metrics": open interest, long/short ratios). Read one bar late: the live API publishes
    # a snapshot some time after it is taken. Z-scores over at most 28 days: the live API serves 30.
    def positioning(name: str) -> pd.DataFrame | None:
        if name not in panel or not panel[name].notna().any().any():
            return None
        x = panel[name].astype("float64")
        return x.where(x > 0).ffill(limit=4).shift(1)

    pos_zw = min(B(cfg.zscore_minutes), 28 * bars_per_day)
    oi_value = positioning("oi_value") if "oi" in cfg.positioning else None
    if oi_value is not None:
        oi = np.log(oi_value)
        for m in (480, 1440, 4320):
            F[f"oi_chg_{m}m"] = (oi - oi.shift(B(m))).clip(-2, 2)
        F["oi_turnover"] = np.log((qv.rolling(day, min_periods=mpd).sum() + 1) / (oi_value + 1)).clip(-10, 10)
        F["oi_price_day"] = F["oi_chg_1440m"] * np.sign(F[ret_names[max(m for m in ret_names if B(m) <= day)]])
    for name in ("ls_top", "ls_account"):
        ratio = positioning(name) if name in cfg.positioning else None
        if ratio is not None:
            x = np.log(ratio)
            F[f"{name}_z"] = rolling_z(x, pos_zw, min_periods=max(10, pos_zw // 10)).clip(-5, 5)
            F[f"{name}_chg_day"] = (x - x.shift(day)).clip(-2, 2)

    # ---------------- cross-sectional transforms ----------------
    if cfg.cross_sectional:
        cs_sources = [n for n in F if n.startswith(("ret_", "flow_"))]
        cs_sources += [n for n in F if n.startswith("iret_")][-3:]
        cs_sources += [n for n in F if n.startswith("trend_")][1:2] + [n for n in F if n.startswith("range_pos_")][-1:]
        cs_sources += [n for n in F if n.startswith("funding_sum_")]
        cs_sources += [
            "vol_level",
            "dvol_surprise_day",
            "amihud_long",
            "beta",
            "premium",
            "premium_chg_8h",
            "ib_rv_ratio",
            "ib_flow_last",
            "ib_vwap_dev",
            "ls_top_z",
        ]
        for name in dict.fromkeys(cs_sources):
            if name in F and not name.startswith("flow_ret"):
                F[f"cs_{name}"] = cs_rank_gauss(F[name], mask)

    # ---------------- market state (shared) ----------------
    if cfg.market_features:
        member_r = r1.where(mask)
        for m, w in _uniq_bars((BAR_MINUTES[bar], 60, 240, 1440, 4320, 10080), bar):
            mk = mkt.rolling(w, min_periods=1).sum()
            M[f"mkt_ret_{m}m"] = (mk / (mkt_vol * np.sqrt(w) + EPS)).clip(-8, 8)
        M["mkt_vol"] = np.log(mkt_vol * np.sqrt(bars_per_day * 365) + 1e-8)
        M["mkt_vol_ratio"] = np.log(
            (np.sqrt((mkt**2).rolling(day, min_periods=max(2, day // 2)).mean()) + EPS) / (mkt_vol + EPS)
        ).clip(-3, 3)
        disp = member_r.std(axis=1)
        M["dispersion"] = np.log(disp.rolling(day, min_periods=mpd).mean() + 1e-8)
        ret_day = F[ret_names[max(m for m in ret_names if B(m) <= day)]]
        # Share of *members* up on the day: averaged over members only (the panel's column set differs between
        # research chunks and the live candidate list, and must not change the scale).
        up = (ret_day > 0).astype("float64").where(mask & ret_day.notna())
        M["breadth_day"] = up.mean(axis=1) - 0.5
        M["mkt_flow_day"] = flow_day.where(mask).mean(axis=1)
        fs = [n for n in F if n.startswith("funding_sum_")]
        if fs:
            M["mkt_funding"] = F[fs[min(1, len(fs) - 1)]].where(mask).mean(axis=1)
        if "premium" in F:
            M["mkt_premium"] = F["premium"].where(mask).mean(axis=1)
        if "BTCUSDT" in close.columns:
            btc = r1["BTCUSDT"].fillna(0)
            M["btc_minus_mkt_day"] = (
                (btc - mkt).rolling(day, min_periods=mpd).sum() / (mkt_vol * np.sqrt(day) + EPS)
            ).clip(-8, 8)
            b4 = B(240)
            M["btc_minus_mkt_4h"] = ((btc - mkt).rolling(b4, min_periods=1).sum() / (mkt_vol * np.sqrt(b4) + EPS)).clip(
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
        if interval is None:
            M["to_funding"] = pd.Series(((8 - np.mod(hour, 8)) % 8) / 8.0, index=close.index)
        else:  # each contract's own settlement clock (settlements fall on UTC multiples of the interval)
            h = np.asarray(hour, dtype="float64")[:, None]
            iv = interval.to_numpy()
            F["to_funding"] = pd.DataFrame(
                np.mod(iv - np.mod(h, iv), iv) / iv, index=close.index, columns=close.columns
            )

    frames = {k: v.astype("float32") for k, v in F.items()}
    market = {k: v.astype("float32") for k, v in M.items()}
    aux = {
        "vol": vol_safe,
        "ivol": ivol,
        "beta": beta,
        "mkt": mkt.to_frame("mkt"),
        "r1": r1,
        "mkt_vol": mkt_vol.to_frame("mkt_vol"),
    }
    return FeatureSet(frames=frames, market=market, aux=aux)
