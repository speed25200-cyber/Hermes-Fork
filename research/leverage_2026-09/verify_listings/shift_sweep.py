"""Noise diagnostic: shift entry/exit of the selected config by -6..+6 hours (same holding length) and report IS/OOS Sharpe.
Not a selection device: only to quantify how much the OOS Sharpe moves under trivial perturbations."""
from sim_v import *
Dt = Data('hybrid2')
rows = []
for s in range(-6, 7):
    c = dict(d0=72 + s, d1=168 + s, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
    a = summarize(simulate(Dt, c, IS_START, OOS_START)); b = summarize(simulate(Dt, c, OOS_START, OOS_END))
    r = simulate(Dt, c, OOS_START, OOS_END)['ret']; m = r.index.to_period('M')
    lomo = min(sharpe(r[m != p]) for p in m.unique())
    rows.append(dict(shift_h=s, is_sharpe=a['sharpe'], oos_sharpe=b['sharpe'], y2025=b['years'][2025], y2026=b['years'][2026], lomo_min=lomo, oos_trades=b['trades'], oos_stops=sum(t['reason'] == 'stop' for t in simulate(Dt, c, OOS_START, OOS_END)['trades'])))
X = pd.DataFrame(rows).round(3); print(X.to_string())
print('OOS Sharpe across shifts: median %.3f min %.3f max %.3f; pass bar1 %d/13; lomo>=1 %d/13' % (X.oos_sharpe.median(), X.oos_sharpe.min(), X.oos_sharpe.max(), ((X.oos_sharpe >= 1.5) & (X.y2025 > 0) & (X.y2026 > 0)).sum(), (X.lomo_min >= 1).sum()))
X.to_csv('shift_sweep_hybrid2.csv', index=False)
# bootstrap CI of OOS Sharpe (daily, stationary blocks of 10 days)
r = simulate(Dt, dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5), OOS_START, OOS_END)['ret'].values
rng = np.random.default_rng(0); n = len(r); B = 2000; out = []
for _ in range(B):
    idx = []
    while len(idx) < n:
        st = rng.integers(n); idx += list(range(st, min(st + 10, n)))
    x = r[np.array(idx[:n])]; out.append(x.mean() / x.std(ddof=1) * np.sqrt(365))
print('OOS Sharpe block-bootstrap 5/50/95%%: %.2f / %.2f / %.2f ; P(Sharpe>=1.5) = %.2f' % (*np.quantile(out, [.05, .5, .95]), np.mean(np.array(out) >= 1.5)))
