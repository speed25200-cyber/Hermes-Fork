"""Union with de-duplicated tokens (same base asset listed on the other venue within the 168 h hold)."""
import json
from v_common import *
Do = load_okx(); Db = load_bn()
d = pd.read_csv(SP + '/xlist/verify/v2_dup_pairs.csv')
ov = d[(d.dh > 0) & (d.dh < 168)]
drop_bn = np.zeros(Db.n, bool); drop_bn[ov.bn_i.values] = True
drop_ok = np.zeros(Do.n, bool); drop_ok[ov.okx_j.values] = True
print('overlapping pairs', len(ov), sorted(ov.base))
variants = {
    'binance_only': merge(Db, Do, keep_b=np.zeros(Do.n, bool)),
    'union_author': merge(Db, Do),
    'union_first_venue_wins (drop later Binance event)': merge(Db, Do, keep_a=~drop_bn),
    'union_binance_wins (drop OKX event)': merge(Db, Do, keep_b=~drop_ok),
}
periods = [('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-01'),
           ('2026 Jan-Aug', '2026-01-01', '2026-09-01'), ('2022-2026', '2022-01-01', '2026-09-01')]
out = {}
for vn, U in variants.items():
    for pn, a, b in periods:
        r, tr = run(U, a, b)
        rec = dict(sharpe=round(sharpe(r['ret']), 3), trades=len(tr), okx=int((U.ev.src.values[tr.i] == 'okx').sum()) if len(tr) else 0,
                   maxdd=round(r['maxdd'], 3), final=round(r['final'], 3))
        out[f'{vn} | {pn}'] = rec
        print(f'{vn:50s} {pn:13s}', rec, flush=True)
json.dump(out, open(SP + '/xlist/verify/v3_dedupe.json', 'w'), indent=1)
