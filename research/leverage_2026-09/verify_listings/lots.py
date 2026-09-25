"""Lot-size feasibility with target = (notional / equity at entry) x account, for OOS trades (hybrid2 prices, OKX instruments today)."""
import sys, json
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.execution.okx.instruments import binance_price_factor
from sim_v import *
Dt = Data('hybrid2')
inst = {x['instId']: x for x in json.load(open(os.path.join(D, 'okx_instruments.json')))}
cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
for Lc, lab in ((0.5, '1x total'), (1.0, '2x total'), (1.5, '3x total')):
    tr = pd.DataFrame(simulate(Dt, cfg, OOS_START, OOS_END, L=Lc)['trades'])
    tr['frac'] = tr.notional / tr.E_before
    tr['iid'] = Dt.ev.inst.values[tr.i.values]
    live = tr[tr.iid.map(lambda x: x in inst and inst[x].get('instCategory') == '1')].copy()
    live['unit'] = [float(inst[r.iid]['ctVal']) * r.entry_px / binance_price_factor(r.sym) for r in live.itertuples()]
    live['lot'] = [float(inst[r.iid]['lotSz']) for r in live.itertuples()]; live['mn'] = [float(inst[r.iid]['minSz']) for r in live.itertuples()]
    for acct in (1000, 10000):
        tgt = live.frac * acct
        n = np.floor(tgt / live.unit / live.lot + 1e-9) * live.lot
        feas = n >= live.mn; err = ((n * live.unit - tgt).abs() / tgt)[feas]
        print(lab, acct, 'OOS trades', len(tr), 'live', len(live), 'min target %.1f USDT' % tgt.min(), 'feasible', int(feas.sum()), 'median err %.4f p90 %.4f max %.4f' % (err.median(), err.quantile(.9), err.max()))
