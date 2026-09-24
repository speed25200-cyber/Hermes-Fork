"""Download Binance USDT-M perp 1m klines + 1m mark-price klines + funding (monthly archives, data.binance.vision),
2021-12 .. 2026-08, for the study universe, parse in memory and store compact per-coin arrays in data/ (RAM tmpfs,
because the scratch disk is full). Missing minutes are filled flat with the previous close (volume 0, flag=1)."""
import io, os, ssl, sys, time, zipfile, urllib.request, concurrent.futures as cf
import numpy as np, pandas as pd
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
BASE = 'https://data.binance.vision/'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
COINS = sys.argv[1:] or ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'AVAX', 'LINK']
months = []
y, m = 2021, 12
while (y, m) <= (2026, 8):
    months.append(f'{y}-{m:02d}'); m += 1
    if m == 13: y, m = y + 1, 1
KC = ['open_time','open','high','low','close','volume','close_time','quote_volume','count','taker_buy_volume','taker_buy_quote_volume','ignore']
def fetch(path):
    for k in range(5):
        try:
            with urllib.request.urlopen(BASE + path, context=CTX, timeout=90) as r:
                return r.read()
        except Exception as e:
            err = e; time.sleep(2 + 3 * k)
    raise RuntimeError(f'{path}: {err}')
def parse(b, names):
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        raw = z.read(z.namelist()[0])
    first = raw[:40].split(b'\n', 1)[0]
    hdr = None if first[:1].isdigit() else 0
    df = pd.read_csv(io.BytesIO(raw), header=hdr)
    if hdr is None: df.columns = names[:df.shape[1]]
    return df
def job(args):
    kind, sym, mo = args
    if kind == 'k':
        return args, parse(fetch(f'data/futures/um/monthly/klines/{sym}/1m/{sym}-1m-{mo}.zip'), KC)[['open_time','open','high','low','close','quote_volume','taker_buy_quote_volume']]
    if kind == 'm':
        return args, parse(fetch(f'data/futures/um/monthly/markPriceKlines/{sym}/1m/{sym}-1m-{mo}.zip'), KC)[['open_time','high','low','close']]
    return args, parse(fetch(f'data/futures/um/monthly/fundingRate/{sym}/{sym}-fundingRate-{mo}.zip'), ['calc_time','funding_interval_hours','last_funding_rate'])
for coin in COINS:
    sym = coin + 'USDT'
    t0 = time.time()
    jobs = [(k, sym, mo) for mo in months for k in ('k', 'm', 'f')]
    res = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for a, df in ex.map(job, jobs):
            res[a] = df
    k = pd.concat([res[('k', sym, mo)] for mo in months]).drop_duplicates('open_time').set_index('open_time').sort_index()
    mk = pd.concat([res[('m', sym, mo)] for mo in months]).drop_duplicates('open_time').set_index('open_time').sort_index()
    f = pd.concat([res[('f', sym, mo)] for mo in months])
    t_first = int(pd.Timestamp('2021-12-01').value // 10**6); t_last = int(pd.Timestamp('2026-09-01').value // 10**6)
    idx = np.arange(t_first, t_last, 60000, dtype=np.int64)
    k = k.reindex(idx); mk = mk.reindex(idx)
    miss = k['close'].isna().values
    c = k['close'].ffill().bfill().values
    o = k['open'].fillna(pd.Series(c, index=idx)).values; h = k['high'].fillna(pd.Series(c, index=idx)).values; l = k['low'].fillna(pd.Series(c, index=idx)).values
    mmiss = mk['close'].isna().values
    mh = np.where(mmiss, h, mk['high'].values); ml = np.where(mmiss, l, mk['low'].values); mc = np.where(mmiss, c, mk['close'].values)
    # funding: event applied at the minute bar starting at calc_time rounded down to the minute
    ft = (f['calc_time'].values // 60000) * 60000
    fr = pd.Series(f['last_funding_rate'].values, index=ft)
    fr = fr[~fr.index.duplicated()]
    fund = np.zeros(len(idx)); fflag = np.zeros(len(idx), np.int8)
    ok = fr.index[(fr.index >= t_first) & (fr.index < t_last)]
    pos = np.searchsorted(idx, ok); fund[pos] = fr.loc[ok].values; fflag[pos] = 1
    np.savez(os.path.join(OUT, f'{coin}_1m.npz'), t=idx, o=o.astype(np.float64), h=h.astype(np.float64), l=l.astype(np.float64), c=c.astype(np.float64),
             qv=k['quote_volume'].fillna(0).values.astype(np.float32), tbq=k['taker_buy_quote_volume'].fillna(0).values.astype(np.float32),
             mh=mh.astype(np.float64), ml=ml.astype(np.float64), mc=mc.astype(np.float64), fund=fund, fflag=fflag, miss=miss.astype(np.int8))
    print(coin, len(idx), 'missing kline', int(miss.sum()), 'missing mark', int(mmiss.sum()), 'funding events', int(fflag.sum()), f'{time.time()-t0:.0f}s', flush=True)
