"""Frozen live rule on: OKX-extra (baseline / excl pre-market / TGE re-anchored) and union vs Binance-only
(baseline / excl pre-market in both sets / re-anchored in both sets, with and without dedupe of the OKX copies of
MET and RE which coincide with Binance METUSDT/REUSDT once re-anchored). -> rerun.json"""
import sys, json, types
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve'); sys.path.insert(0, SP + '/newlisting')
import sim
import numpy as np, pandas as pd
from livesim import simulate_live, sharpe
HERE = SP + '/xlist/verify/premkt'
SIG = 0.1238230231575359
H = 240
KW = dict(tranches=(24, 72), d1=168, stop=0.5, K=5, late=True, max_late=2, shared_stop=True)


def tstat(x):
    x = np.asarray(x, float)
    return float(x.mean() / x.std(ddof=1) * np.sqrt(len(x))) if len(x) > 2 else float('nan')


def recompute_vol(D):
    lr = np.diff(np.log(D.c), axis=1); lr[:, 0] = np.nan
    m = np.isfinite(lr); x = np.where(m, lr, 0.0)
    cs, cs2, cn = np.cumsum(x, 1), np.cumsum(x * x, 1), np.cumsum(m, 1)
    var = (cs2 - cs ** 2 / np.maximum(cn, 1)) / np.maximum(cn - 1, 1)
    D.vol_d = np.sqrt(np.maximum(var, 0) * 24); D.vol_n = cn


def load_okx(path):
    sim.D = path
    D = sim.Data('binance'); D.sigma_ref = SIG
    return D


def okx_stats(D, tag, out):
    for name, a, b in (('all 2022-2026', '2022-01-01', '2026-09-25'), ('2022-2024', '2022-01-01', '2025-01-01'),
                       ('2025-2026', '2025-01-01', '2026-09-25'), ('2026', '2026-01-01', '2026-09-25')):
        r = simulate_live(D, start=a, end=b, **KW)
        tr = pd.DataFrame(r['trades'])
        rec = dict(sharpe=round(sharpe(r['ret']), 3), n=len(tr), mean=round(float(tr.ret.mean()), 4),
                   t=round(tstat(tr.ret), 2), maxdd=round(r['maxdd'], 3))
        out[f'okx_extra {tag} {name}'] = rec
        print(f'okx_extra {tag:10s} {name:14s} Sharpe {rec["sharpe"]:.3f} trades {rec["n"]} mean {rec["mean"]} t {rec["t"]} maxDD {rec["maxdd"]}', flush=True)


def merge(a, b, keep_b=None):
    m = types.SimpleNamespace()
    kb = np.ones(b.n, bool) if keep_b is None else keep_b
    for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund', 'okx_on'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[:, :H], getattr(b, k)[kb, :H]])
    for k in ('vol_d', 'vol_n'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[:, :H - 1], getattr(b, k)[kb, :H - 1]])
    for k in ('g0', 'newtok', 'year'):
        m.__dict__[k] = np.concatenate([getattr(a, k), getattr(b, k)[kb]])
    m.ev = pd.concat([a.ev[['sym', 't0']].assign(src='binance'), b.ev[['sym', 't0']].assign(src='okx')[kb]], ignore_index=True)
    m.n, m.H = len(m.ev), H
    m.sigma_ref = SIG
    return m


PERIODS = [('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-01'),
           ('2026 Jan-Aug', '2026-01-01', '2026-09-01'), ('2022-2026', '2022-01-01', '2026-09-01')]


def union_stats(D_, tag, out):
    for pn, a, b in PERIODS:
        r = simulate_live(D_, start=a, end=b, **KW)
        ret = r['ret']; tr = pd.DataFrame(r['trades'])
        src = D_.ev.src.values[tr.i] if len(tr) else []
        rec = dict(sharpe=round(sharpe(ret), 3), trades=len(tr), okx_trades=int((np.asarray(src) == 'okx').sum()),
                   maxdd=round(r['maxdd'], 3), mean=round(float(tr.ret.mean()), 4))
        out[f'{tag} {pn}'] = rec
        print(f'{tag:34s} {pn:13s} Sharpe {rec["sharpe"]:.3f} trades {rec["trades"]} (okx {rec["okx_trades"]}) maxDD {rec["maxdd"]:.3f} mean {rec["mean"]}', flush=True)


out = {}
# ---------------- OKX-extra
Do0 = load_okx(SP + '/xlist/data/sim')
okx_stats(Do0, 'base', out)
Do1 = load_okx(HERE + '/sim_okx_excl')
okx_stats(Do1, 'excl', out)
Do2 = load_okx(HERE + '/sim_okx_reanchor')
okx_stats(Do2, 'reanchor', out)
# ---------------- Binance (hybrid)
sim.D = SP + '/newlisting/data'
Db0 = sim.Data('hybrid'); Db0.sigma_ref = SIG
anc = pd.read_csv(HERE + '/bn_pm_anchor.csv', parse_dates=['t0', 'tge'])
pm_idx = anc.i.values.astype(int)
Db1 = sim.Data('hybrid'); Db1.sigma_ref = SIG
Db1.newtok = Db1.newtok.copy(); Db1.newtok[pm_idx] = False
Db2 = sim.Data('hybrid'); Db2.sigma_ref = SIG
Db2.ev = Db2.ev.copy()
for _, r in anc.iterrows():
    i = int(r.i)
    s = int((r.tge.floor('h') - r.t0).total_seconds() // 3600)
    for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund', 'okx_on'):
        A = getattr(Db2, k)
        fill = False if A.dtype == bool else (0.0 if k == 'fund' else np.nan)
        row = A[i, s:].copy()
        A[i, :] = fill; A[i, :len(row)] = row
    Db2.g0[i] += s
    Db2.ev.loc[i, 't0'] = int(Db2.ev.t0[i] + s * 3600000)
recompute_vol(Db2)
assert Db2.newtok[pm_idx].all()
# sanity: full-H binance-only runs
for tag, D in (('bn_full base', Db0), ('bn_full excl', Db1), ('bn_full reanchor', Db2)):
    r = simulate_live(D, start='2025-01-01', end='2026-09-01', **KW)
    out[tag + ' 2025-2026'] = round(sharpe(r['ret']), 3)
    print(tag, '2025-2026 Sharpe', round(sharpe(r['ret']), 3), len(r['trades']))
# union / binance-only (H-truncated, as combo.py)
dup = np.isin(Do2.ev.sym.values, ['MET-USDT-SWAP', 'RE-USDT-SWAP'])
cases = [('binance_only base', Db0, Do0, np.zeros(Do0.n, bool)), ('union base', Db0, Do0, None),
         ('binance_only excl', Db1, Do1, np.zeros(Do1.n, bool)), ('union excl', Db1, Do1, None),
         ('binance_only reanchor', Db2, Do2, np.zeros(Do2.n, bool)), ('union reanchor', Db2, Do2, None),
         ('union reanchor dedupe', Db2, Do2, ~dup)]
for tag, a, b, kb in cases:
    union_stats(merge(a, b, kb), tag, out)
json.dump(out, open(HERE + '/rerun.json', 'w'), indent=1)
