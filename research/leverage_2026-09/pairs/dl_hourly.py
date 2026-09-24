"""Step 2: hourly data for every symbol that is in the point-in-time top-30 at some formation date.

Per symbol, only the months needed (formation look-back 150 days before, +62 days after each formation date where it
is eligible; see need_months.json produced from daily_1d.parquet). Binance USDT-M archive:
  klines/1h (last price OHLC + quote volume), markPriceKlines/1h (mark OHLC, used for liquidation), fundingRate.
Stored compactly as data/h/<SYM>.parquet (float32 prices) and data/f/<SYM>.parquet (funding events).
"""
import io, os, json, zipfile, sys
from concurrent.futures import ThreadPoolExecutor
import pandas as pd, numpy as np
from dl_daily import get

HERE = os.path.dirname(os.path.abspath(__file__))
R = 'https://data.binance.vision/data/futures/um/monthly'


def rz(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    return pd.read_csv(io.BytesIO(raw), header=0 if not raw[:1].isdigit() else None)


def kl(sym, m, kind):
    path = 'klines' if kind == 'k' else 'markPriceKlines'
    b = get(f'{R}/{path}/{sym}/1h/{sym}-1h-{m}.zip')
    if b is None:
        return None
    df = rz(b)
    df = df.iloc[:, [0, 1, 2, 3, 4, 7]]
    df.columns = ['t', 'o', 'h', 'l', 'c', 'qv']
    df = df[pd.to_numeric(df.t, errors='coerce').notna()].astype(float)
    return df


def fu(sym, m):
    b = get(f'{R}/fundingRate/{sym}/{sym}-fundingRate-{m}.zip')
    if b is None:
        return None
    df = rz(b)
    df = df.iloc[:, [0, df.shape[1] - 1]]
    df.columns = ['t', 'rate']
    df = df[pd.to_numeric(df.t, errors='coerce').notna()].astype(float)
    return df


def do_sym(sym, months):
    fh = os.path.join(HERE, 'data', 'h', sym + '.parquet')
    if os.path.exists(fh):
        return sym, 'cached'
    K = [kl(sym, m, 'k') for m in months]
    M = [kl(sym, m, 'm') for m in months]
    F = [fu(sym, m) for m in months]
    K = [x for x in K if x is not None]
    if not K:
        return sym, 'nodata'
    k = pd.concat(K).drop_duplicates('t').set_index('t').sort_index()
    mm = [x for x in M if x is not None]
    if mm:
        m_ = pd.concat(mm).drop_duplicates('t').set_index('t').sort_index()
        k = k.join(m_[['h', 'l', 'c']].rename(columns={'h': 'mh', 'l': 'ml', 'c': 'mc'}), how='left')
    else:
        k['mh'] = np.nan; k['ml'] = np.nan; k['mc'] = np.nan
    k = k.astype('float32')
    k.index = k.index.astype('int64')
    k.reset_index().to_parquet(fh, index=False)
    ff = [x for x in F if x is not None]
    if ff:
        f = pd.concat(ff).drop_duplicates('t').sort_values('t')
        f.to_parquet(os.path.join(HERE, 'data', 'f', sym + '.parquet'), index=False)
    return sym, len(k)


if __name__ == '__main__':
    os.makedirs(os.path.join(HERE, 'data', 'h'), exist_ok=True)
    os.makedirs(os.path.join(HERE, 'data', 'f'), exist_ok=True)
    need = json.load(open(os.path.join(HERE, 'data', sys.argv[1] if len(sys.argv) > 1 else 'need_months.json')))
    with ThreadPoolExecutor(12) as ex:
        for s, n in ex.map(lambda kv: do_sym(*kv), need.items()):
            print(s, n, flush=True)
