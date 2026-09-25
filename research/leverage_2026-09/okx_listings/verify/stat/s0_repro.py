"""Reproduce results/union.json with the reviewer copy of the simulator; check full-run slicing vs per-period runs."""
import json, time
from statlib import *
Db, Do = load()
U = merge(Db, Do)
B = merge(Db, Do, keep_b=np.zeros(Do.n, bool))
ref = json.load(open(SP + '/xlist/results/union.json'))
out = {}
for name, D_ in (('binance_only', B), ('union', U)):
    t = time.time()
    full = sim_live(D_, start='2022-01-01', end='2026-09-01')
    dt = time.time() - t
    for pn, a, b in PERIODS:
        r = sim_live(D_, start=a, end=b)
        s = sharpe(r['ret'])
        sl = full['ret'][a:pd.Timestamp(b) - pd.Timedelta(days=1)]
        rr = ref[f'{name} {pn}']
        out[f'{name} {pn}'] = dict(sharpe=round(s, 3), author=rr['sharpe'], trades=len(r['trades']), author_trades=rr['trades'],
                                   maxdd=round(r['maxdd'], 3), author_maxdd=rr['maxdd'], sharpe_slice_of_full=round(sharpe(sl), 3))
        print(name, pn, out[f'{name} {pn}'], f'{dt:.1f}s/full run', flush=True)
json.dump(out, open(OUT + '/s0_repro.json', 'w'), indent=1)
