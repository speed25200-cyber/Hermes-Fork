import pickle, numpy as np, pandas as pd, cb_backtest as cb
AS=cb.load_all()
IS=('2022-01-01','2024-12-31 23:55'); OOS=('2025-01-01','2026-08-31 23:55')
def show(tag,p,L=1,mode='cross'):
    for nm,(a,b) in [('IS',IS),('OOS',OOS)]:
        for asset in ['BTC','ETH']:
            sims,m,_=cb.run_assets([AS[asset]],p,L,mode,a,b)
            tr=pd.DataFrame(sims[0].trades)
            extra=''
            if len(tr):
                N=tr.notional
                extra=f" fut+perp%={((tr.fut_pnl+tr.perp_pnl)/N).sum()*100:.2f} fund%={(tr.fund/N).sum()*100:.2f}"
            print(tag,nm,asset,f"cagr={m['cagr']*100:.2f}% dd={m['maxdd']*100:.1f}% sh={m['sharpe']:.2f} n={m['nentry']}/{m['nroll']}"+extra)
show('always_short',dict(signal='raw',h_in=-9,h_out=None,tau_min=14))
show('always_long ',dict(signal='raw',h_in=None,h_in_rev=9,h_out=None,tau_min=14))
