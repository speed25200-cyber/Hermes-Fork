"""(4) Regime: what if new-token shorts stop working. Re-runs the researcher's selected listing config (hybrid, coin cap 1)
with record=True to get its daily gross, then removes listing drift in proportion to gross (k_l) to hit target OOS
Sharpes, crossed with book haircut scenarios (s1). Also: rolling 6-month combined Sharpe, sub-period stats.
-> s3_regime.json, nl_gross_daily.csv"""
import json, os, sys, numpy as np, pandas as pd
from scipy import optimize
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'newlisting'))
import sim
from s0_repro import load, sharpe, cagr, maxdd, years, lomo_min

cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
Dt = sim.Data('hybrid')
g = []
for a, b in (('2023-08-01', '2025-01-01'), ('2025-01-01', '2026-09-01')):
    r = sim.simulate(Dt, cfg, a, b, L=1.0, record=True)
    g.append(pd.DataFrame({'ret_rerun': r['ret'], 'nl_gross': r['gross'].resample('D').mean()}))
G = pd.concat(g)
G.to_csv(os.path.join(HERE, 'nl_gross_daily.csv'))
j = load().dropna(subset=['book', 'nl'])['2023-08-01':'2026-08-31'].join(G)
# note: IS rerun starts 2023-08-01 (not 2022) so compounding base differs; daily returns must match where positions are identical
print('max |rerun - file| OOS:', float((j.ret_rerun - j.nl)['2025':].abs().max()), ' IS:', float((j.ret_rerun - j.nl)[:'2024'].abs().max()))
IS = slice('2023-08-01', '2024-12-31'); OOS = slice('2025-01-01', '2026-08-31')
S1 = json.load(open(os.path.join(HERE, 's1_haircut.json')))
KB = S1['scenarios_k']
W = 0.6030102397652416
out = dict(nl_gross_oos_mean=float(j.nl_gross[OOS].mean()), nl_gross_oos_max=float(j.nl_gross[OOS].max()),
           nl_flat_days_oos=float((j.nl_gross[OOS] < 1e-6).mean()), book_flat_days_2026=float((j.book_gross['2026'] < 0.02).mean()))

def nl_adj(k):
    return j.nl - k * j.nl_gross

def k_nl(target):
    return optimize.brentq(lambda k: sharpe(nl_adj(k)[OOS]) - target, -0.05, 0.05)

NLS = {'as_is_1.40': 0.0, 'grid_short_median_1.11': k_nl(1.111), 'half_0.70': k_nl(0.70), 'zero': k_nl(0.0), 'flip_-1.0': k_nl(-1.0)}
# 2025-only boom removed: listing earns its 2026 Jan-Aug per-gross rate over the whole OOS? (use grid-median 2026 +4.6% for 8m)
rows = []
for bn in ('as_is', 'haircut50_0.68', 'dsr_excess', 'zero'):
    book = j.book - KB[bn] * j.book_gross
    for ln, kl in NLS.items():
        nl = nl_adj(kl)
        c = (W * book + (1 - W) * nl)[OOS]
        lm = lomo_min(c)
        row = dict(book=bn, listing=ln, book_sh=sharpe(book[OOS]), nl_sh=sharpe(nl[OOS]), comb_sh=sharpe(c), y2025=years(c)[2025],
                   y2026=years(c)[2026], lomo=lm[0], ddd_L1=maxdd(c))
        # largest integer L with daily-close DD <= 35%
        row['L_dd35_daily'] = max([L for L in range(1, 21) if maxdd(L * c) <= 0.35] or [0])
        row['L3_cagr'] = cagr(3 * c)
        rows.append(row)
df = pd.DataFrame(rows)
pd.set_option('display.width', 250)
print(df.round(3).to_string(index=False))
out['grid'] = rows
out['k_nl'] = NLS
# sub-periods of the as-is combination
c = W * j.book + (1 - W) * j.nl
sub = {}
for nm, (a, b) in {'2025H1': ('2025-01-01', '2025-06-30'), '2025H2': ('2025-07-01', '2025-12-31'), '2026H1': ('2026-01-01', '2026-06-30'),
                   '2026Feb-Aug': ('2026-02-01', '2026-08-31'), '2026Mar-Aug(book~flat)': ('2026-03-01', '2026-08-31')}.items():
    s = c[a:b]
    sub[nm] = dict(sharpe=sharpe(s), ret=float((1 + s).prod() - 1), book_ret=float((1 + j.book[a:b]).prod() - 1), nl_ret=float((1 + j.nl[a:b]).prod() - 1),
                   book_gross=float(j.book_gross[a:b].mean()), nl_gross=float(j.nl_gross[a:b].mean()))
out['subperiods'] = sub
print(json.dumps(sub, indent=1))
roll = c[OOS].rolling(182).apply(lambda x: x.mean() / x.std() * np.sqrt(365))
out['rolling182_sharpe'] = {str(d.date()): float(v) for d, v in roll.dropna().iloc[::30].items()}
out['rolling182_last'] = float(roll.iloc[-1]); out['rolling182_min'] = float(roll.min())
print('rolling 182d Sharpe (every 30d):', {k: round(v, 2) for k, v in out['rolling182_sharpe'].items()})
json.dump(out, open(os.path.join(HERE, 's3_regime.json'), 'w'), indent=1, default=float)
