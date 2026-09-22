"""Synthetic perpetual-futures market with *known* planted predictability.

Used by the test-suite and the offline smoke run. It reproduces the stylised facts that matter for the
pipeline (volatility clustering, fat tails, a common market factor with heterogeneous betas, intraday
volume seasonality, staggered listings and a delisting, funding every 8h, a premium index) and plants
three weak signals whose strength is controlled by ``signal_strength``:

* **flow**: the taker-buy share leads the idiosyncratic drift (information in aggressive order flow);
* **carry**: crowded longs pay high funding and subsequently underperform;
* **reversal**: an Ornstein-Uhlenbeck pricing error makes recent idiosyncratic moves partially revert.

With ``signal_strength=0`` returns are unpredictable by construction; the validation layer must then refuse
to promote anything. With the default strength the planted information coefficient is small (a few
percent), i.e. the realistic regime where only honest statistics separate signal from noise.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hermes.data.panel import BAR_TO_OFFSET, Panel


def make_synthetic_panel(
    n_assets: int = 24,
    n_bars: int = 24 * 365,
    bar: str = "1h",
    seed: int = 0,
    signal_strength: float = 1.0,
    start: str = "2022-01-01",
    staggered_listings: bool = True,
) -> Panel:
    rng = np.random.default_rng(seed)
    per_day = {"15m": 96, "30m": 48, "1h": 24, "2h": 12, "4h": 6}[bar]
    scale = np.sqrt(24.0 / per_day)  # keep daily volatility independent of bar size
    T, N = n_bars, n_assets
    index = pd.date_range(start, periods=T, freq=BAR_TO_OFFSET[bar], tz="UTC")
    symbols = [f"S{i:02d}USDT" for i in range(N)]
    symbols[0] = "BTCUSDT"
    if N > 1:
        symbols[1] = "ETHUSDT"

    # --- market factor: GARCH(1,1) with Student-t(4) innovations -----------------------------------------
    sig_m = 0.006 * scale
    omega, alpha, beta_g = sig_m**2 * 0.02, 0.06, 0.92
    z = rng.standard_t(4, size=T) / np.sqrt(2.0)
    h = np.empty(T)
    m = np.empty(T)
    h[0] = sig_m**2
    for t in range(T):
        if t:
            h[t] = omega + alpha * m[t - 1] ** 2 + beta_g * h[t - 1]
        m[t] = np.sqrt(h[t]) * z[t]
    regime_vol = np.sqrt(h) / sig_m  # common volatility regime multiplier

    betas = np.clip(rng.normal(1.0, 0.25, N), 0.5, 1.7)
    betas[0] = 1.0
    idio_sig = rng.uniform(0.004, 0.010, N) * scale
    idio_sig[0] = 0.0015 * scale

    # --- latent alpha: persistent AR(1) drift, unit variance ---------------------------------------------
    hl = 24 * per_day / 24
    phi = 0.5 ** (1.0 / hl)
    a = np.zeros((T, N))
    innov = rng.standard_normal((T, N)) * np.sqrt(1 - phi**2)
    for t in range(1, T):
        a[t] = phi * a[t - 1] + innov[t]
    s = float(signal_strength)
    drift = 0.03 * s * a * idio_sig  # expected idiosyncratic return per bar

    eps = rng.standard_t(5, size=(T, N)) / np.sqrt(5.0 / 3.0) * idio_sig * regime_vol[:, None]
    # --- reversal: OU pricing error inside the log price ----------------------------------------------------
    kappa = 1.0 - 0.5 ** (1.0 / (6 * per_day / 24))
    d = np.zeros((T, N))
    shocks = rng.standard_normal((T, N)) * idio_sig * 0.25 * s * regime_vol[:, None]
    for t in range(1, T):
        d[t] = (1 - kappa) * d[t - 1] + shocks[t]
    ret_log = (
        betas[None, :] * m[:, None]
        + np.vstack([np.zeros((1, N)), drift[:-1]])
        + eps
        + np.diff(np.vstack([np.zeros((1, N)), d]), axis=0)
    )
    logp = np.cumsum(ret_log, axis=0) + np.log(rng.uniform(0.05, 500, N))[None, :]
    logp[:, 0] += np.log(30000) - logp[0, 0]
    close = np.exp(logp)
    open_ = np.vstack([close[:1], close[:-1]])
    wick = np.abs(rng.standard_normal((T, N))) * (idio_sig + sig_m) * regime_vol[:, None] * 0.6
    high = np.maximum(open_, close) * np.exp(wick * rng.uniform(0.2, 1.0, (T, N)))
    low = np.minimum(open_, close) * np.exp(-wick * rng.uniform(0.2, 1.0, (T, N)))

    # --- volume with intraday seasonality and |return| dependence -------------------------------------------
    hours = index.hour.to_numpy() + index.minute.to_numpy() / 60.0
    season = 0.35 * np.cos(2 * np.pi * (hours - 15) / 24)
    base_vol = np.log(rng.uniform(2e6, 2e8, N)) - np.log(per_day / 24)
    base_vol[0] = np.log(3e9) - np.log(per_day / 24)
    absz = np.abs(ret_log) / (idio_sig + betas * sig_m)[None, :]
    log_qv = base_vol[None, :] + season[:, None] + 0.45 * np.log(absz + 0.3) + 0.3 * rng.standard_normal((T, N))
    quote_volume = np.exp(log_qv)
    volume = quote_volume / close
    trades = np.maximum(1.0, quote_volume / rng.uniform(800, 3000, N)[None, :])

    # --- flow: taker-buy share leads the latent alpha -----------------------------------------------------
    rho_flow = 0.35 * min(s, 1.0) if s > 0 else 0.0
    flow_z = rho_flow * a + np.sqrt(1 - rho_flow**2) * rng.standard_normal((T, N))
    flow_z += 0.25 * ret_log / (idio_sig + sig_m)[None, :]  # contemporaneous pressure (not predictive)
    taker_share = np.clip(0.5 + 0.035 * flow_z, 0.05, 0.95)
    taker_buy_quote = taker_share * quote_volume

    # --- funding every 8h: crowded longs (negative alpha) pay more -----------------------------------------
    rho_carry = 0.30 * min(s, 1.0) if s > 0 else 0.0
    carry_z = -rho_carry * a + np.sqrt(1 - rho_carry**2) * rng.standard_normal((T, N))
    premium = 0.0001 + 0.0004 * carry_z + 0.0002 * rng.standard_normal((T, N))
    funding_rate = np.full((T, N), np.nan)
    close_times = index + pd.Timedelta(BAR_TO_OFFSET[bar])
    settle = (close_times.hour % 8 == 0) & (close_times.minute == 0)
    sm = pd.DataFrame(premium).rolling(max(1, 8 * per_day // 24), min_periods=1).mean().to_numpy()
    funding_rate[settle] = np.clip(sm[settle], -0.0075, 0.0075)

    oi_value = np.exp(
        np.log(quote_volume.mean(axis=0) * 5)[None, :] + np.cumsum(0.01 * rng.standard_normal((T, N)), axis=0)
    )

    fields = {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "quote_volume": quote_volume,
        "trades": trades,
        "taker_buy_quote": taker_buy_quote,
        "funding_rate": funding_rate,
        "premium": premium,
        "oi_value": oi_value,
    }
    frames = {k: pd.DataFrame(v, index=index, columns=symbols) for k, v in fields.items()}

    if staggered_listings and N >= 6:
        listing = rng.integers(0, T // 3, size=N)
        listing[:4] = 0
        delist_asset = N - 1
        for j in range(len(symbols)):
            if listing[j] > 0:
                for df in frames.values():
                    df.iloc[: listing[j], j] = np.nan
        cut = int(T * 0.8)
        for df in frames.values():
            df.iloc[cut:, delist_asset] = np.nan

    panel = Panel(frames, bar=bar)
    panel.meta.update(
        {
            "synthetic": True,
            "signal_strength": s,
            "latent_alpha": pd.DataFrame(a, index=index, columns=symbols),
            "betas": pd.Series(betas, index=symbols),
        }
    )
    return panel
