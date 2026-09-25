"""(1) Book haircut scenarios and (2) weight sensitivity for the daily combination.
Haircut: book_adj_t = book_t - k * gross_t (drift removed in proportion to the book's exposure, so flat days stay flat),
k chosen so the book's OOS (2025-01..2026-08) Sharpe equals a target; the same k is applied in-sample.
Targets: as-is (1.80), full-period 1.36, 50% haircut 0.68, DSR excess (SR_obs - SR0 from the report's DSR 0.639 with
44 trials), 0, and a stress where the book earns its 2026 annualized return (-10.9%/yr) over the whole OOS.
-> s1_haircut.json"""
import json, os, numpy as np, pandas as pd
from scipy import stats, optimize
from s0_repro import load, sharpe, cagr, maxdd, years, lomo_min, HERE

j = load().dropna(subset=['book', 'nl'])
j = j['2023-08-01':'2026-08-31']
IS = slice('2023-08-01', '2024-12-31'); OOS = slice('2025-01-01', '2026-08-31')
out = {}
# --- DSR back-out: full book series (report uses all 1129 days incl. 2023-07-31)
b_all = pd.read_csv('/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv', index_col=0)['return'].values
n = len(b_all); sr = b_all.mean() / b_all.std(ddof=1); sk = stats.skew(b_all); ku = stats.kurtosis(b_all, fisher=False)
sd = np.sqrt((1 - sk * sr + (ku - 1) / 4 * sr ** 2) / (n - 1))
sr0 = sr - stats.norm.ppf(0.6385218288768264) * sd
out['dsr_backout'] = dict(n=n, sr_ann=sr * np.sqrt(365), sr0_ann=sr0 * np.sqrt(365), excess_ann=(sr - sr0) * np.sqrt(365),
                          se_ann=sd * np.sqrt(365), psr_check=float(stats.norm.cdf(sr / sd)))
print(out['dsr_backout'])

def book_adj(k):
    return j.book - k * j.book_gross

def k_for_oos_sharpe(target):
    f = lambda k: sharpe(book_adj(k)[OOS]) - target
    return optimize.brentq(f, -0.01, 0.01)

gb = j.book_gross[OOS]
ann2026 = (1 + j.book['2026']).prod() ** (365 / len(j.book['2026'])) - 1
# k for -10.9%/yr OOS drift: mean_adj*365 = ann2026 (arithmetic approx)
k_2026 = (j.book[OOS].mean() - ann2026 / 365) / gb.mean()
scen = {'as_is': 0.0, 'full_period_1.36': k_for_oos_sharpe(1.3633), 'haircut50_0.68': k_for_oos_sharpe(0.68),
        'dsr_excess': k_for_oos_sharpe(out['dsr_backout']['excess_ann']), 'zero': k_for_oos_sharpe(0.0),
        'stress_2026_rate': k_2026}
# alternative book: walk-forward selected setting
sel = pd.read_csv('/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/selection_daily.csv', index_col=0, parse_dates=True)['return']
sel.index = sel.index.tz_localize(None).normalize()

def evaluate(book, nl, w_book, tag):
    c = w_book * book + (1 - w_book) * nl
    o, i = c[OOS], c[IS]
    lm = lomo_min(o)
    res = dict(w_book=w_book, oos_sharpe=sharpe(o), is_sharpe=sharpe(i), book_oos_sharpe=sharpe(book[OOS]),
               book_is_sharpe=sharpe(book[IS]), y2025=years(o).get(2025), y2026=years(o).get(2026),
               lomo_min=lm[0], lomo_month=lm[1], oos_vol=float(o.std() * np.sqrt(365)),
               half_kelly_is=float(0.5 * i.mean() / i.var()), half_kelly_oos=float(0.5 * o.mean() / o.var()),
               corr_oos=float(np.corrcoef(book[OOS], nl[OOS])[0, 1]))
    for L in (2, 3, 4, 5):
        res[f'L{L}_cagr'] = cagr(L * o); res[f'L{L}_ddd'] = maxdd(L * o)
    # 2026 Mar-Aug (book effectively flat from March)
    res['c_2026_feb_aug'] = float((1 + c['2026-02-01':'2026-08-31']).prod() - 1)
    res['book_var_share_oos'] = float((w_book ** 2 * book[OOS].var() + w_book * (1 - w_book) * np.cov(book[OOS], nl[OOS])[0, 1]) / o.var())
    return res

rows = []
for nm, k in scen.items():
    ba = book_adj(k)
    for wb in (0.603, 0.3, 0.4, 0.5, 0.6, 0.7):
        r = evaluate(ba, j.nl, wb, nm); r['scenario'] = nm; r['k'] = k; rows.append(r)
ba = sel.reindex(j.index).fillna(0.0)
for wb in (0.603, 0.5):
    r = evaluate(ba, j.nl, wb, 'wf_selection'); r['scenario'] = 'book=walk-forward-selected setting'; r['k'] = None; rows.append(r)
df = pd.DataFrame(rows)
pd.set_option('display.width', 250)
cols = ['scenario', 'w_book', 'book_oos_sharpe', 'book_is_sharpe', 'oos_sharpe', 'is_sharpe', 'y2025', 'y2026', 'c_2026_feb_aug', 'lomo_min', 'lomo_month',
        'half_kelly_is', 'half_kelly_oos', 'book_var_share_oos', 'L3_cagr', 'L3_ddd', 'L4_cagr', 'L4_ddd', 'L5_cagr', 'L5_ddd']
print(df[cols].round(3).to_string(index=False))
out['scenarios_k'] = scen; out['ann2026_book'] = ann2026
out['rows'] = df.to_dict(orient='records')
# weight fine grid for as-is and dsr_excess
fine = []
for nm in ('as_is', 'haircut50_0.68', 'dsr_excess', 'zero'):
    ba = book_adj(scen[nm])
    for wb in np.round(np.arange(0.0, 1.01, 0.05), 2):
        c = wb * ba + (1 - wb) * j.nl
        fine.append(dict(scenario=nm, w_book=wb, oos_sharpe=sharpe(c[OOS]), y2026=years(c[OOS]).get(2026), lomo=lomo_min(c[OOS])[0]))
fd = pd.DataFrame(fine)
print(fd.pivot(index='w_book', columns='scenario', values='oos_sharpe').round(2).to_string())
out['fine'] = fine
json.dump(out, open(os.path.join(HERE, 's1_haircut.json'), 'w'), indent=1, default=float)
