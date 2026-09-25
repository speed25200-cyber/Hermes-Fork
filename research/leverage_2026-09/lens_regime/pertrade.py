"""Per-trade signal-to-noise and the breadth needed to turn it into an annual Sharpe; trades per year available on
OKX. Short vs BTC from +d0 to +168h, no stop, no costs (event level), and after a flat 0.30%/round trip + funding.
-> pertrade.json"""
import json, numpy as np, pandas as pd
X = pd.read_parquet('events_d7.parquet')
out = {}
for d0 in (24, 72):
    for per, m in (('IS', X.t < '2025-01-01'), ('OOS', X.t >= '2025-01-01'), ('2025', X.t.dt.year == 2025), ('2026', X.t.dt.year == 2026), ('ALL', X.t.notna())):
        Y = X[(X.d0 == d0) & m]
        v = Y.short_btc.values
        w = np.clip(v, -0.5, None)          # a crude 50% loss cap
        yrs = {'IS': 3.0, 'OOS': 20 / 12, '2025': 1.0, '2026': 8 / 12, 'ALL': 4 + 8 / 12}[per]
        n_per_yr = len(v) / yrs
        o = dict(n=len(v), trades_per_year=n_per_yr, mean=float(v.mean()), sd=float(v.std(ddof=1)), sr_trade=float(v.mean() / v.std(ddof=1)),
                 median=float(np.median(v)), sr_trade_capped=float(w.mean() / w.std(ddof=1)),
                 boot_mean_ci90=[float(x) for x in np.quantile(np.random.default_rng(0).choice(v, (5000, len(v))).mean(1), [0.05, 0.95])])
        out[f'd0={d0}_{per}'] = o
        print(f"d0={d0} {per:4s} n={len(v):3d} ({n_per_yr:5.1f}/yr) mean {o['mean']:+.4f} sd {o['sd']:.3f} SR/trade {o['sr_trade']:.3f} (capped {o['sr_trade_capped']:.3f}) median {o['median']:+.3f} boot90 mean {o['boot_mean_ci90']}")
json.dump(out, open('pertrade.json', 'w'), indent=1)
