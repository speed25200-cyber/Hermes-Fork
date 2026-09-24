import glob, numpy as np, pandas as pd
M = pd.date_range('2022-01-01', '2026-08-31 23:59', freq='min', tz='UTC')
res = {}
for s in ['BTCUSDT', 'ETHUSDT']:
    for k in ['markPriceKlines', 'indexPriceKlines']:
        df = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(f'm1/{s}_{k}_*.parquet'))])
        df = df[~df.index.duplicated()].reindex(M)
        miss = df.c.isna()
        res[f'{s}_{k}'] = miss.values
        h = miss.resample('h').sum()
        bad = h[h > 0]
        print(s, k, 'missing min', int(miss.sum()), 'hours affected', len(bad), 'by month:', bad.groupby(bad.index.to_period('M')).size().to_dict())
np.savez_compressed('m1_missing.npz', **res)
