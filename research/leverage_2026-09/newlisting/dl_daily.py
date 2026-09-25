"""Step 1: per Binance USDT-M symbol (delisted included), all monthly 1d klines -> data/daily_klines.parquet;
S3 listings of 1d/1h monthly files -> data/months.json."""
from common import *
from concurrent.futures import ThreadPoolExecutor
syms = json.load(open(os.path.join(D, 'um_symbols.json')))
usdt = sorted(s for s in syms['monthly'] if s.endswith('USDT'))
print(len(usdt), flush=True)

def months(sym):
    out = {}
    for iv in ('1d', '1h'):
        _, keys = s3_list(f'data/futures/um/monthly/klines/{sym}/{iv}/')
        out[iv] = sorted(k for k in keys if k.endswith('.zip'))
    _, keys = s3_list(f'data/futures/um/monthly/fundingRate/{sym}/')
    out['funding'] = sorted(k for k in keys if k.endswith('.zip'))
    return sym, out

fn = os.path.join(D, 'months.json')
if os.path.exists(fn):
    M = json.load(open(fn))
else:
    with ThreadPoolExecutor(16) as ex:
        M = dict(ex.map(months, usdt))
    json.dump(M, open(fn, 'w'))
print('listed', sum(len(v['1d']) for v in M.values()), 'daily files', flush=True)

def load_daily(sym):
    parts = []
    for k in M[sym]['1d']:
        df = parse_klines(get_archive(k))
        if df is not None:
            parts.append(df)
    if not parts:
        return None
    df = pd.concat(parts).drop_duplicates('t').sort_values('t')
    df['sym'] = sym
    return df

with ThreadPoolExecutor(16) as ex:
    parts = [p for p in ex.map(load_daily, usdt) if p is not None]
df = pd.concat(parts, ignore_index=True)
df.to_parquet(os.path.join(D, 'daily_klines.parquet'))
print(df.shape, df.sym.nunique(), flush=True)
