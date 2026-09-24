"""Does the Binance funding extreme exist on OKX (the deployment venue)?
For coins listed on both, per UTC day: sum of Binance funding rates vs sum of OKX realised funding rates
(OKX static swaprate files, 2022-01 .. 2025-09-07). Restricted to days where Binance's daily funding
annualised |.| >= thr. Output: okx_vs_binance.csv (by threshold) and printed summary.
"""
import os, json
import numpy as np, pandas as pd
from common import D
from prep import coin_of

st = pd.read_parquet(os.path.join(D, 'settle.parquet'), columns=['sym', 't', 'rate'])
of = pd.read_parquet('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/data/okx_funding_all.parquet')
of['coin'] = of.instId.str.split('-').str[0]
of['day'] = of.funding_time // 86400000
okd = of.groupby(['coin', 'day']).funding_rate.agg(['sum', 'count']).rename(columns={'sum': 'okx_sum', 'count': 'okx_n'})
okx_coins = set(of.coin)
st['coin'] = [coin_of(s, okx_coins) for s in st.sym]
st['day'] = st.t // 86400000
bd = st.groupby(['sym', 'coin', 'day']).rate.agg(['sum', 'count']).rename(columns={'sum': 'bn_sum', 'count': 'bn_n'}).reset_index()
# 1000-prefix contracts: rates are per contract value, directly comparable
j = bd.merge(okd.reset_index(), on=['coin', 'day'], how='inner')
j = j[(j.bn_n >= 3) & (j.okx_n >= 3)]
last = int(of.day.max())
j = j[j.day < last]
rows = []
for thr in [0.5, 1.0, 2.0, 4.0]:
    m = (j.bn_sum.abs() * 365 >= thr)
    x = j[m]
    same_sign = (np.sign(x.bn_sum) == np.sign(x.okx_sum)).mean()
    ratio = (x.okx_sum * np.sign(x.bn_sum)).sum() / x.bn_sum.abs().sum()
    rows.append({'thr_ann_binance': thr, 'coin_days': int(m.sum()), 'coins': int(x.coin.nunique()),
                 'binance_ann_mean': float((x.bn_sum.abs() * 365).mean()),
                 'okx_ann_same_direction_mean': float((x.okx_sum * np.sign(x.bn_sum) * 365).mean()),
                 'okx_over_binance_ratio': float(ratio), 'same_sign_frac': float(same_sign),
                 'okx_also_above_thr_frac': float(((x.okx_sum * np.sign(x.bn_sum)) * 365 >= thr).mean())})
df = pd.DataFrame(rows)
df.to_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'okx_vs_binance.csv'), index=False)
print(df.round(3).to_string())
