"""Duplicated base assets across the Binance and OKX event sets, and their overlap in the union run."""
import json
sys_path = None
from v_common import *
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.data.universe import base_asset
pd.set_option('display.width', 250)
Do = load_okx(); Db = load_bn()
bo = Do.ev.sym.str.replace('-USDT-SWAP', '', regex=False).values
bb = np.array([base_asset(s) for s in Db.ev.sym])
HMS = 3600000
rows = []
for j, b in enumerate(bo):
    for i in np.where(bb == b)[0]:
        rows.append(dict(base=b, okx_j=j, bn_i=i, okx_t0=pd.Timestamp(Do.ev.t0[j], unit='ms'), bn_t0=pd.Timestamp(Db.ev.t0[i], unit='ms'),
                         dh=(Db.ev.t0[i] - Do.ev.t0[j]) / HMS, okx_new=bool(Do.newtok[j]), bn_new=bool(Db.newtok[i]), cls=Do.ev.cls[j]))
d = pd.DataFrame(rows)
print(d.to_string())
print('pairs', len(d), 'with bn t0 within 168h after okx t0:', int(((d.dh > 0) & (d.dh < 168)).sum()),
      'both newtok & overlapping:', int(((d.dh > 0) & (d.dh < 168) & d.okx_new & d.bn_new).sum()))
# overlap in the actual union run
U = merge(Db, Do)
for pn, a, b in [('2022-2026', '2022-01-01', '2026-09-01')]:
    r, tr = run(U, a, b)
    tr['src'] = U.ev.src.values[tr.i]
    tr['base'] = [base_asset(s) if src == 'binance' else s.replace('-USDT-SWAP', '') for s, src in zip(tr.sym, tr.src)]
    tr['exit_t'] = tr.exit_t.astype(int)
    ov = []
    for base, g in tr.groupby('base'):
        if g.src.nunique() < 2:
            continue
        o, bn = g[g.src == 'okx'], g[g.src == 'binance']
        for _, x in o.iterrows():
            for _, y in bn.iterrows():
                if x.entry_t < y.exit_t and y.entry_t < x.exit_t:
                    ov.append(dict(base=base, okx_entry=pd.Timestamp('2021-12-01') + pd.Timedelta(hours=int(x.entry_t)),
                                   bn_entry=pd.Timestamp('2021-12-01') + pd.Timedelta(hours=int(y.entry_t)), okx_ret=round(x.ret, 3), bn_ret=round(y.ret, 3),
                                   okx_notional=round(x.notional, 3), bn_notional=round(y.notional, 3)))
    ov = pd.DataFrame(ov)
    print(pn, 'simultaneous okx/binance positions on the same token:', len(ov), 'tokens', ov.base.nunique() if len(ov) else 0)
    print(ov.to_string())
    d.to_csv(SP + '/xlist/verify/v2_dup_pairs.csv', index=False)
    ov.to_csv(SP + '/xlist/verify/v2_overlap_trades.csv', index=False)
