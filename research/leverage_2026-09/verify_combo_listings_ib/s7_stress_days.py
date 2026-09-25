"""Stress-day decomposition, break-even 10-10 book shock, IS half-Kelly (hourly combined sim), bar items at L=1.
-> stress_days.json"""
import json
from combo import *
Dt = Data('hybrid2')
out = {}
# (1) worst hour of each stress day: combined intrabar loss vs the day's opening equity, listing vs book parts
SD = ['2025-10-10', '2025-02-03', '2025-12-31', '2025-04-07', '2025-06-03']
tab = {}
for m in ('close', 'C', 'B', 'A'):
    for L in (1, 2, 3, 4, 5, 8):
        r = combo_sim(Dt, L, m, record=True)
        for d in SD:
            day = pd.Timestamp(d)
            e0 = r['eq'].get(day - pd.Timedelta(hours=1), 1.0)
            w = r['eq_worst'].loc[d]
            if w.isna().all():
                continue
            j = w.idxmin()
            tab[f'{d}|{m}|L{L}'] = dict(hour=j.hour, loss_vs_day_open=round(float(w.min() / e0 - 1), 4),
                                        listing_part=round(float(r['comp_l'][j] / e0), 4), book_part=round(float(r['comp_b'][j] / e0), 4),
                                        dd_from_peak=round(float(1 - w.min() / r['eq'].loc[:j].max()), 4))
out['stress_days'] = tab
for k, v in tab.items():
    if k.split('|')[0] in ('2025-10-10', '2025-02-03', '2025-12-31'):
        print(k, v)
# listing positions open on 10-10 and 02-03
r = combo_sim(Dt, 3, 'close', record=True)
for d in ('2025-10-10', '2025-02-03'):
    t0 = pd.Timestamp(d); t1 = t0 + pd.Timedelta(days=1)
    op = [(tr['sym'], str(pd.Timestamp(G0 + tr['entry_t'] * HMS, unit='ms')), round(tr['notional'] / tr['E'], 3)) for tr in r['trades']
          if pd.Timestamp(G0 + tr['entry_t'] * HMS, unit='ms') < t1 and pd.Timestamp(G0 + tr['exit_t'] * HMS, unit='ms') > t0]
    out[f'listing_open_{d}_L3'] = op
    print(d, 'listing positions open (sym, entry, coin notional / E) at L=3:', op)
# (2) break-even extra book trough on 2025-10-10 (fraction of BOOK capital) on top of the 'close' model
be = {}
for L in (2, 3, 4):
    hit = None
    for x in np.arange(0.0, 0.601, 0.005):
        s = combo_sim(Dt, L, 'close', extra_book_shock={'2025-10-10': x})
        if s['maxdd'] > 0.35 or s['liq']:
            hit = round(float(x), 3); break
    be[L] = hit
out['break_even_book_shock_1010_frac_book_capital'] = be
print('break-even 10-10 book trough (fraction of book capital) for DD>35%:', be)
# (3) IS half-Kelly on the hourly combined sim (2023-08-01 .. 2024-12-31, close model)
ri = combo_sim(Dt, 1.0, 'close', start='2023-08-01', end='2025-01-01')['ret']
out['is_sharpe_hourly_sim'] = sharpe(ri)
out['is_kelly'] = float(ri.mean() / ri.var()); out['is_half_kelly'] = out['is_kelly'] / 2
print('IS Sharpe', round(out['is_sharpe_hourly_sim'], 3), 'IS half-Kelly', round(out['is_half_kelly'], 2))
# (4) bar items at L=1 (close model): LOMO (drop days), LOCO (listing coins), cost x1.5 + 1 bar latency (book costs x1.5 too)
base = combo_sim(Dt, 1.0, 'close')
r0 = base['ret']; m = r0.index.to_period('M')
lomo = {str(p): sharpe(r0[m != p]) for p in m.unique()}
out['lomo_min'] = min(lomo.values()); out['lomo_min_month'] = min(lomo, key=lomo.get)
syms = sorted({t['sym'] for t in base['trades']})
loco = {s: sharpe(combo_sim(Dt, 1.0, 'close', exclude={s})['ret']) for s in syms}
out['loco_min'] = min(loco.values()); out['loco_min_coin'] = min(loco, key=loco.get)
import combo as C_
B0 = C_.BOOK['return'].copy()
Bfull = pd.read_csv(BOOKCSV, index_col=0, parse_dates=True); Bfull.index = Bfull.index.tz_localize(None)
C_.BOOK['return'] = B0 - 0.5 * (Bfull.fees + Bfull.spread + Bfull.impact).reindex(B0.index).fillna(0)
out['cost15_lat1'] = sharpe(combo_sim(Dt, 1.0, 'close', cost_mult=1.5, lat=1)['ret'])
C_.BOOK['return'] = B0
print({k: out[k] for k in ('lomo_min', 'lomo_min_month', 'loco_min', 'loco_min_coin', 'cost15_lat1')})
json.dump(out, open('stress_days.json', 'w'), indent=1, default=str)
