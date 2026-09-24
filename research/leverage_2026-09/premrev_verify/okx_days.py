"""Pick the UTC days to scan on OKX: union of (a) days with a Binance BTC/ETH basis deviation >= 40 bp from its
trailing 1-day median (any minute), (b) the 60 most volatile days per coin (Binance spot daily high-low range)."""
import numpy as np, pandas as pd, json
from common import load, T0, minute_of
out = {}
for c in ['BTCUSDT', 'ETHUSDT']:
    X = load(c)
    ok = X['s_ok'] & X['f_ok'] & np.isfinite(X['b_c'])
    b = pd.Series(np.where(ok, X['b_c'], np.nan))
    med = b.shift(1).rolling(1440, min_periods=720).median()
    dev = (b - med).abs().values
    idx = T0 + pd.to_timedelta(np.arange(len(dev)), unit='min')
    s = pd.Series(dev, index=idx)
    s = s[(s.index >= '2022-01-01') & (s.index < '2026-09-01')]
    ev_days = set(s[s >= 40e-4].index.floor('D').strftime('%Y-%m-%d'))
    sc = pd.Series(X['s_c'], index=idx)
    hi = pd.Series(X['s_c'] * (1 + np.nan_to_num(X['s_h'])), index=idx); lo = pd.Series(X['s_c'] * (1 + np.nan_to_num(X['s_l'])), index=idx)
    rng = ((hi.resample('D').max() - lo.resample('D').min()) / sc.resample('D').first())
    rng = rng[(rng.index >= '2022-01-01') & (rng.index < '2026-09-01')]
    top = set(rng.sort_values(ascending=False).head(60).index.strftime('%Y-%m-%d'))
    days = sorted(ev_days | top)
    print(c, 'event days', len(ev_days), 'top-range days', len(top), 'union', len(days))
    out[c] = days
json.dump(out, open('out/okx_scan_days.json', 'w'))
