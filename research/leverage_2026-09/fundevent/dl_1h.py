"""1h perp klines (Binance USDT-M monthly archives) for every symbol that ever had |ann funding| >= 50%,
for each month in which the symbol had funding settlements (2021-12 .. 2026-08). Also BTCUSDT.
-> data/k1h.parquet (sym, t, o, h, l, c, qv); used for beta / volume (decision-time) and the slow strategy (iii).
Optional arg 'mark' downloads the 1h MARK-price klines (liquidation sensitivity) -> data/k1h_mark.parquet.
Optional arg 'spot' downloads Binance SPOT 1h klines of the same symbols (spot hedge) -> data/k1h_spot.parquet.
"""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
from common import D, get_archive, parse_klines

KIND = sys.argv[1] if len(sys.argv) > 1 else 'perp'


PREFIX = [('1000000', 1e6), ('1M', 1e6), ('1000', 1e3)]


def spot_candidates(sym):
    b = sym[:-4]
    out = [(sym, 1.0)]
    for p, mult in PREFIX:
        if b.startswith(p) and len(b) > len(p):
            out.insert(0, (b[len(p):] + 'USDT', mult))
            break
    return out


def one(a):
    sym, mo = a
    if KIND in ('perp', 'mark'):
        sub = 'klines' if KIND == 'perp' else 'markPriceKlines'
        k = f'data/futures/um/monthly/{sub}/{sym}/1h/{sym}-1h-{mo}.zip'
        df = parse_klines(get_archive(k))
        mult = 1.0
    else:
        df = None
        for ss, mult in spot_candidates(sym):
            df = parse_klines(get_archive(f'data/spot/monthly/klines/{ss}/1h/{ss}-1h-{mo}.zip'))
            if df is not None:
                break
    if df is None:
        return None
    if mult != 1.0:   # express spot prices in perp units (1000PEPE perp = 1000 PEPE)
        for c in ['o', 'h', 'l', 'c']:
            df[c] = df[c] * mult
    df.insert(0, 'sym', sym)
    return df


f = pd.read_parquet(os.path.join(D, 'settle.parquet'))
ext = f[(f.ann.abs() >= 0.5) | (f.ann_prev.abs() >= 0.5)].sym.unique().tolist()
syms = set(ext) | {'BTCUSDT', 'ETHUSDT'}
f = f[f.sym.isin(syms)]
f['mo'] = pd.to_datetime(f.t, unit='ms').dt.strftime('%Y-%m')
jobs = sorted(set(zip(f.sym, f.mo)))
jobs = [j for j in jobs if '2021-12' <= j[1] <= '2026-08']
print(KIND, 'symbols', len(syms), 'symbol-months', len(jobs), flush=True)
t0 = time.time()
parts = []
with ThreadPoolExecutor(12) as ex:
    for i, p in enumerate(ex.map(one, jobs)):
        if p is not None:
            parts.append(p)
        if i % 2000 == 0:
            print(i, round(time.time() - t0), 's', flush=True)
df = pd.concat(parts, ignore_index=True)
for c in ['o', 'h', 'l', 'c', 'qv']:
    df[c] = df[c].astype(np.float32)
df = df.drop_duplicates(['sym', 't']).sort_values(['sym', 't']).reset_index(drop=True)
df.to_parquet(os.path.join(D, {'perp': 'k1h.parquet', 'spot': 'k1h_spot.parquet', 'mark': 'k1h_mark.parquet'}[KIND]))
print('rows', len(df), 'symbols', df.sym.nunique(), round(time.time() - t0), 's')
