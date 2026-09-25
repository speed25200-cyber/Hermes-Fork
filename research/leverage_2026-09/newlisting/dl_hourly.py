"""Step 2: 1h klines + funding for the first 5 calendar months of every USDT perp listed since 2021-09
(delisted included), plus BTCUSDT 1h and funding 2021-09..2026-08. -> data/h1.parquet, data/funding.parquet"""
from common import *
from concurrent.futures import ThreadPoolExecutor
M = json.load(open(os.path.join(D, 'months.json')))
dk = pd.read_parquet(os.path.join(D, 'daily_klines.parquet'))
first = dk.groupby('sym').t.min()
first = pd.to_datetime(first, unit='ms')
new = sorted(s for s, d in first.items() if d >= pd.Timestamp('2021-09-01'))
print('new since 2021-09:', len(new), flush=True)

def ym(k):
    return k.rsplit('-', 2)[-2] + '-' + k.rsplit('-', 1)[-1][:2]

def job(sym):
    h = M[sym]['1h'][:5] if sym != 'BTCUSDT' else [k for k in M[sym]['1h'] if '2021-09' <= ym(k) <= '2026-08']
    f = M[sym]['funding'][:5] if sym != 'BTCUSDT' else [k for k in M[sym]['funding'] if '2021-09' <= ym(k) <= '2026-08']
    ph = [parse_klines(get_archive(k)) for k in h]
    pf = [parse_funding(get_archive(k)) for k in f]
    ph = [p for p in ph if p is not None]
    pf = [p for p in pf if p is not None]
    a = pd.concat(ph).drop_duplicates('t').sort_values('t') if ph else None
    b = pd.concat(pf).drop_duplicates('t').sort_values('t') if pf else None
    if a is not None:
        a['sym'] = sym
    if b is not None:
        b['sym'] = sym
    return a, b

with ThreadPoolExecutor(16) as ex:
    res = list(ex.map(job, new + ['BTCUSDT']))
h1 = pd.concat([a for a, _ in res if a is not None], ignore_index=True)
fu = pd.concat([b for _, b in res if b is not None], ignore_index=True)
h1.to_parquet(os.path.join(D, 'h1.parquet'))
fu.to_parquet(os.path.join(D, 'funding.parquet'))
print(h1.shape, h1.sym.nunique(), fu.shape, fu.sym.nunique(), flush=True)
