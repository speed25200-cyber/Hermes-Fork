"""Reproduce OKX-alone and union/Binance-only headline numbers; check truncation equivalence."""
import json
from v_common import *
Do = load_okx()
Db = load_bn()
print('okx n', Do.n, 'newtok', int(Do.newtok.sum()), 'bn n', Db.n, 'H', Db.H)
out = {}
for name, a, b in [('okx all', '2022-01-01', '2026-09-25'), ('okx 2022-24', '2022-01-01', '2025-01-01'),
                   ('okx 2025-26', '2025-01-01', '2026-09-25'), ('okx 2026', '2026-01-01', '2026-09-25'),
                   ('okx 2025-26 to 09-01', '2025-01-01', '2026-09-01'), ('okx 2022-26 to 09-01', '2022-01-01', '2026-09-01')]:
    r, tr = run(Do, a, b)
    out[name] = dict(sharpe=round(sharpe(r['ret']), 3), n=len(tr), mean=round(tr.ret.mean(), 4), t=round(tstat(tr.ret), 2), maxdd=round(r['maxdd'], 3))
    print(name, out[name])
periods = [('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-01'),
           ('2026 Jan-Aug', '2026-01-01', '2026-09-01'), ('2022-2026', '2022-01-01', '2026-09-01')]
Bt = merge(Db, Do, keep_b=np.zeros(Do.n, bool))
U = merge(Db, Do)
for pn, a, b in periods:
    rf, trf = run(Db, a, b)
    rt, trt = run(Bt, a, b)
    ru, tru = run(U, a, b)
    same = len(trf) == len(trt) and np.allclose(rf['ret'].values, rt['ret'].values)
    out['bn_full ' + pn] = dict(sharpe=round(sharpe(rf['ret']), 3), n=len(trf), maxdd=round(rf['maxdd'], 3))
    out['bn_trunc ' + pn] = dict(sharpe=round(sharpe(rt['ret']), 3), n=len(trt), maxdd=round(rt['maxdd'], 3), identical_to_full=bool(same))
    out['union ' + pn] = dict(sharpe=round(sharpe(ru['ret']), 3), n=len(tru), okx=int((U.ev.src.values[tru.i] == 'okx').sum()), maxdd=round(ru['maxdd'], 3))
    print(pn, out['bn_full ' + pn], out['bn_trunc ' + pn], out['union ' + pn])
json.dump(out, open(SP + '/xlist/verify/v1_repro.json', 'w'), indent=1)
