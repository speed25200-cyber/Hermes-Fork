"""OKX 1H last-price candles (with volume) for MET/RE-USDT-SWAP from perp start to TGE+250h -> okx_pm_h1.parquet"""
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist")
from common import okx_get
import pandas as pd
HOUR = 3600000
jobs = {'MET-USDT-SWAP': ('2025-10-10 08:00', '2025-10-23 14:00'), 'RE-USDT-SWAP': ('2026-06-17 10:00', '2026-06-18 14:00')}
out = []
for inst, (a, tge) in jobs.items():
    a = pd.Timestamp(a).value // 10**6
    b = pd.Timestamp(tge).value // 10**6 + 250 * HOUR
    rows, after = [], b + HOUR
    for _ in range(80):
        d = okx_get(f'/api/v5/market/history-candles?instId={inst}&bar=1H&limit=100&after={after}')
        if not d:
            break
        rows += d
        after = int(d[-1][0])
        if after <= a:
            break
    df = pd.DataFrame([[int(x[0])] + [float(v) for v in x[1:8]] for x in rows],
                      columns=['t', 'o', 'h', 'l', 'c', 'vol', 'volCcy', 'volQuote'])
    df = df[(df.t >= a) & (df.t <= b)].drop_duplicates('t').sort_values('t')
    df['instId'] = inst
    out.append(df)
    print(inst, len(df), pd.to_datetime(df.t.min(), unit='ms'), pd.to_datetime(df.t.max(), unit='ms'))
pd.concat(out).to_parquet('okx_pm_h1.parquet')
