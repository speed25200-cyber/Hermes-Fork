"""Critic spot-checks: (A) union de-dupe by base asset (independent matching), (B) Binance pre-market exclusion,
(C) combined corrections. Read-only use of author data."""
import sys, json, types
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve'); sys.path.insert(0, SP + '/newlisting'); sys.path.insert(0, '/home/user/Hermes/src')
import sim
import numpy as np, pandas as pd
from livesim import simulate_live, sharpe
from hermes.data.universe import base_asset
SIG = 0.1238230231575359; H = 240
KW = dict(tranches=(24, 72), d1=168, stop=0.5, K=5, late=True, max_late=2, shared_stop=True)
Db = sim.Data('hybrid'); Db.sigma_ref = SIG
sim.D = SP + '/xlist/data/sim'
Do = sim.Data('binance'); Do.sigma_ref = SIG

def merge(a, b, ka=None, kb=None):
    m = types.SimpleNamespace()
    ka = np.ones(a.n, bool) if ka is None else ka
    kb = np.ones(b.n, bool) if kb is None else kb
    for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund', 'okx_on'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[ka, :H], getattr(b, k)[kb, :H]])
    for k in ('vol_d', 'vol_n'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[ka, :H - 1], getattr(b, k)[kb, :H - 1]])
    for k in ('g0', 'newtok', 'year'):
        m.__dict__[k] = np.concatenate([getattr(a, k)[ka], getattr(b, k)[kb]])
    m.ev = pd.concat([a.ev[['sym', 't0']].assign(src='binance')[ka], b.ev[['sym', 't0']].assign(src='okx')[kb]], ignore_index=True)
    m.n, m.H = len(m.ev), H; m.sigma_ref = SIG
    return m

# --- independent base matching
bb = np.array([base_asset(s) for s in Db.ev.sym]); ob = np.array([s.split('-')[0] for s in Do.ev.sym])
pairs = []
for j in range(Do.n):
    for i in np.where(bb == ob[j])[0]:
        dh = (Db.ev.t0.values[i] - Do.ev.t0.values[j]) / 3.6e6
        pairs.append(dict(base=ob[j], i=int(i), j=int(j), dh=dh, bn_new=bool(Db.newtok[i]), okx_new=bool(Do.newtok[j])))
pairs = pd.DataFrame(pairs)
both = pairs[pairs.bn_new & pairs.okx_new]
w168 = both[(both.dh > 0) & (both.dh < 168)]
print('base overlaps', len(pairs), 'both newtok', len(both), 'within (0,168h)', len(w168), sorted(w168.base))
print('both newtok, dh>=168:', len(both[both.dh >= 168]), sorted(both[both.dh >= 168].base))
drop168 = np.zeros(Db.n, bool); drop168[w168.i.values] = True
dropall = np.zeros(Db.n, bool); dropall[both[both.dh > 0].i.values] = True

# --- Binance pre-market list from the premkt lens
pm = pd.read_csv(SP + '/xlist/verify/premkt/bn_premarket_classification.csv')
pm_syms = sorted(pm[pm.premarket == True].sym)
print('premarket bn events', len(pm_syms), pm_syms)
pmask = Db.ev.sym.isin(pm_syms).values
# OKX-extra pre-market: MET, RE
opm = Do.ev.sym.isin(['MET-USDT-SWAP', 'RE-USDT-SWAP']).values

P = [('2022-24', '2022-01-01', '2025-01-01'), ('2025-26', '2025-01-01', '2026-09-01'), ('2026JanAug', '2026-01-01', '2026-09-01'), ('2022-26', '2022-01-01', '2026-09-01')]
none_b = np.zeros(Do.n, bool)
V = {
 'bn_only': merge(Db, Do, kb=none_b),
 'union': merge(Db, Do),
 'union_dedupe168': merge(Db, Do, ka=~drop168),
 'union_dedupe_forever': merge(Db, Do, ka=~dropall),
 'bn_only_exPM': merge(Db, Do, ka=~pmask, kb=none_b),
 'union_exPM': merge(Db, Do, ka=~pmask, kb=~opm),
 'union_exPM_dedupe168': merge(Db, Do, ka=~pmask & ~drop168, kb=~opm),
}
out = {}
daily = {}
for vn, U in V.items():
    for pn, a, b in P:
        r = simulate_live(U, start=a, end=b, **KW)
        tr = pd.DataFrame(r['trades'])
        rec = dict(sharpe=round(sharpe(r['ret']), 3), trades=len(tr), okx=int((U.ev.src.values[tr.i] == 'okx').sum()), maxdd=round(r['maxdd'], 3), final=round(r['final'], 3))
        out[f'{vn}|{pn}'] = rec
        daily[f'{vn}|{pn}'] = r['ret']
        print(f'{vn:24s} {pn:11s} {rec}', flush=True)
json.dump(dict(runs=out, w168=w168.to_dict('records'), pm=pm_syms), open(SP + '/xlist/verify/critic/c1_spot.json', 'w'), indent=1, default=str)
pd.to_pickle(daily, SP + '/xlist/verify/critic/c1_daily.pkl')
