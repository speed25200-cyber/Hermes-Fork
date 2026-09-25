"""(e) OKX order books now (2026-09-25, public REST books sz=400) for (i) OKX crypto USDT swaps listed in the last 60
days (the closest available proxy for a book at listing+3..7d; flag if Binance USDT-M also lists the symbol, from
data.binance.vision daily klines prefixes) and (ii) the 2025-26 event coins still listed. For market SELL orders of
50..2000 USDT: cost vs mid in bp (half spread + walk of the bids). Also current max lever / tier-1 cap / maxMktSz.
-> exec_books.json, exec_books_rows.csv"""
import sys, json, time, ssl, urllib.request, re
import numpy as np, pandas as pd
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def get(u):
    for k in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=30) as r:
                return r.read()
        except Exception:
            time.sleep(1 + k)
    return None
sys.path.insert(0, '/home/user/Hermes/src')
from hermes.execution.okx.instruments import okx_inst_id
ins = json.loads(get('https://www.okx.com/api/v5/public/instruments?instType=SWAP'))['data']
I = {x['instId']: x for x in ins}
now = pd.Timestamp('2026-09-25')
recent = [x for x in ins if x.get('instCategory') == '1' and x['settleCcy'] == 'USDT' and x['state'] == 'live'
          and pd.Timestamp(int(x['listTime']), unit='ms') >= now - pd.Timedelta(days=60)]
X = pd.read_parquet('events_d7.parquet')
ev_insts = sorted({okx_inst_id(s) for s in X[X.t >= '2025-01-01'].sym} & set(I))
targets = [(x['instId'], 'recent60d') for x in recent] + [(i, 'event2025_26') for i in ev_insts]
SIZES = [50, 200, 500, 1000, 2000]
rows = []
for iid, grp in targets:
    x = I[iid]
    b = get(f'https://www.okx.com/api/v5/market/books?instId={iid}&sz=400')
    time.sleep(0.12)
    if b is None:
        continue
    d = json.loads(b)
    if d.get('code') != '0' or not d['data']:
        continue
    bk = d['data'][0]
    bids = np.array([[float(p), float(s)] for p, s, *_ in bk['bids']])
    asks = np.array([[float(p), float(s)] for p, s, *_ in bk['asks']])
    if len(bids) == 0 or len(asks) == 0:
        continue
    ctv = float(x['ctVal'])
    mid = (bids[0, 0] + asks[0, 0]) / 2
    r = dict(inst=iid, group=grp, listTime=pd.Timestamp(int(x['listTime']), unit='ms'), lever=float(x['lever']), ctVal=ctv,
             minSz=float(x['minSz']), maxMktSz=float(x['maxMktSz']) if x.get('maxMktSz') else np.nan,
             maxMktUSD=float(x['maxMktSz']) * ctv * mid if x.get('maxMktSz') else np.nan,
             spread_bp=(asks[0, 0] - bids[0, 0]) / mid * 1e4, mid=mid,
             depth_bid_usd_1pct=float((bids[bids[:, 0] >= mid * 0.99, 1] * ctv * bids[bids[:, 0] >= mid * 0.99, 0]).sum()),
             depth_bid_usd_2pct=float((bids[bids[:, 0] >= mid * 0.98, 1] * ctv * bids[bids[:, 0] >= mid * 0.98, 0]).sum()))
    usd = bids[:, 1] * ctv * bids[:, 0]
    cum = np.cumsum(usd)
    for S in SIZES:
        if cum[-1] < S:
            r[f'cost_{S}_bp'] = np.nan; continue
        k = np.searchsorted(cum, S)
        filled = np.concatenate([usd[:k], [S - (cum[k - 1] if k else 0)]])
        px = bids[:k + 1, 0]
        qty = filled / px
        vwap = filled.sum() / qty.sum()
        r[f'cost_{S}_bp'] = (1 - vwap / mid) * 1e4
    rows.append(r)
B = pd.DataFrame(rows)
# Binance USDT-M listing check for the recent group (data.binance.vision daily klines prefix exists)
def on_binance(iid):
    base = iid.replace('-USDT-SWAP', '')
    for sym in (base + 'USDT', '1000' + base + 'USDT'):
        u = f'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/futures/um/daily/klines/{sym}/&delimiter=/&max-keys=1'
        t = get(u)
        if t and b'<CommonPrefixes>' in t:
            return True
    return False
B['on_binance_um'] = [on_binance(i) if g == 'recent60d' else True for i, g in zip(B.inst, B.group)]
B.to_csv('exec_books_rows.csv', index=False)
out = {}
for nm, m in (('recent60d_all', B.group == 'recent60d'), ('recent60d_binance_listed', (B.group == 'recent60d') & B.on_binance_um),
              ('event2025_26_now', B.group == 'event2025_26')):
    Z = B[m]
    out[nm] = dict(n=int(len(Z)), spread_bp_med=float(Z.spread_bp.median()), spread_bp_p90=float(Z.spread_bp.quantile(0.9)),
                   lever_min=float(Z.lever.min()), lever_med=float(Z.lever.median()), maxMktUSD_min=float(Z.maxMktUSD.min()),
                   depth1pct_usd_med=float(Z.depth_bid_usd_1pct.median()), depth1pct_usd_p10=float(Z.depth_bid_usd_1pct.quantile(0.1)),
                   **{f'cost_{S}_bp_med': float(Z[f'cost_{S}_bp'].median()) for S in SIZES},
                   **{f'cost_{S}_bp_p90': float(Z[f'cost_{S}_bp'].quantile(0.9)) for S in SIZES})
print(B[B.group == 'recent60d'][['inst', 'listTime', 'on_binance_um', 'lever', 'spread_bp', 'depth_bid_usd_1pct', 'cost_200_bp', 'cost_2000_bp', 'maxMktUSD']].round(1).to_string())
print(json.dumps(out, indent=1))
json.dump(out, open('exec_books.json', 'w'), indent=1)
