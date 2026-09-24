"""Align Binance 1m mark/index (BTC, ETH) to the hourly panel index: arrays (nH, 60, 2) for mark h/c and index h/c."""
import sys, glob
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry')
import carry_sim as cs, numpy as np, pandas as pd
P = cs.load(); idx = P['s_c'].index
M = pd.date_range(idx[0], idx[-1] + pd.Timedelta(minutes=59), freq='min', tz='UTC')
out = {}
for s in ['BTCUSDT', 'ETHUSDT']:
    for k, tag in [('markPriceKlines', 'mk'), ('indexPriceKlines', 'ix')]:
        df = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(f'm1/{s}_{k}_*.parquet'))])
        df = df[~df.index.duplicated()].reindex(M)
        miss = int(df.c.isna().sum())
        df['c'] = df.c.ffill(); df['h'] = df.h.fillna(df.c); df['l'] = df.l.fillna(df.c)
        print(s, tag, 'missing minutes (in 2022+ ok if only Dec-21):', miss, int(df.c['2022':].isna().sum()))
        for col in 'hlc':
            out[f'{tag}_{col}_{s}'] = df[col].values.astype('float32').reshape(len(idx), 60)
np.savez_compressed('m1_aligned.npz', **out)
print('saved')
