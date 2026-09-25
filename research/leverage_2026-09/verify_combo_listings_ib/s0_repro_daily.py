"""Reproduce the main loop's quick daily combination (inverse IS-vol weights over 2023-08..2024-12; returns
L*(w_b*r_book + w_l*r_list)) from the saved daily series, and same-day loss statistics. -> repro_daily.json"""
import json, numpy as np, pandas as pd
NL = '../newlisting/results/'
B = pd.read_csv('/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv', index_col=0, parse_dates=True)
B.index = B.index.tz_localize(None)
li = pd.concat([pd.read_csv(NL + 'is_daily_selected_hybrid.csv', index_col=0, parse_dates=True).ret,
                pd.read_csv(NL + 'oos_daily_selected_hybrid.csv', index_col=0, parse_dates=True).ret])
li.index = pd.DatetimeIndex(li.index).tz_localize(None)
df = pd.DataFrame({'b': B['return'], 'l': li}).dropna()
IS, OOS = slice('2023-08-01', '2024-12-31'), slice('2025-01-01', '2026-08-31')
v = df.loc[IS].std()
w = (1 / v) / (1 / v).sum()
def sh(r): return float(r.mean() / r.std() * np.sqrt(365))
def mdd(r):
    e = (1 + r).cumprod(); return float((1 - e / e.cummax()).max())
out = dict(weights=w.round(4).to_dict())
c = df.b * w.b + df.l * w.l
out['is_sharpe'] = sh(c.loc[IS]); out['oos_sharpe'] = sh(c.loc[OOS])
out['oos_corr'] = float(df.loc[OOS].corr().iloc[0, 1])
for L in (1, 2, 3, 4, 5, 8):
    r = L * c.loc[OOS]
    e = (1 + r).cumprod()
    out[f'L{L}'] = dict(cagr=float(e.iloc[-1] ** (365 / len(r)) - 1), daily_maxdd=mdd(r),
                        y2025=float((1 + r.loc['2025']).prod() - 1), y2026=float((1 + r.loc['2026']).prod() - 1))
x = df.loc[OOS]
act = (B.gross.reindex(x.index) > 0.01) & (x.l != 0)
xa = x[act]
bl, ll = xa.b < 0, xa.l < 0
out['same_day'] = dict(days_both_active=int(act.sum()), p_book_loss=float(bl.mean()), p_list_loss=float(ll.mean()),
                       p_both_loss=float((bl & ll).mean()), p_both_if_indep=float(bl.mean() * ll.mean()),
                       p_book_loss_given_list_loss=float((bl & ll).sum() / ll.sum()))
q = xa.quantile(0.1)
bt, lt = xa.b <= q.b, xa.l <= q.l
out['same_day']['worst_decile_both'] = int((bt & lt).sum()); out['same_day']['worst_decile_both_if_indep'] = float(len(xa) * 0.01)
out['same_day']['book_on_10_worst_list_days'] = {str(k.date()): [round(float(xa.l[k]), 4), round(float(xa.b[k]), 4)] for k in xa.l.nsmallest(10).index}
out['same_day']['all_days_p_both_loss'] = float(((x.b < 0) & (x.l < 0)).mean())
# IS half-Kelly of the daily combination at L=1
ci = c.loc[IS]; out['is_kelly_daily'] = float(ci.mean() / ci.var()); out['is_half_kelly_daily'] = out['is_kelly_daily'] / 2
json.dump(out, open('repro_daily.json', 'w'), indent=1)
print(json.dumps(out, indent=1))
