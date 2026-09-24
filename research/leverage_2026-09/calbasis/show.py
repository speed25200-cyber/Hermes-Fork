import pandas as pd, sys
df=pd.read_csv('results_all.csv')
pd.set_option('display.width',250)
def tab(cfg, mode='cross', scope='BTC+ETH', cost='base'):
    d=df[(df.config==cfg)&(df['mode']==mode)&(df.scope==scope)&(df.cost==cost)]
    rows=[]
    for L in sorted(d.L.unique()):
        i=d[(d.L==L)&(d.period=='IS')].iloc[0]; o=d[(d.L==L)&(d.period=='OOS')].iloc[0]; f=d[(d.L==L)&(d.period=='FULL')].iloc[0]
        rows.append(dict(L=L, cagr_IS=i.cagr*100, cagr_OOS=o.cagr*100, dd_IS=i.maxdd*100, dd_OOS=o.maxdd*100, sh_IS=i.sharpe, sh_OOS=o.sharpe,
            wd_IS=i.worst_day*100, wd_OOS=o.worst_day*100, liq_IS=i.nliq, liq_OOS=o.nliq, tr_IS=f"{i.nentry}+{i.nroll}r", tr_OOS=f"{o.nentry}+{o.nroll}r",
            **{f'y{y}':f[f'y{y}']*100 for y in range(2022,2027)}, rb=o.nrb))
    print(f'--- {cfg} | {mode} | {scope} | cost={cost}'); print(pd.DataFrame(rows).round(1).to_string(index=False))
for cfg in sys.argv[1].split(','):
    for mode in sys.argv[2].split(','):
        for scope in (sys.argv[3].split(',') if len(sys.argv)>3 else ['BTC+ETH']):
            tab(cfg,mode,scope, sys.argv[4] if len(sys.argv)>4 else 'base')
