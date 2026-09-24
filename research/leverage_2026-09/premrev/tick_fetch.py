"""Tick-level check of the selected trades: download Binance daily aggTrades (perp + spot) for the event days
IN MEMORY, keep only the trades in windows around each entry / exit minute boundary, save small parquet.
Usage: python tick_fetch.py <trades_csv> <exec> <lat>"""
import io, os, sys, ssl, time, zipfile, urllib.request
from multiprocessing import Pool
import numpy as np, pandas as pd

CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
BASE = 'https://data.binance.vision/'
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev/out/ticks'
os.makedirs(OUT, exist_ok=True)
T0 = pd.Timestamp('2021-12-01', tz='UTC')


def fetch(path):
    for k in range(6):
        try:
            with urllib.request.urlopen(BASE + path, context=CTX, timeout=300) as r:
                return r.read()
        except Exception as e:
            time.sleep(3 * (k + 1))
    raise RuntimeError(path)


def job(args):
    market, sym, day, windows = args
    fn = f'{OUT}/{market}_{sym}_{day}.parquet'
    if os.path.exists(fn):
        return fn, 'cached'
    kind = 'futures/um' if market == 'perp' else 'spot'
    b = fetch(f'data/{kind}/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip')
    z = zipfile.ZipFile(io.BytesIO(b))
    keep = []
    with z.open(z.namelist()[0]) as f:
        first = f.readline().decode()
        header = not first.split(',')[0].strip().isdigit()
    with z.open(z.namelist()[0]) as f:
        rd = pd.read_csv(f, header=0 if header else None, usecols=[1, 2, 5, 6], chunksize=2_000_000)
        for ch in rd:
            ch.columns = ['price', 'qty', 'time', 'ibm']
            t = ch['time'].values.astype(np.int64)
            t = np.where(t > 10**14, t // 1000, t)
            m = np.zeros(len(t), bool)
            for a, e in windows:
                m |= (t >= a) & (t <= e)
            if m.any():
                c = ch[m].copy(); c['time'] = t[m]
                c['ibm'] = c['ibm'].astype(str).str.lower().isin(['true', '1'])
                keep.append(c)
    del b, z
    df = pd.concat(keep) if keep else pd.DataFrame(columns=['price', 'qty', 'time', 'ibm'])
    df.to_parquet(fn, index=False)
    return fn, len(df)


def main():
    tr = pd.read_csv(sys.argv[1], parse_dates=['t'])
    tr = tr[(tr['exec'] == sys.argv[2]) & (tr['lat'] == int(sys.argv[3]))]
    need = {}
    for _, r in tr.iterrows():
        sig = int((r.t - T0).total_seconds() // 60)
        te = (sig + 1) * 60_000 + int(T0.value // 1_000_000)
        # exit decision minute = entry exec minute + hold; open-exec at xx -> boundary xx*60s
        xx = sig + 1 + int(r.hold_min)
        tx = xx * 60_000 + int(T0.value // 1_000_000)
        for (a, e) in [(te - 120_000, te + 90_000), (tx - 90_000, tx + 90_000)]:
            for d in {pd.Timestamp(a, unit='ms').strftime('%Y-%m-%d'), pd.Timestamp(e, unit='ms').strftime('%Y-%m-%d')}:
                for mk in ['perp', 'spot']:
                    need.setdefault((mk, r.coin, d), []).append((a, e))
    jobs = [(mk, s, d, w) for (mk, s, d), w in sorted(need.items())]
    print(len(jobs), 'files', flush=True)
    with Pool(4) as p:
        for fn, n in p.imap_unordered(job, jobs):
            print(fn.split('/')[-1], n, flush=True)


if __name__ == '__main__':
    main()
