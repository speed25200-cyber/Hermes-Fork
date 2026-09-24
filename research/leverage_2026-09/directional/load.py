import pandas as pd, numpy as np, zipfile, glob, os, io
D = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/directional/data'
def read_zip(p, ncols_names):
    with zipfile.ZipFile(p) as z:
        raw = z.read(z.namelist()[0]).decode()
    first = raw.split('\n', 1)[0]
    hdr = 0 if not first[0].isdigit() else None
    df = pd.read_csv(io.StringIO(raw), header=hdr)
    if hdr is None:
        df.columns = ncols_names[:df.shape[1]]
    return df
KC = ['open_time','open','high','low','close','volume','close_time','quote_volume','count','taker_buy_volume','taker_buy_quote_volume','ignore']
def build(sym):
    k = pd.concat([read_zip(p, KC) for p in sorted(glob.glob(f'{D}/data_futures_um_monthly_klines_{sym}_5m_*.zip'))])
    m = pd.concat([read_zip(p, KC) for p in sorted(glob.glob(f'{D}/data_futures_um_monthly_markPriceKlines_{sym}_5m_*.zip'))])
    f = pd.concat([read_zip(p, ['calc_time','funding_interval_hours','last_funding_rate']) for p in sorted(glob.glob(f'{D}/data_futures_um_monthly_fundingRate_{sym}_*.zip'))])
    k = k.drop_duplicates('open_time').set_index('open_time').sort_index()
    m = m.drop_duplicates('open_time').set_index('open_time').sort_index()
    idx = np.arange(k.index.min(), k.index.max() + 1, 300000)
    print(sym, 'kline rows', len(k), 'expected', len(idx), 'missing', len(set(idx) - set(k.index)))
    k = k.reindex(idx)
    miss = k['close'].isna()
    k['close'] = k['close'].ffill()
    for c in ['open','high','low']:
        k[c] = k[c].fillna(k['close'])
    k['volume'] = k['volume'].fillna(0)
    m = m.reindex(idx)
    print(sym, 'mark missing', m['close'].isna().sum())
    mm = m['close'].isna()
    ts_m = pd.to_datetime(pd.Series(idx[mm.values]), unit='ms')
    print(sym, 'mark-missing by month', ts_m.dt.to_period('M').value_counts().sort_index().to_dict())
    # missing mark bars: fall back to last-price OHLC for that bar (handled below)
    # sanity: mark extremes should not be crazy vs last
    out = pd.DataFrame({'t': idx, 'o': k['open'].values, 'h': k['high'].values, 'l': k['low'].values, 'c': k['close'].values,
                        'v': k['volume'].values, 'mo': m['open'].values, 'mh': m['high'].values, 'ml': m['low'].values, 'mc': m['close'].values})
    # some mark klines may be missing - fall back to last price
    for a, b in [('mo','o'),('mh','h'),('ml','l'),('mc','c')]:
        out[a] = out[a].fillna(out[b])
    # funding: map each funding event to the 5m bar that starts at the funding time (rounded down to 5m)
    f['t'] = (f['calc_time'] // 300000) * 300000
    f = f.drop_duplicates('t')
    fr = pd.Series(f['last_funding_rate'].values, index=f['t'].values)
    out['fund'] = out['t'].map(fr).fillna(0.0).values
    out['fund_flag'] = out['t'].isin(fr.index).astype(np.int8)
    out.to_parquet(f'{D}/../{sym}_5m.parquet')
    print(sym, out.shape, pd.to_datetime(out.t.iloc[0], unit='ms'), pd.to_datetime(out.t.iloc[-1], unit='ms'),
          'funding events', out.fund_flag.sum())
    # annualised funding by year
    ts = pd.to_datetime(out.t, unit='ms')
    print(out.groupby(ts.dt.year)['fund'].sum() * 365 / out.groupby(ts.dt.year)['fund'].size().div(288))
for s in ['BTCUSDT', 'ETHUSDT']:
    build(s)
