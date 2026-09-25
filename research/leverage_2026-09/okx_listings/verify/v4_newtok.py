"""OKX-extra alone under alternative 'new token' definitions."""
import json
from v_common import *
Do = load_okx()
ev = Do.ev
t0 = pd.to_datetime(ev.t0, unit='ms')
bn = pd.to_datetime(ev.bn_spot)
okl = pd.to_datetime(ev.okx_spot_live)
byb = pd.to_datetime(ev.bybit_perp)
def new_from(sf):
    return (sf.isna() | ((t0 - sf).dt.days <= 30)).values
defs = {
    'author (bn spot, okx spot listTime, okx spot archive probe)': Do.newtok.copy(),
    'live-rule literal (Binance spot only)': new_from(bn),
    'bn spot + okx spot listTime (no archive probe)': new_from(pd.concat([bn, okl], axis=1).min(axis=1)),
    'author + Bybit perp > 30 d old counts as old': Do.newtok & ~(byb.notna() & ((t0 - byb).dt.days > 30)).values,
    'author minus TESTPM002': Do.newtok & (ev.sym != 'TESTPM002-USDT-SWAP').values,
}
out = {}
periods = [('2022-2026', '2022-01-01', '2026-09-25'), ('2022-2024', '2022-01-01', '2025-01-01'),
           ('2025-2026', '2025-01-01', '2026-09-25'), ('2026', '2026-01-01', '2026-09-25')]
for name, nt in defs.items():
    Do.newtok = nt
    for pn, a, b in periods:
        r, tr = run(Do, a, b)
        rec = dict(events=int(nt.sum()), sharpe=round(sharpe(r['ret']), 3), trades=len(tr), mean=round(float(tr.ret.mean()), 4) if len(tr) else None,
                   t=round(tstat(tr.ret), 2) if len(tr) else None, maxdd=round(r['maxdd'], 3))
        out[f'{name} | {pn}'] = rec
        print(f'{name:60s} {pn:10s}', rec, flush=True)
json.dump(out, open(SP + '/xlist/verify/v4_newtok.json', 'w'), indent=1)
