"""Pre-market trades: share of the hold before TGE; and trade returns when re-anchored at TGE (Binance-only, full H)."""
import sys
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
sys.path.insert(0, SP + '/review_sleeve'); sys.path.insert(0, SP + '/newlisting')
import sim, numpy as np, pandas as pd
from livesim import simulate_live, sharpe
HERE = SP + '/xlist/verify/premkt'
KW = dict(tranches=(24, 72), d1=168, stop=0.5, K=5, late=True, max_late=2, shared_stop=True)
anc = pd.read_csv(HERE + '/bn_pm_anchor.csv', parse_dates=['t0', 'tge'])
t = pd.read_csv(HERE + '/bn_trades_base.csv')
t = t.merge(anc[['i', 'tge_h']], on='i')
D = sim.Data('hybrid')
t['k_in'] = t.entry_t - D.g0[t.i]; t['k_out'] = t.exit_t - D.g0[t.i]
t['state'] = np.where(t.k_out <= t.tge_h, 'all pre-TGE', np.where(t.k_in >= t.tge_h, 'all post-TGE', 'spans TGE'))
rows = []
for _, r in t.iterrows():
    i = int(r.i); kt = int(np.floor(r.tge_h))
    if r.state == 'spans TGE':
        px_tge = D.o[i, kt]
        rows.append(dict(sym=r.sym, tr=r.tranche, pre=-(px_tge / r.entry_px - 1), post_ret=r.ret))
print(t.groupby('state').ret.agg(['count', 'mean']).round(4))
print(pd.DataFrame(rows).round(3).to_string())
# re-anchored version of the same events (full-H Binance-only 2025-2026)
D2 = sim.Data('hybrid'); D2.sigma_ref = 0.1238230231575359
for _, r in anc.iterrows():
    i = int(r.i); s = int((r.tge.floor('h') - r.t0).total_seconds() // 3600)
    for k in ('o', 'h', 'l', 'c', 'bo', 'bh', 'bl', 'bc', 'fund', 'okx_on'):
        A = getattr(D2, k); fill = False if A.dtype == bool else (0.0 if k == 'fund' else np.nan)
        row = A[i, s:].copy(); A[i, :] = fill; A[i, :len(row)] = row
    D2.g0[i] += s
lr = np.diff(np.log(D2.c), axis=1); lr[:, 0] = np.nan; m = np.isfinite(lr); x = np.where(m, lr, 0.0)
cs, cs2, cn = np.cumsum(x, 1), np.cumsum(x * x, 1), np.cumsum(m, 1)
D2.vol_d = np.sqrt(np.maximum((cs2 - cs ** 2 / np.maximum(cn, 1)) / np.maximum(cn - 1, 1), 0) * 24); D2.vol_n = cn
r2 = simulate_live(D2, start='2025-01-01', end='2026-09-01', **KW)
t2 = pd.DataFrame(r2['trades'])
x2 = t2[t2.i.isin(anc.i)]
print('re-anchored pm trades', len(x2), 'mean', round(x2.ret.mean(), 4), 't', round(x2.ret.mean() / x2.ret.std() * np.sqrt(len(x2)), 2))
print(x2[['sym', 'tranche', 'ret', 'reason']].round(3).to_string())
o2 = t2[~t2.i.isin(anc.i)]; print('other trades in reanchor run', len(o2), round(o2.ret.mean(), 4))
