"""Pre-registered TSMOM grid (72 configs). Selection rule (fixed before looking at OOS): highest IS (2022-01..2024-12)
net Sharpe at 1x.  Every config's IS and OOS numbers are saved (grid distribution)."""
import itertools, json
import numpy as np, pandas as pd
import tsmom as M

LBS = {'all': [10, 20, 40, 60, 120], 'short': [10, 20, 40], 'long': [40, 60, 120]}
rows = []
cache = {}
daily = {}
for N, lb, kind, hl, band in itertools.product([20, 30, 40], ['all', 'short', 'long'], ['sign', 'z'], [20, 60],
                                                [0.0, 0.5]):
    cfg = dict(N=N, lbs=LBS[lb], kind=kind, hl=hl)
    key = ('w', N, lb, kind, hl)
    if key not in cache:
        cache[key] = M.target_weights(cfg, cache)
    Wt, mem, rank = cache[key]
    df = M.backtest(Wt, rank, band=band)
    name = f'N{N}_{lb}_{kind}_hl{hl}_b{band}'
    si = M.stats(df, M.IS_START, M.IS_END)
    so = M.stats(df, M.OOS_START, M.OOS_END)
    gross_is = M.sharpe(df.loc[M.IS_START:M.IS_END, 'ret'] + df.loc[M.IS_START:M.IS_END, 'cost'])
    gross_oos = M.sharpe(df.loc[M.OOS_START:M.OOS_END, 'ret'] + df.loc[M.OOS_START:M.OOS_END, 'cost'])
    yr = df.ret.groupby(df.index.year).sum()
    rows.append(dict(name=name, N=N, lbs=lb, kind=kind, hl=hl, band=band,
                     sharpe_is=si['sharpe'], sharpe_oos=so['sharpe'], cagr_is=si['cagr'], cagr_oos=so['cagr'],
                     vol_is=si['vol'], vol_oos=so['vol'], maxdd_close_is=si['maxdd_close'],
                     maxdd_close_oos=so['maxdd_close'], gross_is=si['gross'], turnover_is=si['turnover_ann'],
                     cost_is=si['cost_ann'], fund_is=si['fund_ann'], cost_oos=so['cost_ann'],
                     fund_oos=so['fund_ann'], sharpe_is_precost=gross_is, sharpe_oos_precost=gross_oos,
                     **{f'sum_{y}': float(v) for y, v in yr.items()}))
    daily[name] = df.ret
    print(name, round(si['sharpe'], 2), round(so['sharpe'], 2), flush=True)
G = pd.DataFrame(rows).sort_values('sharpe_is', ascending=False)
G.to_csv(f'{M.W}/grid_tsmom.csv', index=False)
pd.DataFrame(daily).to_csv(f'{M.W}/grid_tsmom_daily.csv')
print(G.head(10).to_string())
print('IS Sharpe distribution', G.sharpe_is.describe().to_dict())
print('OOS Sharpe distribution', G.sharpe_oos.describe().to_dict())
print('rank corr IS vs OOS', G[['sharpe_is', 'sharpe_oos']].corr(method='spearman').iloc[0, 1])
best = G.iloc[0]
json.dump(best.to_dict(), open(f'{M.W}/tsmom_selected.json', 'w'), indent=1, default=float)
