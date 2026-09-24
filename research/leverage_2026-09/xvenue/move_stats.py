"""How often does price move more than X within the collateral-transfer window?  (mark prices, Binance)
A venue at leverage L loses ~L*x of its equity on an adverse move x; with maintenance margin m it is liquidated
at x ~ 1/L - m.  Count distinct days with such a move inside a window of h hours (the time a transfer needs)."""
import numpy as np, pandas as pd
Z = np.load('data/panel.npz', allow_pickle=True)
coins = list(Z['coins']); time = pd.to_datetime(Z['time'].astype('int64'), unit='ns', utc=True)
c0, hi, lo = Z['bmc'], Z['bmh'], Z['bml']
rows = []
for h in [1, 5, 9, 25]:
    for name in ['BTC', 'ETH', 'SOL', 'DOGE', 'AVAX', 'alts_median']:
        per = {}
        idxs = [coins.index(name)] if name in coins else [i for i, c in enumerate(coins) if c not in ('BTC', 'ETH')]
        res = {x: [] for x in [0.025, 0.05, 0.0667, 0.10]}
        for i in idxs:
            s = pd.Series(c0[:, i], index=time)
            H = pd.Series(hi[:, i], index=time).rolling(h).max().shift(-h)
            Lw = pd.Series(lo[:, i], index=time).rolling(h).min().shift(-h)
            exc = np.maximum(H / s - 1, 1 - Lw / s)
            ok = s.notna() & exc.notna()
            yrs = ok.sum() / 8760
            for x in res:
                days = exc[ok & (exc > x)].index.floor('D').nunique()
                res[x].append(days / yrs if yrs > 0.3 else np.nan)
        for x, v in res.items():
            rows.append(dict(window_h=h, coin=name, move=x, days_per_year=np.nanmedian(v)))
df = pd.DataFrame(rows).pivot_table(index=['window_h', 'coin'], columns='move', values='days_per_year').round(1)
print('days per year with a mark-price excursion larger than X inside the window (2022-01..2026-08)')
print(df.to_string())
df.to_csv('results/move_stats.csv')
