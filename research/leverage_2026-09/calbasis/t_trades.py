import pickle, numpy as np, pandas as pd, cb_backtest as cb
AS=cb.load_all()
p=dict(signal='net', h_in=0.08, h_out=0.02, tau_min=30)
for a in ['BTC','ETH']:
    s=cb.Sim(AS[a],p,1,'cross',start='2022-01-01',end='2024-12-31 23:55').run()
    tr=pd.DataFrame(s.trades); N=tr.notional
    tr['spread%']=(tr.fut_pnl+tr.perp_pnl)/N*100; tr['fund%']=tr.fund/N*100; tr['net%']=(tr.Eend/tr.E0-1)*100; tr['prem_in%']=tr.prem_in*100
    print(tr[['con','side','entry','exit','why','sig_in','prem_in%','spread%','fund%','net%']].round(3).to_string())
