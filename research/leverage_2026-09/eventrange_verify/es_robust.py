"""Verifier: robustness of the claimed 'only robust effect' (long-only buying of 5-min >=10 sigma dumps with volume
spike). Same event definition and entry as event_study.py (next open after the signal bar, non-overlapping >=240 min
per coin), long side only. Reports naive t, day-clustered t (events on the same UTC day pooled across coins),
leave-one-coin-out, leave-one-month-out min, share of the total from the top 5 days, and a net figure after a
taker round trip (2 x 5 bp fee + 2 x base slippage + 5% of the entry/exit bar ranges)."""
import numpy as np, pandas as pd, itertools, json
from common import *
from signals import wick_events
D = {c: load(c) for c in COINS}
rows = []
evlog = []
for V in (3, 10):
    for per, (a, b) in dict(IS=(IS0, IS1), OOS=(OOS0, OOS1)).items():
        recs = []
        for coin in COINS:
            d = D[coin]; i0, i1 = window(d, a, b)
            idx, dr, mf = wick_events(d, 5, 10, V)
            m = (idx >= i0) & (idx < i1 - 241); idx, dr = idx[m], dr[m]
            keep = []; last = -10**9
            for j, i in enumerate(idx):
                if i - last >= 240:
                    keep.append(j); last = i
            idx, dr = idx[keep], dr[keep]
            idx = idx[dr == 1]
            ent = d['o'][idx + 1]
            for k in (15, 60):
                pass
            f15 = d['c'][idx + 15] / ent - 1; f60 = d['c'][idx + 60] / ent - 1
            rng_in = (d['h'][idx + 1] - d['l'][idx + 1]) / d['o'][idx + 1]
            rng15 = (d['h'][idx + 16] - d['l'][idx + 16]) / d['o'][idx + 16]
            rng60 = (d['h'][idx + 61] - d['l'][idx + 61]) / d['o'][idx + 61]
            cost15 = 2 * FEE_T + 2 * BASE_SLIP[coin] + RANGE_SLIP * (rng_in + rng15)
            cost60 = 2 * FEE_T + 2 * BASE_SLIP[coin] + RANGE_SLIP * (rng_in + rng60)
            for j, i in enumerate(idx):
                recs.append(dict(coin=coin, t=int(d['t'][i]), f15=f15[j], f60=f60[j], c15=cost15[j], c60=cost60[j]))
        df = pd.DataFrame(recs)
        df['day'] = pd.to_datetime(df.t, unit='ms').dt.floor('D'); df['month'] = pd.to_datetime(df.t, unit='ms').dt.to_period('M')
        for h in (15, 60):
            x = df[f'f{h}']; net = x - df[f'c{h}']
            dm = df.groupby('day')[f'f{h}'].mean()
            t_naive = x.mean() / x.std() * np.sqrt(len(x))
            t_day = dm.mean() / dm.std() * np.sqrt(len(dm))
            loco = {c: 1e4 * x[df.coin != c].mean() for c in COINS}
            lomo = {str(mm): 1e4 * x[df.month != mm].mean() for mm in df.month.unique()}
            daysum = df.groupby('day')[f'f{h}'].sum().sort_values(ascending=False)
            top5 = daysum.head(5)
            rows.append(dict(V=V, period=per, h=h, n=len(x), n_days=len(dm), gross_bp=1e4 * x.mean(), median_bp=1e4 * x.median(),
                             t_naive=t_naive, t_dayclustered=t_day, net_bp=1e4 * net.mean(), mean_cost_bp=1e4 * df[f'c{h}'].mean(),
                             t_net_day=(df.assign(n=net).groupby('day').n.mean().mean() / df.assign(n=net).groupby('day').n.mean().std() * np.sqrt(len(dm))),
                             loco_min_bp=min(loco.values()), loco_min_coin=min(loco, key=loco.get), lomo_min_bp=min(lomo.values()),
                             lomo_min_month=min(lomo, key=lomo.get),
                             gross_bp_ex_top5days=1e4 * x[~df.day.isin(top5.index)].mean(),
                             top5_days=', '.join(f'{k.date()}' for k in top5.index),
                             share_sum_top5=top5.sum() / x.sum() if x.sum() != 0 else np.nan))
out = pd.DataFrame(rows)
pd.set_option('display.width', 300); pd.set_option('display.max_columns', 40); pd.set_option('display.max_colwidth', 80)
print(out.round(2).to_string())
out.to_csv('out/verify_event_study_robust.csv', index=False)
