"""(a) LEG book model on the grid; (b) each sleeve alone in the same account/intrabar framework, for the 'more leverage'
comparison: book alone (w_b=1, w_l=0, book capital = k*E) and listing alone (w_l=1: sim leverage = short-leg cap).
-> leg_alone.json"""
import json
from combo import *
Dt = Data('hybrid2')
out = {}
for m in ('LEG',):
    for L in (1, 2, 3, 4, 5, 8):
        s = summ(combo_sim(Dt, L, m)); out[f'combo|{m}|L{L}'] = s
        print('combo', m, L, s['ib_maxdd'], s['liq'], s['maxdd_time'], s['parts'])
    s = summ(combo_sim(Dt, 4, m, stop_worst=True)); print('combo LEG L4 stopworst', s['ib_maxdd'])
    s = summ(combo_sim(Dt, 3, m, stop_worst=True)); print('combo LEG L3 stopworst', s['ib_maxdd'])
for m in ('close', 'LEG', 'C', 'B'):
    for k in (1, 2, 3, 4, 6, 8, 10):
        s = summ(combo_sim(Dt, k, m, wb=1.0, wl=0.0)); out[f'book_alone|{m}|k{k}'] = s
        print('book alone', m, 'k', k, 'mean gross', round(k * 0.51, 2), 'cagr', s['cagr'], 'ibDD', s['ib_maxdd'], s['liq'], s['maxdd_time'])
for Ls in (0.5, 1.0, 1.5, 2.0, 2.5):
    s = summ(combo_sim(Dt, Ls, 'close', wb=0.0, wl=1.0)); out[f'listing_alone|Ls{Ls}'] = s
    print('listing alone short-leg cap', Ls, 'total gross', 2 * Ls, 'cagr', s['cagr'], 'ibDD', s['ib_maxdd'], s['liq'], s['years'])
json.dump(out, open('leg_alone.json', 'w'), indent=1, default=str)
