"""(6) Joining the two daily series: day-boundary checks (both are UTC calendar days closing at 24:00 UTC: Hermes groups
30m bars by open-time floor('D'), the bar 23:30 closing at 24:00; sim.py resamples hourly equity indexed by bar open time,
last = close of 23:00 bar = 24:00), lead-lag correlations, +-1 day shift sensitivity, conditional/tail co-movement.
-> s5_align.json"""
import json, os, numpy as np, pandas as pd
from s0_repro import load, sharpe, HERE
j = load().dropna(subset=['book', 'nl'])['2023-08-01':'2026-08-31']
G = pd.read_csv(os.path.join(HERE, 'nl_gross_daily.csv'), index_col=0, parse_dates=True)
j = j.join(G.nl_gross)
OOS = slice('2025-01-01', '2026-08-31'); W = 0.6030102397652416
o = j[OOS]
out = {}
out['lag_corr_oos'] = {f'nl_lead{k}': float(o.book.corr(o.nl.shift(-k))) for k in (-2, -1, 0, 1, 2)}
out['shift_sharpe_oos'] = {f'nl_shift{k}': sharpe((W * j.book + (1 - W) * j.nl.shift(k)).dropna()[OOS]) for k in (-1, 0, 1)}
both = o[(o.book_gross > 0.2) & (o.nl_gross > 0.01)]
out['corr_both_active'] = dict(days=len(both), corr=float(both.book.corr(both.nl)))
out['corr_spearman_oos'] = float(o.book.corr(o.nl, method='spearman'))
q = o.nl.quantile(0.05); qb = o.book.quantile(0.05)
out['tail'] = dict(book_mean_on_nl_worst5pct=float(o.book[o.nl <= q].mean()), book_mean_all=float(o.book.mean()),
                   nl_mean_on_book_worst5pct=float(o.nl[o.book <= qb].mean()), nl_mean_all=float(o.nl.mean()),
                   both_negative_on_nl_worst5pct=float((o.book[o.nl <= q] < 0).mean()))
worst = (W * o.book + (1 - W) * o.nl).nsmallest(8)
out['worst_combined_days'] = {str(d.date()): dict(comb=float(v), book=float(o.book[d]), nl=float(o.nl[d])) for d, v in worst.items()}
# monthly correlation (less sensitive to daily noise / day-boundary)
m = o[['book', 'nl']].resample('ME').apply(lambda x: (1 + x).prod() - 1)
out['monthly_corr_oos'] = float(m.book.corr(m.nl)); out['months'] = len(m)
w = o[['book', 'nl']].resample('W').apply(lambda x: (1 + x).prod() - 1)
out['weekly_corr_oos'] = float(w.book.corr(w.nl))
print(json.dumps(out, indent=1))
json.dump(out, open(os.path.join(HERE, 's5_align.json'), 'w'), indent=1)
