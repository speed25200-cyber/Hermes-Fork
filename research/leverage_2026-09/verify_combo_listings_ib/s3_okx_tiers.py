"""Live OKX metadata (2026-09-25): USDT SWAP instruments, tickers, and cross-margin position tiers for
(a) every USDT swap listed in the last 180 days (new-listing proxies), (b) the OOS-traded listing coins still on OKX,
(c) the book universe names, (d) BTC. -> okx_tiers_live.json, okx_tiers_summary.csv"""
import json, ssl, time, urllib.request
import pandas as pd, numpy as np
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def okx(path):
    for k in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request('https://www.okx.com' + path, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=30) as r:
                j = json.loads(r.read())
            if j.get('code') == '0':
                return j['data']
            time.sleep(1.5)
        except Exception:
            time.sleep(1 + k)
    return None
ins = okx('/api/v5/public/instruments?instType=SWAP')
tk = {t['instId']: float(t['last']) for t in okx('/api/v5/market/tickers?instType=SWAP') if t['last']}
ins = [i for i in ins if i['settleCcy'] == 'USDT' and i['ctType'] == 'linear']
now = pd.Timestamp('2026-09-25', tz='UTC')
tr = pd.read_csv('../newlisting/results/trades_selected_hybrid.csv')
tr = tr[pd.to_datetime(tr.entry_time) >= '2025-01-01']
traded = {s.replace('USDT', '') for s in tr.sym}
U = pd.read_parquet('universe_daily.parquet')
book = {s.replace('USDT', '') for s in U.columns}
rows, raw = [], {}
for i in ins:
    base = i['ctValCcy'] if i['ctValCcy'] not in ('USDT', '') else i['instFamily'].split('-')[0]
    lt = pd.Timestamp(int(i['listTime']), unit='ms', tz='UTC') if i['listTime'] else None
    age = (now - lt).days if lt is not None else None
    fam = i['instFamily']
    grp = []
    if age is not None and age <= 180: grp.append('new180')
    if base in traded or fam.split('-')[0] in traded: grp.append('oos_traded')
    if fam.split('-')[0] in book: grp.append('book')
    if fam == 'BTC-USDT': grp.append('btc')
    if not grp: continue
    t = okx(f'/api/v5/public/position-tiers?instType=SWAP&tdMode=cross&instFamily={fam}')
    time.sleep(0.12)
    if not t: continue
    raw[fam] = t
    t1 = sorted(t, key=lambda x: int(x['tier']))[0]
    px = tk.get(i['instId'], np.nan); ctv = float(i['ctVal']) * float(i.get('ctMult') or 1)
    rows.append(dict(instId=i['instId'], groups='|'.join(grp), listTime=str(lt), age_days=age, state=i['state'],
                     lever_inst=float(i['lever']) if i['lever'] else np.nan, t1_mmr=float(t1['mmr']), t1_imr=float(t1['imr']),
                     t1_maxLever=float(t1['maxLever']), t1_maxSz=float(t1['maxSz']), ctVal=ctv, last=px,
                     t1_cap_usdt=float(t1['maxSz']) * ctv * px, n_tiers=len(t),
                     min_order_usdt=float(i['minSz']) * ctv * px, lot_usdt=float(i['lotSz']) * ctv * px,
                     max_mmr=max(float(x['mmr']) for x in t)))
df = pd.DataFrame(rows)
df.to_csv('okx_tiers_summary.csv', index=False)
json.dump(raw, open('okx_tiers_live.json', 'w'))
for g in ('new180', 'oos_traded', 'book', 'btc'):
    x = df[df.groups.str.contains(g)]
    print(g, 'n', len(x))
    print(x[['t1_mmr', 't1_maxLever', 't1_cap_usdt', 'min_order_usdt', 'age_days']].describe(percentiles=[.1, .5, .9]).round(4).to_string())
x = df[df.groups.str.contains('new180')].sort_values('listTime', ascending=False)
print(x.head(25)[['instId', 'listTime', 'age_days', 'lever_inst', 't1_mmr', 't1_maxLever', 't1_cap_usdt', 'min_order_usdt']].to_string())
