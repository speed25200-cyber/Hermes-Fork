"""OKX transferability check: OKX dated futures premium vs Binance quarterly premium on overlapping live contracts;
OKX vs Binance funding. Only public endpoints."""
import json, ssl, time, urllib.request, pandas as pd, numpy as np
ctx = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def get(path):
    for i in range(5):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request('https://www.okx.com' + path, headers={'User-Agent': 'curl/8.5.0'}), context=ctx, timeout=30).read())
        except Exception as e:
            time.sleep(1 + i)
    raise RuntimeError(path)
def candles(inst, bar='1H', start='2026-03-01', ep='history-candles'):
    out = []; after = ''
    t0 = pd.Timestamp(start).value // 10**6
    while True:
        d = get(f'/api/v5/market/{ep}?instId={inst}&bar={bar}&limit=100' + (f'&after={after}' if after else ''))['data']
        if not d: break
        out += d; after = d[-1][0]
        if int(after) < t0: break
        time.sleep(0.12)
    df = pd.DataFrame(out).iloc[:, :5]; df.columns = ['t', 'o', 'h', 'l', 'c']
    df.index = pd.to_datetime(df.t.astype('int64'), unit='ms'); df = df.drop(columns='t').astype(float).sort_index()
    return df[~df.index.duplicated()]
def funding(inst):
    out = []; after = ''
    while True:
        d = get(f'/api/v5/public/funding-rate-history?instId={inst}&limit=100' + (f'&after={after}' if after else ''))['data']
        if not d: break
        out += d; after = d[-1]['fundingTime']; time.sleep(0.12)
        if len(out) > 2000: break
    f = pd.DataFrame(out); f.index = pd.to_datetime(f.fundingTime.astype('int64'), unit='ms')
    return f.realizedRate.replace('', np.nan).astype(float).fillna(f.fundingRate.astype(float)).sort_index()
res = {}
okx = {}
for inst in ['BTC-USDT-SWAP', 'BTC-USD-261225', 'BTC-USD-260925', 'BTC-USD_UM-261225', 'ETH-USDT-SWAP', 'ETH-USD-261225', 'ETH-USD-260925']:
    okx[inst] = candles(inst)
    print(inst, len(okx[inst]), okx[inst].index[0], okx[inst].index[-1], flush=True)
bn = {}
for n in ['BTCUSDT_perp_last', 'BTCUSDT_261225_last', 'BTCUSDT_260925_last', 'ETHUSDT_perp_last', 'ETHUSDT_261225_last', 'ETHUSDT_260925_last']:
    d = pd.read_parquet(f'data/{n}.parquet'); d.index = pd.to_datetime(d.t, unit='ms'); bn[n] = d.c.resample('1h').last()
rows = []
for a, (swap, perp) in {'BTC': ('BTC-USDT-SWAP', 'BTCUSDT_perp_last'), 'ETH': ('ETH-USDT-SWAP', 'ETHUSDT_perp_last')}.items():
    for exp in ['261225', '260925']:
        o = okx[f'{a}-USD-{exp}'].c; os_ = okx[swap].c
        b = bn[f'{a}USDT_{exp}_last']; bp = bn[perp]
        # OKX hourly candles are labelled by open time: use close of the hour -> compare with Binance last 5m close of the same hour
        j = pd.DataFrame({'okx_prem': o / os_ - 1, 'bn_prem': b / bp - 1}).dropna()
        j = j[j.index < '2026-09-01']
        tau = ((pd.Timestamp('20' + exp) + pd.Timedelta(hours=8)) - j.index).total_seconds() / 86400
        j['okx_ann'] = j.okx_prem * 365 / tau; j['bn_ann'] = j.bn_prem * 365 / tau
        j = j[tau > 14]
        d = (j.okx_ann - j.bn_ann)
        rows.append(dict(asset=a, contract=exp, hours=len(j), start=str(j.index[0]), okx_ann_mean=j.okx_ann.mean(), bn_ann_mean=j.bn_ann.mean(),
                         diff_mean=d.mean(), diff_std=d.std(), corr=j.okx_ann.corr(j.bn_ann),
                         okx_dprem_std=j.okx_prem.diff().std(), bn_dprem_std=j.bn_prem.diff().std()))
cmp = pd.DataFrame(rows); print(cmp.round(4).to_string())
fr = {}
for a in ['BTC', 'ETH']:
    fo = funding(f'{a}-USDT-SWAP')
    fb = pd.read_parquet(f'data/{a}USDT_funding.parquet'); fb.index = pd.DatetimeIndex(pd.to_datetime(fb.t, unit='ms')).floor('h'); fb = fb.rate
    fo.index = fo.index.floor('h')
    j = pd.DataFrame({'okx': fo, 'bn': fb}).dropna()
    fr[a] = dict(n=len(j), start=str(j.index[0]), okx_ann=j.okx.mean() * 3 * 365, bn_ann=j.bn.mean() * 3 * 365, corr=j.okx.corr(j.bn),
                 okx_interval_h=float(pd.Series(fo.index).diff().median().total_seconds() / 3600))
    print(a, fr[a])
delv = get('/api/v5/public/delivery-exercise-history?instType=FUTURES&uly=BTC-USD&limit=100')['data']
dl = [(pd.to_datetime(int(x['ts']), unit='ms'), y['insId'], float(y['px'])) for x in delv for y in x['details']]
print('OKX BTC-USD deliveries:', dl[:12])
json.dump(dict(premium_compare=rows, funding_compare=fr, okx_btc_usd_deliveries=[(str(a), b, c) for a, b, c in dl]), open('okx_check.json', 'w'), indent=1, default=str)
