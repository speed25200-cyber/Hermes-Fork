"""Correlation of the selected new-listing short (hybrid prices, 1x) with the current book's OOS daily returns, and the
Sharpe of a 50/50 risk-parity blend over their common OOS days (2025-01..2026-08). -> results/book_corr.json"""
from sim import *
import json
b = pd.read_csv('/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv', index_col=0, parse_dates=True)
b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
out = {}
for price in ('hybrid', 'binance'):
    s = pd.read_csv(os.path.join(BASE, 'results', f'oos_daily_selected_{price}.csv'), index_col=0, parse_dates=True)['ret']
    j = pd.concat([s.rename('nl'), b['return'].rename('book')], axis=1).dropna()
    j = j['2025-01-01':'2026-08-31']
    w = (1 / j.std()) / (1 / j.std()).sum()
    blend = (j * w).sum(axis=1)
    out[price] = dict(days=len(j), corr=float(j.corr().iloc[0, 1]), sharpe_nl=sharpe(j.nl), sharpe_book=sharpe(j.book),
                      sharpe_blend=sharpe(blend), w_nl=float(w.nl))
print(json.dumps(out, indent=1))
json.dump(out, open(os.path.join(BASE, 'results', 'book_corr.json'), 'w'), indent=1)
