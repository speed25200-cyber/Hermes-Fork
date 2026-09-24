import pandas as pd, json
d = pd.read_csv('out/grid_v2.csv.gz')
low = pd.read_csv('out/grid_v2_low.csv.gz')
d = pd.concat([d, low])
q = d[(d.method=='coint')&(d.hedge=='lvl')&(d.Wf==60)&(d.K==3)&(d.Wz==336)&(d.zin==2.5)&(d.zout==0.5)&(d.liq=='m1')&(d.margin=='cross')&(d.lev==1)]
print(q[['zstop','exec','period','cagr','sharpe','maxdd_intrabar','worst_day','trades','legged','maker_miss','fees','funding','exposure','avg_gross_lev','stops','timeouts','forced']].round(4).to_string(index=False))
s = json.load(open('out/selections.json'))
names = sorted(f[:-8] for f in __import__('os').listdir('data/h'))
for k in ['coint|lvl|60', 'corr|ret|120']:
    for t in ['2022-04-01 00:00:00', '2022-05-01 00:00:00', '2022-11-01 00:00:00']:
        print(k, t[:10], [(names[a], names[b]) for a, b, be in s[k][t][:10] if 'LUNAUSDT' in (names[a], names[b]) or 'FTTUSDT' in (names[a], names[b]) or 'SOLUSDT' in (names[a], names[b])])
