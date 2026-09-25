"""Live-implementation check (1): OKX SWAP instrument specs / position tiers for the OKX-extra events still listed,
order granularity at 250-1000 USDT per tranche, and ex-ante visibility of upcoming listings (state=preopen)."""
import sys, json, pandas as pd, numpy as np
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist')
from common import okx_get
from concurrent.futures import ThreadPoolExecutor
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
ins = okx_get('/api/v5/public/instruments?instType=SWAP')
I = {d['instId']: d for d in ins}
print('SWAP instruments', len(ins), 'states', pd.Series([d['state'] for d in ins]).value_counts().to_dict())
pre = [d for d in ins if d['state'] != 'live']
for d in pre:
    print('  non-live:', d['instId'], d['state'], 'listTime', pd.to_datetime(int(d['listTime'] or 0), unit='ms'), 'cat', d.get('instCategory'), 'ruleType', d.get('ruleType'))
print('keys', sorted(ins[0].keys()))
ev = pd.read_parquet(SP + '/xlist/data/sim/events.parquet')
tr = pd.read_csv(SP + '/xlist/results/okx_trades.csv')
h1 = pd.read_parquet(SP + '/xlist/data/ev_h1.parquet')
rows = []
for s in sorted(set(tr.sym)):
    d = I.get(s)
    if d is None:
        continue
    t = tr[tr.sym == s]
    entry_px = float(t.entry_px.iloc[0])
    last = float(h1[h1.instId == s].c.iloc[-1])
    ctv, lot, mn = float(d['ctVal']), float(d['lotSz']), float(d['minSz'])
    rows.append(dict(sym=s, ctVal=ctv, lotSz=lot, minSz=mn, maxMktSz=float(d['maxMktSz']), maxMktAmt=float(d.get('maxMktAmt') or 'nan'),
                     lever=d['lever'], entry_px=entry_px, min_order_usdt_at_entry=mn * ctv * entry_px,
                     step_usdt_at_entry=lot * ctv * entry_px, rounding_err_800=(lot * ctv * entry_px) / 2 / 800,
                     maxMkt_usdt_at_entry=float(d['maxMktSz']) * ctv * entry_px, listTime=pd.to_datetime(int(d['listTime']), unit='ms'),
                     t0=pd.to_datetime(ev[ev.sym == s].t0.iloc[0], unit='ms'), last_px=last))
df = pd.DataFrame(rows)
def tiers(s):
    fam = s.replace('-SWAP', '')
    t = okx_get(f'/api/v5/public/position-tiers?instType=SWAP&tdMode=cross&instFamily={fam}')
    if not t:
        return s, None
    t = sorted(t, key=lambda x: float(x['maxSz']))
    return s, dict(n=len(t), t1_maxSz=float(t[0]['maxSz']), t1_lever=float(t[0]['maxLever']), top_maxSz=float(t[-1]['maxSz']), top_lever=float(t[-1]['maxLever']))
with ThreadPoolExecutor(4) as ex:
    T = dict(ex.map(tiers, df.sym))
for k in ('n', 't1_maxSz', 't1_lever', 'top_maxSz', 'top_lever'):
    df[k] = [T[s][k] if T[s] else np.nan for s in df.sym]
df['t1_usdt_last'] = df.t1_maxSz * df.ctVal * df.last_px
df['top_usdt_last'] = df.top_maxSz * df.ctVal * df.last_px
pd.set_option('display.width', 250)
print(df[['sym', 't0', 'listTime', 'ctVal', 'minSz', 'lotSz', 'min_order_usdt_at_entry', 'rounding_err_800', 'maxMkt_usdt_at_entry', 'lever', 't1_usdt_last', 't1_lever', 'top_usdt_last']].to_string())
print('live traded OKX-extra instruments:', len(df), 'of', tr.sym.nunique())
print('max min-order USDT at entry', df.min_order_usdt_at_entry.max(), 'max rounding err (share of 800)', df.rounding_err_800.max())
print('min tier-1 notional (last px)', df.t1_usdt_last.min(), 'min maxMkt at entry', df.maxMkt_usdt_at_entry.min())
df.to_csv(SP + '/xlist/verify/critic/c3_inst.csv', index=False)
