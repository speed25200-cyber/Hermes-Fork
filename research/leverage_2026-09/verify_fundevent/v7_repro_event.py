"""Verifier step 7: reproduce the IS-selected rule-B configs of families (i) and (ii) with the researcher's engine
(vevent.py = unchanged copy of fe_event.py), and measure how often a pre-settlement (ii) maker exit (b=-1)
is only filled AFTER the settlement (position then held through t with no funding booked)."""
import os, sys, json
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vevent as fe
HERE = os.path.dirname(os.path.abspath(__file__))
ev, W, WB = fe.load()
T = ev.t.values
rows = []
SPECS = [('i_ruleB_1x', (-30, 5, 'recv'), 'pred', 'none', 'maker', 'all', 4.0, 3),
         ('ii_ruleB_1x', (-60, -1, 'recv'), 'pred', 'none', 'maker', 'all', 4.0, 1),
         ('ii_pay_pre_maker', (-60, -1, 'pay'), 'pred', 'none', 'maker', 'all', 4.0, 1)]
for name, (a, b, d), sig, hedge, execm, uni, thr, K in SPECS:
    tr = fe.trades(ev, W, WB, a, b, d, sig, hedge, execm)
    absig = np.nan_to_num(np.abs(fe.signal(ev, sig, a)))
    order = np.lexsort((-absig, T))
    for L in fe.LEVS:
        el = fe.eligibility(ev, L, hedge, uni) & tr['traded']
        P = fe.portfolio(T, absig, tr['r'], tr['z'], tr['trough'], el, thr, K, L, order)
        mi = fe.metrics(*P, fe.IS0, fe.IS1); mo = fe.metrics(*P, fe.IS1, fe.OOS1)
        rows.append({'name': name, 'L': L, 'cagr_is': mi['cagr'], 'cagr_oos': mo['cagr'], 'maxdd_oos': mo['maxdd'],
                     'worst_day_oos': mo['worst_day'], 'sharpe_oos': mo['sharpe'], 'liq_oos': mo['liq'], 'trades_is': mi['trades'], 'trades_oos': mo['trades']})
        print(rows[-1], flush=True)
    sel = tr['traded'] & (absig >= thr)
    per = np.where(T < fe.IS1.value // 10 ** 6, 'IS', 'OOS')
    for p in ['IS', 'OOS']:
        m = sel & (per == p)
        print(f'  {name} {p}: n {m.sum()} mean r {np.nanmean(tr["r"][m]) * 1e4:.1f}bp  px {np.nanmean(tr["px"][m]) * 1e4:.1f} fund {np.nanmean(tr["fund"][m]) * 1e4:.1f} cost {np.nanmean(tr["cost"][m]) * 1e4:.1f}', flush=True)
# maker exit after t for b = -1 specs: recompute the exit-fill bar exactly as the engine does
PRE, NB = fe.PRE, fe.NB
for d in ['recv', 'pay']:
    a, b = -60, -1
    s_all = np.nan_to_num(np.sign(fe.signal(ev, 'pred', a))); s_all = -s_all if d == 'recv' else s_all
    absig = np.abs(np.nan_to_num(fe.signal(ev, 'pred', a)))
    idx = np.nonzero(absig >= 4.0)[0]
    Wc = np.asarray(W[idx], dtype=np.float64)
    O, H, L_ = Wc[:, :, 0], Wc[:, :, 1], Wc[:, :, 2]
    tk = fe.tick_rel(Wc) * np.nanmedian(Wc[:, :, 3], axis=1)
    s = s_all[idx]
    jb = b + PRE; Wn = 15
    J = np.arange(NB)[None, :]
    Pxl = O[:, jb]
    cand2 = (J >= jb) & (J < jb + Wn)
    hit2 = np.where(s[:, None] > 0, H >= (Pxl + tk)[:, None], L_ <= (Pxl - tk)[:, None]) & cand2
    any2 = hit2.any(axis=1)
    jx2 = np.where(any2, hit2.argmax(axis=1), jb + Wn)
    after = jx2 >= PRE       # fill bar opens at or after t -> held through the settlement
    print(f'(ii) {d} a=-60,b=-1 maker, |pred|>=400%: events {len(idx)}; exit filled at/after t (or taker after t): {after.mean():.3f}; '
          f'of which unfilled->taker at t+14: {(~any2).mean():.3f}; mean funding owed/received on those (bp) {np.nanmean(np.abs(ev.rate.values[idx][after])) * 1e4:.1f}')
pd.DataFrame(rows).to_csv(os.path.join(HERE, 'v7_repro_event.csv'), index=False)
