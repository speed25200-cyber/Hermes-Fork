"""Evidence table for the 99 OKX-extra events -> okx_ev_evidence.csv"""
import sys, json
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist/verify/premkt")
from venues import *
from concurrent.futures import ThreadPoolExecutor
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
ev = pd.read_parquet(f'{SP}/xlist/data/sim/events.parquet')
sw = {x['instId']: x for x in json.load(open(f'{SP}/xlist/data/okx_swap_instruments.json'))}


def one(r):
    base = r.sym[:-len('-USDT-SWAP')]
    d = dict(sym=r.sym, base=base, t0_exact=pd.Timestamp(int(r.t0_exact), unit='ms'))
    x = sw.get(r.sym)
    d['okx_listTime_now'] = pd.Timestamp(int(x['listTime']), unit='ms') if x else None
    d['okx_preMktSw'] = pd.Timestamp(int(x['preMktSwTime']), unit='ms') if x and x.get('preMktSwTime') else None
    d['okx_ruleType'] = x['ruleType'] if x else None
    d.update(bybit_spot_first(base) or {})
    d.update(gate_start(base) or {})
    d.update(okx_spot_first(base, r.t0_exact))
    d['bn_spot_day'] = bn_spot_first(base)
    return d


bybit_dirs(); bn_spot_syms(); gate_pairs()
with ThreadPoolExecutor(12) as ex:
    rows = list(ex.map(one, [r for _, r in ev.iterrows()]))
out = pd.DataFrame(rows)
out.to_csv(f'{HERE}/okx_ev_evidence.csv', index=False)
print(out.notna().sum().to_dict())
