"""List all trades of one config (BTC+ETH or given coins) with costs and per-leverage outcomes; save CSV."""
import sys, numpy as np, pandas as pd
import run_grid as R, engine as E
from common import T0
cfg = tuple(eval(sys.argv[1])); coins = sys.argv[2].split(','); tag = sys.argv[3]
rows = []
for c in coins:
    P = R.prep(c)
    for name, lat, mode, hm in R.EXECS:
        tr = R.trades_for(P, c, cfg, mode, lat, hedge_mid=hm)
        for r in tr:
            d = dict(coin=c, exec=name, lat=lat, t=T0 + pd.Timedelta(minutes=int(r[0])), side=int(r[3]), dev_bp=r[12]*1e4, b0_bp=r[11]*1e4,
                     hold_min=int(r[2]-r[1]), gross_bp=r[4]*1e4, fees_bp=r[5]*1e4, slip_bp=r[6]*1e4, fund_bp=r[7]*1e4,
                     net_bp=(r[4]-r[5]-r[6]+r[7])*1e4, mk_in=r[9], mk_out=r[10])
            for m in ['pm', 'mc', 'sep']:
                for li, L in enumerate(E.LEVS):
                    d[f'{m}_ret{int(L)}'] = r[E.col(m,'ret')+li]; d[f'{m}_worst{int(L)}'] = r[E.col(m,'worst')+li]
                    d[f'{m}_liq{int(L)}'] = r[E.col(m,'liq')+li]; d[f'{m}_rej{int(L)}'] = r[E.col(m,'rej')+li]
            rows.append(d)
df = pd.DataFrame(rows).sort_values(['exec','lat','t'])
df.to_csv(f'{R.OUTDIR}/trades_{tag}.csv', index=False, float_format='%.6g')
pd.set_option('display.width', 250); pd.set_option('display.max_rows', 300)
for (ex, lat), g in df.groupby(['exec','lat']):
    print('====', ex, lat)
    g = g.copy(); g['seg'] = np.where(g.t < pd.Timestamp('2025-01-01', tz='UTC'), 'IS', 'OOS')
    print(g[['coin','t','side','dev_bp','b0_bp','hold_min','gross_bp','fees_bp','slip_bp','fund_bp','net_bp','mk_in','mk_out','pm_ret20','pm_worst20','pm_liq20']].round(2).to_string(index=False))
    for s, h in g.groupby('seg'):
        print(s, 'n', len(h), 'mean net %.1f bp, median %.1f, t=%.2f, hit %.2f' % (h.net_bp.mean(), h.net_bp.median(), h.net_bp.mean()/h.net_bp.std()*np.sqrt(len(h)) if len(h)>1 else np.nan, (h.net_bp>0).mean()))
