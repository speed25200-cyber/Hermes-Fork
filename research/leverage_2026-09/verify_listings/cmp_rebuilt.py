import sys
from sim_v import *
Dh = Data('hybrid'); D2 = Data('hybrid2')
cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
a = simulate(Dh, cfg, OOS_START, OOS_END); b = simulate(D2, cfg, OOS_START, OOS_END)
ta = pd.DataFrame(a['trades']).set_index('sym'); tb = pd.DataFrame(b['trades']).set_index('sym')
syms = ['GRIFFAINUSDT', 'AI16ZUSDT', 'ZEREBROUSDT', 'ALCHUSDT', 'SONICUSDT', 'SOLVUSDT', 'VINEUSDT', 'IPUSDT', 'BRUSDT', 'GUNUSDT', 'PROMPTUSDT', 'MEMEFIUSDT', 'OLUSDT', 'TREEUSDT', 'XANUSDT', 'BLUAIUSDT', 'TURTLEUSDT', 'KITEUSDT']
rows = []
for s in syms:
    i = int(ta.loc[s, 'i']); g0 = Dh.g0[i]; k0 = int(ta.loc[s, 'entry_t'] - g0); k1 = min(k0 + 96, Dh.H - 1)
    bn = Dh.c[i, k0:k1]; ok = D2.c[i, k0:k1]
    m = np.isfinite(bn) & np.isfinite(ok)
    rows.append(dict(sym=s, bn_ret=ta.loc[s, 'ret'], bn_reason=ta.loc[s, 'reason'], okx_ret=tb.loc[s, 'ret'] if s in tb.index else np.nan,
                     okx_reason=tb.loc[s, 'reason'] if s in tb.index else None, okx_hours=int(D2.src_okx[i, k0:k1].sum()), n=k1 - k0,
                     med_logdiff=float(np.median(np.log(ok[m] / bn[m]))) if m.any() else np.nan,
                     max_high_ratio=float(np.nanmax(D2.h[i, k0:k1] / Dh.h[i, k0:k1]))))
X = pd.DataFrame(rows); pd.set_option('display.width', 200)
print(X.to_string())
print('mean ret binance %.4f okx-rebuilt %.4f' % (X.bn_ret.mean(), X.okx_ret.mean()))
print('hybrid', summarize(a)['sharpe'], 'hybrid2', summarize(b)['sharpe'], summarize(b)['years'], summarize(b)['maxdd'])
X.to_csv('rebuilt_vs_binance_oos.csv', index=False)
