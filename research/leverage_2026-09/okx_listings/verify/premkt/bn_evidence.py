"""Spot-start evidence for the 137 Binance events traded by the frozen rule (2022-01..2026-09) -> bn_ev_evidence.csv"""
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist/verify/premkt")
from venues import *
from concurrent.futures import ThreadPoolExecutor
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
u = pd.read_csv(f'{HERE}/bn_traded_events.csv', parse_dates=['spot_first'])
okx = {x['instId']: x for x in json.load(open(f'{SP}/newlisting/data/okx_instruments.json'))} if False else {}
sw = {x['instId']: x for x in json.load(open(f'{SP}/xlist/data/okx_swap_instruments.json'))}


def one(r):
    base = r.base
    d = dict(i=r.i, sym=r.sym, base=base, t0=pd.Timestamp(int(r.t0), unit='ms'), bn_spot=r.spot_first)
    x = sw.get(f'{base}-USDT-SWAP')
    d['okx_preMktSw'] = pd.Timestamp(int(x['preMktSwTime']), unit='ms') if x and x.get('preMktSwTime') else None
    d.update(bybit_spot_first(base) or {})
    d.update(gate_start(base) or {})
    d.update(okx_spot_first(base, r.t0))
    return d


bybit_dirs(); gate_pairs()
with ThreadPoolExecutor(12) as ex:
    rows = list(ex.map(one, [r for _, r in u.iterrows()]))
out = pd.DataFrame(rows)
out.to_csv(f'{HERE}/bn_ev_evidence.csv', index=False)
print(out.notna().sum().to_dict())
