"""Download Binance 1m perp / spot / premium-index / mark / index klines (in memory, zips never written to
disk: the disk is almost full) and store a compact per-minute panel per coin (int16 in 0.1 bp units,
parquet zstd).

Stored fields (per UTC minute t, 2021-12-01 .. 2026-08-31):
  s_c      float64->float32  Binance spot close (last trade)
  b_c      (F_c/S_c - 1)                 tradeable perp-spot basis at the bar close (Binance last prices)
  b_o      (F_o/S_o - 1)                 basis at the bar open
  ls_c     int32 log(spot close) x 1e5
  s_o      spot open relative to spot close (0.1 bp)
  s_h,s_l  spot high/low relative to spot close (1 bp units)
  f_h,f_l  perp high/low relative to perp close (1 bp)
  mp_c     (M_c/I_c - 1) mark premium over index (0.1 bp)
  p_c      Binance premium index close (0.1 bp); p_h, p_l = premium index high/low minus close (1 bp)
  m_h,m_l  mark high/low relative to spot close (1 bp)
  i_c      index close relative to spot close (0.1 bp)
  s_ok,f_ok     bar present with >0 trades
int16 fields: 0.1 bp (1e-5) or 1 bp (1e-4) units as stated; NaN / missing -> -32768; values clipped to +-32767.
"""
import io, os, ssl, sys, time, zipfile, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd

CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
BASE = 'https://data.binance.vision/'
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev/data'
os.makedirs(OUT, exist_ok=True)
T0 = pd.Timestamp('2021-12-01', tz='UTC')
T1 = pd.Timestamp('2026-09-01', tz='UTC')
NMIN = int((T1 - T0).total_seconds() // 60)
T0MS = int(T0.value // 1_000_000)
MONTHS = [p.strftime('%Y-%m') for p in pd.period_range('2021-12', '2026-08', freq='M')]
KINDS = {'f': 'futures/um/monthly/klines', 's': 'spot/monthly/klines', 'p': 'futures/um/monthly/premiumIndexKlines',
         'm': 'futures/um/monthly/markPriceKlines', 'i': 'futures/um/monthly/indexPriceKlines'}


def fetch(path):
    for k in range(6):
        try:
            with urllib.request.urlopen(BASE + path, context=CTX, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 * (k + 1))
        except Exception:
            time.sleep(2 * (k + 1))
    raise RuntimeError('failed ' + path)


def parse(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 1, 2, 3, 4, 8], low_memory=False)
    if not str(df.iloc[0, 0]).strip().isdigit():
        df = df.iloc[1:]
    df = df.astype({0: 'int64', 1: 'float64', 2: 'float64', 3: 'float64', 4: 'float64', 8: 'float64'})
    t = df[0].values
    t = np.where(t > 10**14, t // 1000, t)          # spot files switched to microseconds in 2025
    return t, df[1].values, df[2].values, df[3].values, df[4].values, df[8].values


def build(sym):
    jobs = [(k, mo, f'data/{KINDS[k]}/{sym}/1m/{sym}-1m-{mo}.zip') for k in KINDS for mo in MONTHS]
    with ThreadPoolExecutor(10) as ex:
        blobs = list(ex.map(lambda j: fetch(j[2]), jobs))
    arr = {k: np.full((NMIN, 5), np.nan) for k in KINDS}
    missing = []
    for (k, mo, p), b in zip(jobs, blobs):
        if b is None:
            missing.append(f'{k}:{mo}')
            continue
        t, o, h, l, c, n = parse(b)
        i = (t - T0MS) // 60000
        ok = (i >= 0) & (i < NMIN)
        arr[k][i[ok]] = np.column_stack([o, h, l, c, n])[ok]
    del blobs
    F, S, P, M, I = arr['f'], arr['s'], arr['p'], arr['m'], arr['i']
    q = lambda x: np.where(np.isfinite(x), np.clip(np.round(x * 1e5), -32767, 32767), -32768).astype(np.int16)
    q1 = lambda x: np.where(np.isfinite(x), np.clip(np.round(x * 1e4), -32767, 32767), -32768).astype(np.int16)
    sc = S[:, 3]
    d = {
        'ls_c': np.where(np.isfinite(sc), np.round(np.log(sc) * 1e5), -2**31).astype(np.int32),   # log spot close x1e5
        'b_c': q(F[:, 3] / S[:, 3] - 1), 'b_o': q(F[:, 0] / S[:, 0] - 1),
        's_o': q(S[:, 0] / sc - 1), 's_h': q1(S[:, 1] / sc - 1), 's_l': q1(S[:, 2] / sc - 1),
        'f_h': q1(F[:, 1] / F[:, 3] - 1), 'f_l': q1(F[:, 2] / F[:, 3] - 1),
        'mp_c': q(M[:, 3] / I[:, 3] - 1),
        'p_c': q(P[:, 3]), 'p_h': q1(P[:, 1] - P[:, 3]), 'p_l': q1(P[:, 2] - P[:, 3]),
        'm_h': q1(M[:, 1] / sc - 1), 'm_l': q1(M[:, 2] / sc - 1),
        'i_c': q(I[:, 3] / sc - 1),
        's_ok': np.isfinite(S[:, 3]) & (S[:, 4] > 0), 'f_ok': np.isfinite(F[:, 3]) & (F[:, 4] > 0),
    }
    df = pd.DataFrame(d)
    df.to_parquet(f'{OUT}/{sym}.parquet', compression='zstd', compression_level=12, index=False, use_dictionary=False,
                  column_encoding={'ls_c': 'DELTA_BINARY_PACKED'})
    sz = os.path.getsize(f'{OUT}/{sym}.parquet') / 1e6
    cov = {k: float(np.isfinite(arr[k][:, 3]).mean()) for k in KINDS}
    print(sym, f'{sz:.1f}MB', 'coverage', {k: round(v, 4) for k, v in cov.items()}, 'missing', missing, flush=True)


if __name__ == '__main__':
    for sym in sys.argv[1:]:
        t = time.time()
        build(sym)
        print(sym, 'done in', round(time.time() - t), 's', flush=True)
