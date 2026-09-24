"""Validate the 5m synchronous-mark approximation on extreme days using 1m mark klines (daily Binance archive)."""
import io, zipfile, ssl, urllib.request, pandas as pd, numpy as np
ctx = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def mk1m(sym, day):
    u = f'https://data.binance.vision/data/futures/um/daily/markPriceKlines/{sym}/1m/{sym}-1m-{day}.zip'
    try: b = urllib.request.urlopen(u, context=ctx, timeout=60).read()
    except Exception as e: return None
    z = zipfile.ZipFile(io.BytesIO(b)); df = pd.read_csv(z.open(z.namelist()[0]), header=None, dtype=str)
    df = df[pd.to_numeric(df[0], errors='coerce').notna()].iloc[:, :5].astype(float); df.columns = ['t', 'o', 'h', 'l', 'c']
    df.index = pd.to_datetime(df.t.astype('int64'), unit='ms'); return df
cases = [('BTC', '221230', '2022-11-10'), ('ETH', '221230', '2022-11-10'), ('BTC', '240329', '2023-10-23'), ('BTC', '240329', '2023-10-24'),
         ('BTC', '240628', '2024-04-08'), ('ETH', '240628', '2024-04-08'), ('BTC', '240628', '2024-04-13'), ('ETH', '240628', '2024-04-13'),
         ('BTC', '250627', '2025-04-09'), ('ETH', '250627', '2025-05-08')]
rows = []
for a, c, day in cases:
    F = mk1m(f'{a}USDT_{c}', day); P = mk1m(f'{a}USDT', day)
    if F is None or P is None: print('missing', a, c, day); continue
    j = F.join(P, lsuffix='F', rsuffix='P', how='inner')
    ref = j.cP
    s1 = pd.concat([(j['o' + 'F'] - j['oP']), (j.hF - j.hP), (j.lF - j.lP), (j.cF - j.cP)], axis=1).div(ref, axis=0) * 100   # 1m synchronous candidates
    stress1 = (j.hF - j.lP) / ref * 100
    # 5m aggregation and the same synchronous candidates used in the backtest
    g = j.resample('5min').agg({'oF': 'first', 'hF': 'max', 'lF': 'min', 'cF': 'last', 'oP': 'first', 'hP': 'max', 'lP': 'min', 'cP': 'last'})
    s5 = pd.concat([(g.oF - g.oP), (g.hF - g.hP), (g.lF - g.lP), (g.cF - g.cP)], axis=1).div(g.cP, axis=0) * 100
    stress5 = (g.hF - g.lP) / g.cP * 100
    rows.append(dict(asset=a, contract=c, day=day, spread_max_1m_sync=s1.max().max(), spread_max_5m_sync=s5.max().max(),
                     spread_min_1m_sync=s1.min().min(), spread_min_5m_sync=s5.min().min(), stress_max_1m=stress1.max(), stress_max_5m=stress5.max()))
r = pd.DataFrame(rows); print(r.round(3).to_string()); r.to_csv('check_1m.csv', index=False)
