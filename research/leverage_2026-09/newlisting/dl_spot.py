"""Binance spot first-trading day for the base asset of each USDT perp (quote USDT, else any of USDT/BUSD/USDC/FDUSD/BTC/BNB)
-> data/spot_first.csv. Used to tell 'new token' perps from 'new perp on an old token'."""
import sys
sys.path.insert(0, '/home/user/Hermes/src')
from common import *
from concurrent.futures import ThreadPoolExecutor
from hermes.data.universe import base_asset
p, _ = s3_list('data/spot/monthly/klines/')
spot = set(x.split('/')[-2] for x in p)
print('spot symbols', len(spot), flush=True)
dk = pd.read_parquet(os.path.join(D, 'daily_klines.parquet'))
syms = sorted(dk.sym.unique())

def first_spot(sym):
    base = base_asset(sym)
    best = None
    for q in ('USDT', 'BUSD', 'USDC', 'FDUSD', 'BTC', 'BNB', 'TRY'):
        s = base + q
        if s not in spot:
            continue
        _, keys = s3_list(f'data/spot/monthly/klines/{s}/1d/')
        keys = sorted(k for k in keys if k.endswith('.zip'))
        if not keys:
            continue
        df = parse_klines(get_archive(keys[0]))
        if df is None or not len(df):
            continue
        d = pd.to_datetime(df.t.min(), unit='ms')
        if best is None or d < best[1]:
            best = (s, d)
    return sym, base, best[0] if best else None, best[1] if best else None

with ThreadPoolExecutor(16) as ex:
    rows = list(ex.map(first_spot, syms))
pd.DataFrame(rows, columns=['sym', 'base', 'spot_sym', 'spot_first']).to_csv(os.path.join(D, 'spot_first.csv'), index=False)
print('done', sum(r[2] is not None for r in rows), 'of', len(rows))
