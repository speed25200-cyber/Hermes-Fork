"""Decision-time PREDICTED funding from Binance 1m premium-index klines (what Binance shows as the estimated rate).

For each event (sym, t, funding interval I hours) and decision offset k (minutes before t), using only premium-index
minutes that CLOSED by t-k:  P_hat = sum(w_j * p_j) / sum(w_j) over minutes j of [t-I, t-k) with Binance's
linearly increasing weights (w_j = position of the minute in the interval), p_j = minute close of the premium index;
F_hat = P_hat + clamp(i - P_hat, -0.05%, +0.05%), i = 0.03%/day * I/24 (Binance interest component).
Output data/pred.npz: pred_k{1,5,15,30,60} aligned with ev.parquet rows (NaN when no premium data).
"""
import os, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
from common import D, get_archive, parse_klines

KS = [1, 5, 15, 30, 60]


def fetch(args):
    sym, rows, ts, ih = args
    start = ts - (ih * 3600000).astype(np.int64)
    days = set()
    for a, b in zip(start, ts):
        for d in pd.date_range(pd.to_datetime(a, unit='ms').normalize(), pd.to_datetime(b - 60000, unit='ms').normalize(), freq='D'):
            days.add(d.strftime('%Y-%m-%d'))
    days = sorted(days)
    bym = {}
    for d in days:
        bym.setdefault(d[:7], []).append(d)
    keys = []
    for m, ds in bym.items():
        keys += [('m', m, ds)] if len(ds) >= 25 else [('d', d, [d]) for d in ds]

    def one(k):
        typ, x, ds = k
        if typ == 'm':
            df = parse_klines(get_archive(f'data/futures/um/monthly/premiumIndexKlines/{sym}/1m/{sym}-1m-{x}.zip'))
            if df is not None:
                return df
        parts = [parse_klines(get_archive(f'data/futures/um/daily/premiumIndexKlines/{sym}/1m/{sym}-1m-{d}.zip')) for d in ds]
        parts = [p for p in parts if p is not None]
        return pd.concat(parts) if parts else None

    with ThreadPoolExecutor(6) as ex:
        parts = [p for p in ex.map(one, keys) if p is not None]
    out = np.full((len(rows), len(KS)), np.nan)
    if not parts:
        return rows, out, len(keys)
    df = pd.concat(parts).drop_duplicates('t').sort_values('t')
    tm = (df.t.values // 60000).astype(np.int64)
    base = tm[0]
    n = tm[-1] - base + 1
    p = np.full(n, np.nan)
    p[tm - base] = df.c.values
    for r, (t, I) in enumerate(zip(ts, ih)):
        nI = int(round(I * 60))
        s = t // 60000 - nI - base            # index of the first minute of the interval
        w = np.arange(1, nI + 1, dtype=np.float64)
        lo = max(s, 0)
        seg = np.full(nI, np.nan)
        hi = min(s + nI, n)
        if hi > lo:
            seg[lo - s:hi - s] = p[lo:hi]
        for q, k in enumerate(KS):
            m = nI - k
            if m <= 0:
                continue
            x, ww = seg[:m], w[:m]
            ok = np.isfinite(x)
            if ok.sum() < 0.5 * m:
                continue
            P = np.sum(ww[ok] * x[ok]) / np.sum(ww[ok])
            i_int = 0.0003 * I / 24.0
            out[r, q] = P + np.clip(i_int - P, -0.0005, 0.0005)
    return rows, out, len(keys)


def main():
    ev = pd.read_parquet(os.path.join(D, 'ev.parquet'), columns=['sym', 't', 'row', 'interval_h', 'rate', 'ann'])
    groups = [(s, g.row.values, g.t.values.astype(np.int64), g.interval_h.values.astype(float)) for s, g in ev.groupby('sym')]
    groups.sort(key=lambda z: -len(z[1]))
    PR = np.full((len(ev), len(KS)), np.nan)
    t0 = time.time(); nf = 0
    with ThreadPoolExecutor(10) as ex:
        for i, (rows, out, nk) in enumerate(ex.map(fetch, groups)):
            PR[rows] = out
            nf += nk
            if i % 50 == 0:
                print(i, 'files', nf, round(time.time() - t0), 's', flush=True)
    np.savez(os.path.join(D, 'pred.npz'), **{f'pred_k{k}': PR[:, q] for q, k in enumerate(KS)})
    ann_real = ev.ann.values
    for q, k in enumerate(KS):
        a = PR[:, q] * 8760 / ev.interval_h.values
        ok = np.isfinite(a)
        big = ok & (np.abs(a) >= 1.0)
        print(f'k={k}: coverage {ok.mean():.3f}  corr(pred_ann, realised_ann) {np.corrcoef(a[ok], ann_real[ok])[0, 1]:.3f}  '
              f'sign agreement when |pred|>=100%: {(np.sign(a[big]) == np.sign(ann_real[big])).mean():.3f}  '
              f'median realised/pred when |pred|>=100%: {np.median(ann_real[big] / a[big]):.3f}')
    print('done', round(time.time() - t0), 's files', nf)


if __name__ == '__main__':
    main()
