"""Download Binance USDT-M perp 1m klines around every candidate settlement and keep only the windows.

Candidate settlement = |ann_prev| >= 50%/yr OR |ann_t| >= 50%/yr (the second only so that the oracle
upper bound can be computed), t in [2022-01-01, 2026-09-01).
Window: bars with open time t-61min .. t+60min (122 bars; bar j has open time t + (j-61) minutes).
Stored: data/win_meta.parquet (sym, t, row) and data/win_perp.npy float32 [n, 122, 5] (o, h, l, c, quote vol);
missing minutes are NaN.  Source files: daily 1m archives (monthly archive when >= 25 days of a month are needed).
"""
import os, sys, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
from common import D, get_archive, parse_klines

KIND = sys.argv[1] if len(sys.argv) > 1 else 'perp'   # 'perp' or 'spot'
PRE, POST = 61, 60
NB = PRE + POST + 1
T0 = pd.Timestamp('2022-01-01').value // 10 ** 6
T1 = pd.Timestamp('2026-09-01').value // 10 ** 6


PREFIX = [('1000000', 1e6), ('1M', 1e6), ('1000', 1e3)]


def spot_sym(sym):
    """perp symbol -> (spot symbol, price multiplier to perp units); 1000PEPEUSDT -> (PEPEUSDT, 1000)"""
    b = sym[:-4]
    for p, mult in PREFIX:
        if b.startswith(p) and len(b) > len(p):
            return b[len(p):] + 'USDT', mult
    return sym, 1.0


def key_daily(sym, d):
    if KIND == 'perp':
        return f'data/futures/um/daily/klines/{sym}/1m/{sym}-1m-{d}.zip'
    ss = spot_sym(sym)[0]
    return f'data/spot/daily/klines/{ss}/1m/{ss}-1m-{d}.zip'


def key_monthly(sym, m):
    if KIND == 'perp':
        return f'data/futures/um/monthly/klines/{sym}/1m/{sym}-1m-{m}.zip'
    ss = spot_sym(sym)[0]
    return f'data/spot/monthly/klines/{ss}/1m/{ss}-1m-{m}.zip'


def fetch_sym(args):
    sym, ts = args
    ts = np.asarray(sorted(ts), dtype=np.int64)
    days = set(pd.to_datetime(ts - PRE * 60000, unit='ms').strftime('%Y-%m-%d')) | \
        set(pd.to_datetime(ts + POST * 60000, unit='ms').strftime('%Y-%m-%d'))
    days = sorted(days)
    bym = {}
    for d in days:
        bym.setdefault(d[:7], []).append(d)
    keys = []
    for m, ds in bym.items():
        if len(ds) >= 25:
            keys.append(('m', m, ds))
        else:
            keys += [('d', d, [d]) for d in ds]

    def one(k):
        typ, x, ds = k
        df = parse_klines(get_archive(key_monthly(sym, x) if typ == 'm' else key_daily(sym, x)))
        if df is None and typ == 'm':   # monthly missing (e.g. current month) -> daily
            parts = [parse_klines(get_archive(key_daily(sym, d))) for d in ds]
            parts = [p for p in parts if p is not None]
            df = pd.concat(parts) if parts else None
        return df

    with ThreadPoolExecutor(6) as ex:
        parts = [p for p in ex.map(one, keys) if p is not None]
    out = np.full((len(ts), NB, 5), np.nan, dtype=np.float32)
    if not parts:
        return sym, ts, out, 0
    df = pd.concat(parts).drop_duplicates('t').sort_values('t')
    if KIND == 'spot':
        mult = spot_sym(sym)[1]
        for c in ['o', 'h', 'l', 'c']:
            df[c] = df[c] * mult
    tm = (df.t.values // 60000).astype(np.int64)
    arr = df[['o', 'h', 'l', 'c', 'qv']].values.astype(np.float32)
    base = tm[0]
    n = tm[-1] - base + 1
    full = np.full((n, 5), np.nan, dtype=np.float32)
    full[tm - base] = arr
    for i, t in enumerate(ts):
        s = t // 60000 - PRE - base
        e = s + NB
        lo, hi = max(s, 0), min(e, n)
        if hi > lo:
            out[i, lo - s:hi - s] = full[lo:hi]
    return sym, ts, out, len(keys)


def main():
    f = pd.read_parquet(os.path.join(D, 'settle.parquet'))
    m = (f.t >= T0) & (f.t < T1) & ((f.ann_prev.abs() >= 0.5) | (f.ann.abs() >= 0.5))
    ev = f[m]
    if KIND == 'spot':   # spot hedge only exists for positive funding (short perp / long spot)
        ev = ev[(ev.ann_prev >= 0.5) | (ev.ann >= 0.5)]
        ks = pd.read_parquet(os.path.join(D, 'k1h_spot.parquet'), columns=['sym', 't'])
        ks['d'] = ks.t // 86400000
        have = set(zip(ks.sym, ks.d))
        ev = ev[[(s_, t_ // 86400000) in have for s_, t_ in zip(ev.sym, ev.t)]]
    groups = [(s, g.t.values) for s, g in ev.groupby('sym')]
    groups.sort(key=lambda z: -len(z[1]))
    print(KIND, 'events', len(ev), 'symbols', len(groups), flush=True)
    metas, arrs = [], []
    nfiles = 0
    t0 = time.time()
    with ThreadPoolExecutor(10) as ex:
        for i, (sym, ts, out, nk) in enumerate(ex.map(fetch_sym, groups)):
            metas.append(pd.DataFrame({'sym': sym, 't': ts}))
            arrs.append(out)
            nfiles += nk
            if i % 25 == 0:
                print(i, sym, len(ts), 'files so far', nfiles, round(time.time() - t0), 's', flush=True)
    meta = pd.concat(metas, ignore_index=True)
    meta['row'] = np.arange(len(meta))
    A = np.concatenate(arrs)
    meta.to_parquet(os.path.join(D, f'win_meta_{KIND}.parquet'))
    np.save(os.path.join(D, f'win_{KIND}.npy'), A)
    ok = np.isfinite(A[:, PRE, 0])
    print('done', A.shape, 'with price at t:', ok.mean().round(4), 'files', nfiles, round(time.time() - t0), 's')


if __name__ == '__main__':
    main()
