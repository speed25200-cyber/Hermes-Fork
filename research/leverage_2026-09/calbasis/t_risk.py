import pickle, numpy as np, pandas as pd, cb_backtest as cb
from evaluate import CFG
AS=cb.load_all()
for cfg in ['short_B','two_B']:
    for per,(a,b) in [('IS',('2022-01-01','2024-12-31 23:55')),('OOS',('2025-01-01','2026-08-31 23:55'))]:
        sims,m,ec=cb.run_assets([AS['BTC'],AS['ETH']],CFG[cfg],20,'cross',a,b)
        for s,nm in zip(sims,['BTC','ETH']):
            e,l=cb.equity_series([s],[1.0],a,b)
            d=e.resample('1D').last(); r=d.pct_change()
            pk=e.cummax(); dd=np.minimum(l,e)/pk-1
            print(cfg,per,nm,'CAGR',round(cb.metrics_from(e,l)['cagr']*100,1),'worst days:',{str(k.date()):round(v*100,1) for k,v in r.nsmallest(3).items()},
                  'maxDD at',dd.idxmin(),round(dd.min()*100,1))
        # margin headroom: min equity/MM ratio
