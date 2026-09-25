"""Regime persistence checks at event level (short vs BTC, +d0 -> +168h): (1) split by BTC 30d return sign known at
entry, (2) does the trailing mean of the last 5 COMPLETED events (exit before this entry) predict the next event?
-> regime2.json"""
import json, numpy as np, pandas as pd
from scipy.stats import spearmanr
X = pd.read_parquet('events_d7.parquet')
import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
h1 = pd.read_parquet('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting/data/h1.parquet', filters=[('sym', '==', 'BTCUSDT')])
bc = pd.Series(h1.c.values, pd.to_datetime(h1.t.values, unit='ms')).resample('D').last()
b30 = (bc / bc.shift(30) - 1).shift(1)
out = {}
for d0 in (24, 72):
    Y = X[X.d0 == d0].sort_values('t').reset_index(drop=True)
    Y['btc30'] = Y.t.dt.normalize().map(b30)
    for per, m in (('IS', Y.t < '2025-01-01'), ('OOS', Y.t >= '2025-01-01')):
        Z = Y[m]
        for nm, mm in (('btc_up', Z.btc30 > 0), ('btc_down', Z.btc30 <= 0)):
            out[f'd0={d0}_{per}_{nm}'] = dict(n=int(mm.sum()), mean=float(Z.short_btc[mm].mean()), median=float(Z.short_btc[mm].median()))
            print(f'd0={d0} {per} {nm}: n={int(mm.sum())} mean short vs BTC {Z.short_btc[mm].mean():+.4f} median {Z.short_btc[mm].median():+.4f}')
    exit_t = Y.t + pd.Timedelta(hours=168 - d0)
    trail = []
    for i, r in Y.iterrows():
        done = Y[(exit_t < r.t)]
        trail.append(done.short_btc.tail(5).mean() if len(done) >= 5 else np.nan)
    Y['trail5'] = trail
    Z = Y.dropna(subset=['trail5'])
    rho = spearmanr(Z.trail5, Z.short_btc)
    hi, lo = Z[Z.trail5 > 0], Z[Z.trail5 <= 0]
    out[f'd0={d0}_trail5'] = dict(n=len(Z), spearman=float(rho.correlation), p=float(rho.pvalue), mean_after_pos=float(hi.short_btc.mean()), n_pos=len(hi),
                                  mean_after_neg=float(lo.short_btc.mean()), n_neg=len(lo))
    print(f'd0={d0} trailing-5 completed events -> next: spearman {rho.correlation:.3f} (p {rho.pvalue:.2f}); after trail>0 mean {hi.short_btc.mean():+.4f} (n {len(hi)}), after trail<=0 {lo.short_btc.mean():+.4f} (n {len(lo)})')
json.dump(out, open('regime2.json', 'w'), indent=1)
