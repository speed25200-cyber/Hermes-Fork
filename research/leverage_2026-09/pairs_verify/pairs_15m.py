"""15-minute variant of the pairs stat-arb (robustness check of the bar size).

Same walk-forward universe and pair selection as pairs_bt.py (formation on hourly closes, past data only).
Signals, execution, stops, funding and liquidation on 15m bars:
  - Binance USDT-M 15m last-price klines for every (symbol, month) that appears in a selection, plus the previous
    month (z-score look-back); downloaded into memory only (disk is nearly full).
  - Liquidation / intrabar DD bound: long leg at its 15m LOW, short leg at its 15m HIGH (last price, whose wicks are
    at least as deep as the mark's) -> conservative at 15-minute resolution.
  - z windows 96 / 288 / 672 bars (1 / 3 / 7 days), max hold = window, other tunables as in the hourly grid.
Protocol identical: tunables chosen on 2022-2024 only, 2025-01..2026-08 OOS.
"""
import os, sys, io, json, time, zipfile
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pairs_bt as pb
from pairs_v2 import sim2, FEE_M
from dl_daily import get

GRID = pd.date_range(pb.G0, pb.G1, freq='15min', inclusive='left')
NB = len(GRID)
R = 'https://data.binance.vision/data/futures/um/monthly/klines'


def fetch15(sym, month):
    b = get(f'{R}/{sym}/15m/{sym}-15m-{month}.zip')
    if b is None:
        return None
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=0 if not raw[:1].isdigit() else None, usecols=[0, 1, 2, 3, 4])
    df.columns = ['t', 'o', 'h', 'l', 'c']
    return df[pd.to_numeric(df.t, errors='coerce').notna()].astype(float)


