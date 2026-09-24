# sanity: event counts per year in the IN-SAMPLE window only (no return information used)
import numpy as np, pandas as pd, time
from common import *
from signals import wick_events, sessions
for coin in ['BTC', 'SOL', 'DOGE']:
    d = load(coin)
    i0, i1 = window(d, IS0, IS1)
    ts = pd.to_datetime(d['t'], unit='ms')
    for w in (1, 5):
        for z in (6, 10):
            for V in (3, 6):
                idx, dr, mf = wick_events(d, w, z, V)
                m = (idx >= i0) & (idx < i1)
                print(coin, w, z, V, 'IS events/yr', round(m.sum() / 3, 1), 'median move %', round(100 * np.median(mf[m]), 2) if m.sum() else None, 'frac long', round((dr[m] == 1).mean(), 2) if m.sum() else None)
    print(coin, 'missing bars by month', pd.Series(d['miss'], index=ts).resample('ME').sum().loc[lambda x: x > 0].to_dict())
