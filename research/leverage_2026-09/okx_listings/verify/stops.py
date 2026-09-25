import sys; sys.path.insert(0,'.')
from dl import *
import numpy as np
SP='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist'
H=3600000; G0=pd.Timestamp('2021-12-01').value//10**6
sel=pd.read_csv('sel.csv'); bars=pd.read_pickle('okx_bars.pkl')
rows=[]
for _,r in sel.iterrows():
    b=bars[r.sym]; t0=int(r.t0); g0=(t0-G0)//H
    ke=int(r.entry_t-g0); kx=int(r.exit_t-g0)
    e_px=b.o.get(ke)
    rec=dict(sym=r.sym,tr=r.tranche,reason=r.reason,ke=ke,kx=kx,entry_author=r.entry_px,entry_mine=e_px,stop=r.stop_px)
    tr=pd.read_parquet(f'cache/{r.sym}.trades.parquet')
    if r.reason=='stop':
        # max high between first entry of listing and the stop bar (exclusive)
        k_first=int(sel[(sel.sym==r.sym)].ke.min()) if 'ke' in sel else None
        hb=b.loc[ke:kx-1,'h'].max()
        s=tr[(tr.created_time>=t0+kx*H)&(tr.created_time<t0+(kx+1)*H)]
        above=s[s.price>=r.stop_px]
        first_cross=above.created_time.min()
        # time (min) price stayed >= stop in bar: fraction of trades above, minutes from first cross to last above
        rec.update(prev_max_high=hb,stop_bar_o=b.o[kx],stop_bar_h=b.h[kx],stop_bar_c=b.c[kx],n_trades_bar=len(s),n_above=len(above),
                   sz_above=above['size'].sum(),sz_bar=s['size'].sum(),
                   first_cross=pd.to_datetime(first_cross,unit='ms'),mins_above_span=(above.created_time.max()-first_cross)/60000,
                   close_vs_stop=b.c[kx]/r.stop_px-1, next_bars_high_vs_stop=b.loc[kx+1:kx+6,'h'].max()/r.stop_px-1,
                   next6_close_max_vs_stop=b.loc[kx:kx+6,'c'].max()/r.stop_px-1)
    else:
        rec.update(exit_mine=b.o.get(kx),exit_author=r.exit_px_implied,maxhigh_hold_vs_stop=b.loc[ke:kx-1,'h'].max()/r.stop_px-1)
    rows.append(rec)
d=pd.DataFrame(rows); pd.set_option('display.width',300); pd.set_option('display.max_columns',40)
d.to_csv('stops_check.csv',index=False)
print(d[d.reason=='stop'].drop(columns=['exit_mine','exit_author','maxhigh_hold_vs_stop'],errors='ignore').to_string())
print(d[d.reason=='end'][['sym','tr','ke','kx','entry_author','entry_mine','exit_author','exit_mine','stop','maxhigh_hold_vs_stop']].to_string())