def load15(syms, sel):
    need = set()
    for d in sel.values():
        for t, v in d.items():
            t = pd.Timestamp(t)
            for (a, b, be) in v:
                for s in (a, b):
                    need.add((s, t.strftime('%Y-%m')))
                    need.add((s, (t - pd.offsets.MonthBegin(1)).strftime('%Y-%m')))
    need = sorted(need)
    print('15m files', len(need), flush=True)
    A = {k: np.full((len(syms), NB), np.nan, dtype=np.float32) for k in ['o', 'h', 'l', 'c']}
    t0 = GRID[0].value // 10**6

    def one(x):
        s, m = x
        return s, fetch15(syms[s], m)
    with ThreadPoolExecutor(16) as ex:
        for s, df in ex.map(one, need):
            if df is None:
                continue
            idx = ((df.t.values - t0) // 900000).astype(np.int64)
            ok = (idx >= 0) & (idx < NB)
            for k in A:
                A[k][s, idx[ok]] = df[k].values[ok]
    return A


def fund15(P):
    """Funding events on the 15m grid (event at T charged to the bar whose close is T)."""
    FR = np.zeros((len(P['syms']), NB))
    t0 = GRID[0].value // 10**6
    for j, s in enumerate(P['syms']):
        fp = os.path.join(pb.D, 'f', s + '.parquet')
        if os.path.exists(fp):
            f = pd.read_parquet(fp)
            th = np.round((f.t.values - t0) / 900000.0).astype(np.int64) - 1
            ok = (th >= 0) & (th < NB)
            np.add.at(FR[j], th[ok], f.rate.values[ok])
    return FR


def slot15(logc, U, names, sel_m, K, Wz):
    PA = np.full((K, NB), -1, np.int32); PB = np.full((K, NB), -1, np.int32)
    BE = np.zeros((K, NB)); Z = np.full((K, NB), np.nan)
    SA = np.zeros((K, NB)); SB = np.zeros((K, NB)); NEWM = np.zeros(NB, np.int8)
    prev = {}
    for m, t in enumerate(pb.FORM_DATES):
        i0 = GRID.get_loc(t)
        i1 = GRID.get_loc(pb.FORM_DATES[m + 1]) if m + 1 < len(pb.FORM_DATES) else NB
        NEWM[i0] = 1
        pairs = sel_m[t][:K]
        slots = {}; free = list(range(K))
        for (a, b, be) in pairs:
            if (a, b) in prev:
                slots[(a, b)] = prev[(a, b)]; free.remove(prev[(a, b)])
        for (a, b, be) in pairs:
            if (a, b) not in slots:
                slots[(a, b)] = free.pop(0)
        rk = {name: r for r, name in enumerate(U[t])}
        for (a, b, be) in pairs:
            k = slots[(a, b)]
            j0 = max(0, i0 - Wz - 96)
            ss = pd.Series(logc[a, j0:i1] - be * logc[b, j0:i1])
            mu = ss.rolling(Wz, min_periods=int(Wz * 0.9)).mean().values
            sd = ss.rolling(Wz, min_periods=int(Wz * 0.9)).std().values
            z = (ss.values - mu) / sd
            PA[k, i0:i1] = a; PB[k, i0:i1] = b; BE[k, i0:i1] = be; Z[k, i0:i1] = z[i0 - j0:]
            SA[k, i0:i1] = pb.base_slip(names[a], rk.get(names[a], 99))
            SB[k, i0:i1] = pb.base_slip(names[b], rk.get(names[b], 99))
        prev = slots
    return PA, PB, BE, Z, SA, SB, NEWM


SIG15 = [(Wz, zin, zout, zin + dz) for Wz in [96, 288, 672] for zin in [1.5, 2.0, 2.5, 3.0] for zout in [0.0, 0.5]
         for dz in [1.5, 3.0, 99.0]]
SCHEMES15 = [(m, h, w, k) for m in ['coint', 'corr'] for h in ['lvl', 'ret'] for w in [60, 120] for k in [3, 5, 10]]
SCHEMES15 += [('fixed', h, w, 5) for h in ['lvl', 'ret'] for w in [60, 120]]
_G = {}


def _worker(scheme):
    g = _G
    method, hedge, Wf, K = scheme
    rows = []
    day_all = GRID.normalize()
    for Wz in sorted(set(s[0] for s in SIG15)):
        PA, PB, BE, Z, SA, SB, NEWM = slot15(g['logc'], g['U'], g['names'], g['sel'][(method, hedge, Wf)], K, Wz)
        W0 = np.full((K, NB), np.nan)
        for (wz, zin, zout, zstop) in [s for s in SIG15 if s[0] == Wz]:
            for pname, (p0, p1) in {'IS': (pb.IS0, pb.IS1), 'OOS': (pb.OOS0, pb.OOS1)}.items():
                i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < pb.G1 else NB
                days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
                DAY = ((day_all - p0).days).values.astype(np.int64)
                for ex in ['taker', 'maker', 'gross']:
                    levs = [1] if ex == 'gross' else pb.LEVS
                    for lev in levs:
                        fee = 0.0 if ex == 'gross' else pb.FEE_T
                        sm = 0.0 if ex == 'gross' else 1.0
                        daily, st, _ = sim2(g['O'], g['H'], g['L'], g['C'], g['H'], g['L'], g['FR'], DAY, i0, i1,
                                            len(days), PA, PB, BE, Z, SA * sm, SB * sm, NEWM, W0, W0, g['mmr'],
                                            g['imr'], zin, zout, zstop, Wz, float(lev), False, fee,
                                            FEE_M if ex == 'maker' else fee, pb.RANGE_SLIP * sm, ex == 'maker', False, 1)
                        m = pb.metrics(daily, days)
                        rows.append(dict(method=method, hedge=hedge, Wf=Wf, K=K, Wz=Wz, zin=zin, zout=zout,
                                         zstop=zstop, exec=ex, lev=lev, period=pname, cagr=m['cagr'],
                                         final=m['final'], sharpe=m['sharpe'], worst_day=m['worst_day'],
                                         maxdd_intrabar=st[1], trades=int(st[2]), liqs=int(st[3]), stops=int(st[4]),
                                         fees=st[7], funding=st[8], exposure=st[9], legged=int(st[11]),
                                         maker_miss=int(st[12]),
                                         per_year=json.dumps({str(k): round(v, 4) for k, v in m['per_year'].items()})))
    print('15m', scheme, 'done', flush=True)
    return pd.DataFrame(rows)


if __name__ == '__main__':
    import multiprocessing as mp
    P, syms, first_bar, U, mmr, imr = pb.prepare()
    sel = pb.build_selections(P, U, first_bar)
    t = time.time()
    A = load15(list(P['syms']), sel)
    print('15m data loaded', f'{time.time() - t:.0f}s', flush=True)
    del P['o'], P['h'], P['l'], P['mh'], P['ml']
    FR = fund15(P)
    O = A['o'].astype(np.float64); H = A['h'].astype(np.float64); L = A['l'].astype(np.float64)
    C = A['c'].astype(np.float64)
    del A
    _G.update(O=O, H=H, L=L, C=C, FR=FR, logc=np.log(C), U=U, names=list(P['syms']), sel=sel, mmr=mmr, imr=imr)
    del P
    with mp.get_context('fork').Pool(4) as pool:
        parts = pool.map(_worker, SCHEMES15, chunksize=1)
    df = pd.concat(parts, ignore_index=True)
    df.to_csv(os.path.join(pb.OUT, 'grid_15m.csv.gz'), index=False, compression='gzip')
    print(df.shape)
