"""Binance monthly funding (2021-12..2026-08) for every USDT perp, for the listing-age cross-section.
-> data/funding_all.parquet (sym, t ms, rate)"""
from common import *
from concurrent.futures import ThreadPoolExecutor
M = json.load(open(os.path.join(D, 'months.json')))

def ym(k):
    return k.rsplit('-', 2)[-2] + '-' + k.rsplit('-', 1)[-1][:2]

def job(sym):
    ks = [k for k in M[sym]['funding'] if '2021-12' <= ym(k) <= '2026-08']
    parts = [parse_funding(get_archive(k)) for k in ks]
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    df = pd.concat(parts).drop_duplicates('t')
    df['sym'] = sym
    return df

with ThreadPoolExecutor(12) as ex:
    parts = [p for p in ex.map(job, sorted(M)) if p is not None]
out = pd.concat(parts, ignore_index=True)
out.to_parquet(os.path.join(D, 'funding_all.parquet'))
print(out.shape, out.sym.nunique())
