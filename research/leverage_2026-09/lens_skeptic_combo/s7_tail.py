"""Tail checks for the listing sleeve inside the combination.
A. Supportable L when the listing sleeve fills stops at the bar high (worst) or with 5% slippage (with costs x1.5 and
   fair latency); book as-is / 50% haircut / DSR-excess, book trough = its daily loss.
B. Jump tail of new-token perps in the traded window (listing +72h..+168h), all eligible 'newtok' events on OKX 2022-2026:
   max excursion high / entry open, and largest single-hour high / previous close.
-> s7_tail.json"""
import json, os, sys, numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
from sim_patch import sim, simulate2
from s0_repro import load, sharpe, cagr, maxdd, years

def path(rc, rw):
    C = np.cumprod(1 + rc.values); prevC = np.r_[1.0, C[:-1]]
    peak_prev = np.maximum.accumulate(np.r_[1.0, C])[:-1]
    worst = prevC * (1 + rw.values)
    return max(float(np.max(1 - worst / peak_prev)), maxdd(rc))

cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
Dt = sim.Data('hybrid')
j = load().dropna(subset=['book', 'nl'])['2025-01-01':'2026-08-31']
S1 = json.load(open(os.path.join(HERE, 's1_haircut.json'))); KB = S1['scenarios_k']
W = 0.6030102397652416; WL = 1 - W
LEVS = [1, 2, 2.5, 3, 3.5, 4, 5]
VAR = {'stopworst': dict(stop_worst=True), 'stopslip5_c15_fairlat': dict(stop_slip=0.05, cost_mult=1.5, lat=1, fair_lat=True),
       'stopworst_c15_fairlat': dict(stop_worst=True, cost_mult=1.5, lat=1, fair_lat=True)}
out = {'A': {}}
for vn, kw in VAR.items():
    for bn in ('as_is', 'haircut50_0.68', 'dsr_excess'):
        rb = j.book - KB[bn] * j.book_gross
        res = {}
        for L in LEVS:
            r = simulate2(Dt, cfg, '2025-01-01', '2026-09-01', L=WL * L, record=True, **kw)
            eqc = r['eq'].resample('D').last(); prev = eqc.shift(1).fillna(1.0)
            tr = (r['eq_worst'].resample('D').min() / prev - 1).fillna(0.0).reindex(j.index)
            rl = r['ret'].reindex(j.index)
            rc = L * W * rb + rl; rw = np.minimum(L * W * np.minimum(rb, 0) + np.minimum(tr, rl), rc)
            res[L] = dict(dd_ib=path(rc, rw), sharpe=sharpe(rc), cagr=cagr(rc), liq=r['liq'], alone_ib=r['maxdd'])
        ok = [L for L, v in res.items() if v['dd_ib'] <= 0.35 and not v['liq']]
        out['A'][f'{vn}|{bn}'] = dict(L_max=max(ok) if ok else 0, sharpe=res[1]['sharpe'],
                                      dd_ib={str(L): round(v['dd_ib'], 3) for L, v in res.items()},
                                      listing_alone_ib={str(L): round(v['alone_ib'], 3) for L, v in res.items()})
        print(vn, bn, out['A'][f'{vn}|{bn}'])
# ---- B. jump tail
rows = []
for i in range(Dt.n):
    if not Dt.newtok[i] or Dt.H <= 168:
        continue
    k0 = 72
    if not (Dt.okx_on[i, k0] and np.isfinite(Dt.o[i, k0])):
        continue
    o0 = Dt.o[i, k0]
    h = Dt.h[i, k0:168]; c = Dt.c[i, k0 - 1:167]
    if not np.isfinite(h).any():
        continue
    rows.append(dict(sym=Dt.ev.sym.values[i], year=int(Dt.year[i]), max_exc=float(np.nanmax(h) / o0 - 1),
                     max_1h_jump=float(np.nanmax(h / c) - 1)))
T = pd.DataFrame(rows)
qs = [0.5, 0.9, 0.95, 0.99, 1.0]
out['B'] = dict(n=len(T), max_exc_q={str(q): float(T.max_exc.quantile(q)) for q in qs},
                max_1h_jump_q={str(q): float(T.max_1h_jump.quantile(q)) for q in qs},
                frac_exc_ge_50=float((T.max_exc >= 0.5).mean()), frac_exc_ge_100=float((T.max_exc >= 1.0).mean()),
                frac_1h_jump_ge_30=float((T.max_1h_jump >= 0.3).mean()), frac_1h_jump_ge_50=float((T.max_1h_jump >= 0.5).mean()),
                top5=T.nlargest(5, 'max_1h_jump').to_dict(orient='records'))
print(json.dumps(out['B'], indent=1))
json.dump(out, open(os.path.join(HERE, 's7_tail.json'), 'w'), indent=1, default=float)
