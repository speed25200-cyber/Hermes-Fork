"""Lot-size feasibility of replicating the 16-member ensemble as separate tranches (1/16 of the sleeve each) for
OOS events on contracts still listed on OKX: one contract = ctVal x entry price (OKX units). Tranche = E/16 x L_coin
/ K x vol-scale, K=5, L_coin=1 (total gross 2 with the BTC leg); vol-scale 0.25 (floor), 0.5, 1. Also a 4-tranche
simplification (2 entry days x 2 stop levels). -> lots16.json"""
import sys, json
import numpy as np, pandas as pd
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.execution.okx.instruments import okx_inst_id, binance_price_factor
inst = {x['instId']: x for x in json.load(open('../newlisting/data/okx_instruments.json'))}
X = pd.read_parquet('events_d7.parquet')
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
from sim import Data
Dt = Data('hybrid')
px = {}
for i in range(Dt.n):
    for d0 in (24, 72):
        px[(Dt.ev.sym.values[i], d0)] = Dt.o[i, d0]
X = X[X.t >= '2025-01-01']
rows = []
for r in X.itertuples():
    iid = okx_inst_id(r.sym)
    if iid not in inst:
        continue
    unit = float(inst[iid]['ctVal']) * px[(r.sym, r.d0)] / binance_price_factor(r.sym) * float(inst[iid]['minSz'])
    rows.append(dict(sym=r.sym, d0=r.d0, unit=unit))
U = pd.DataFrame(rows)
out = dict(n=len(U), unit_usdt_q={str(q): float(U.unit.quantile(q)) for q in (0.5, 0.9, 0.99, 1.0)})
for E in (1000, 3000, 10000):
    for ntr in (16, 4):
        for sc in (0.25, 0.5, 1.0):
            tr = E / ntr * 1.0 / 5 * sc
            out[f'E{E}_tranches{ntr}_scale{sc}'] = dict(tranche_usdt=tr, frac_infeasible=float((U.unit > tr).mean()),
                                                        frac_rounding_gt_20pct=float(((tr - np.floor(tr / U.unit) * U.unit) / tr > 0.2).mean()))
print(json.dumps(out, indent=1))
json.dump(out, open('lots16.json', 'w'), indent=1)
