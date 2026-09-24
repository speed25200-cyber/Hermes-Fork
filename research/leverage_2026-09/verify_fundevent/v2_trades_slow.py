"""Verifier step 2: trade log of the selected slow config at 1x and 3x (IS and OOS): per-coin and per-month
P&L concentration, share of hours held with missing 1h data (no liquidation check possible in those hours)."""
import json, sys, os
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vslow as fs
HERE = os.path.dirname(os.path.abspath(__file__))
fs.load(); G = fs.G
cfg = (2.0, 0.0, 10, 'btc', 'both', 'all')
H0 = fs.H0
out = []
for L in [1, 3]:
    for per, (t0, t1) in (('IS', (fs.IS0, fs.IS1)), ('OOS', (fs.IS1, fs.OOS1))):
        G['log_trades'] = []
        eqc, eqt, ntr, nliq, fsum, csum = fs.simulate(cfg, L, t0, t1)
        tr = pd.DataFrame(G['log_trades'], columns=['h0', 'h1', 'sym', 's', 'm0', 'mend'])
        tr['ret'] = tr.mend / tr.m0 - 1
        tr['pnl'] = tr.mend - tr.m0
        tr['L'] = L; tr['per'] = per
        # hours held with NaN high (no liquidation check)
        si = {s: i for i, s in enumerate(G['syms'])}
        nanh = []
        for r in tr.itertuples():
            i = si[r.sym]
            seg = G['A_h'][i, r.h0:r.h1 + 1]
            nanh.append(int((~np.isfinite(seg)).sum()))
        tr['nan_hours'] = nanh
        tr['hold_h'] = tr.h1 - tr.h0
        out.append(tr)
        eqd = pd.Series(eqc[23::24], index=pd.date_range(t0, periods=len(eqc[23::24]), freq='D'))
        mret = eqd.resample('ME').last().pct_change()
        mret.iloc[0] = eqd.resample('ME').last().iloc[0] / 1.0 - 1
        by = tr.groupby('sym').pnl.sum().sort_values()
        print(f'L={L} {per}: trades {len(tr)} liq {nliq} final {eqc[-1]:.3f} funding {fsum:.3f} costs {csum:.3f} '
              f'| sum pnl {tr.pnl.sum():.3f} top5 {by.tail(5).round(2).to_dict()} worst5 {by.head(5).round(2).to_dict()}')
        print('   mean ret/trade %.4f median %.4f win %.3f | NaN-hours share %.4f, trades with NaN hours %d' % (
            tr.ret.mean(), tr.ret.median(), (tr.ret > 0).mean(), tr.nan_hours.sum() / max(tr.hold_h.sum(), 1), (tr.nan_hours > 0).sum()))
        print('   monthly returns:', ' '.join(f'{d:%y-%m}:{v:+.2f}' for d, v in mret.items()))
        best = mret.idxmax()
        nd = (t1 - t0).days
        ex = np.prod(1 + mret.drop(best)) ** (365.0 / (nd - 30)) - 1
        print(f'   best month {best:%Y-%m} {mret.max():+.3f}; CAGR without best month ~ {ex:+.3f}; without best 2 months ~ '
              f'{np.prod(1 + mret.drop(mret.nlargest(2).index)) ** (365.0 / (nd - 61)) - 1:+.3f}', flush=True)
pd.concat(out).to_csv(os.path.join(HERE, 'v2_trades_slow.csv'), index=False)
