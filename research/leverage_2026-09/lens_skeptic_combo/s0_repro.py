"""Reproduce the main loop's daily combination: book (Hermes equity_daily 'return') + new-listing short (hybrid, coin cap 1).
Weights = inverse IS vol over 2023-08..2024-12 overlap. -> s0_repro.json, joined.csv"""
import json, os, numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
NL = os.path.join(HERE, '..', 'newlisting', 'results')
BOOK = '/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv'

def sharpe(r):
    r = np.asarray(r, float); s = r.std(ddof=1)
    return float(r.mean() / s * np.sqrt(365)) if s > 0 else 0.0

def cagr(r):
    g = np.prod(1 + np.asarray(r)); return float(g ** (365 / len(r)) - 1) if g > 0 else -1.0

def maxdd(r):
    e = np.cumprod(1 + np.asarray(r)); return float(1 - (e / np.maximum.accumulate(np.r_[1.0, e][1:].clip(min=0) if False else np.maximum.accumulate(e))).min())

def years(r):
    return {int(k): float(np.prod(1 + v) - 1) for k, v in r.groupby(r.index.year)}

def lomo_min(r):
    m = r.index.to_period('M'); v = {str(p): sharpe(r[m != p]) for p in m.unique()}
    k = min(v, key=v.get); return v[k], k

def load():
    b = pd.read_csv(BOOK, index_col=0, parse_dates=True)
    b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
    li = pd.concat([pd.read_csv(os.path.join(NL, f'{s}_daily_selected_hybrid.csv'), index_col=0, parse_dates=True)['ret']
                    for s in ('is', 'oos')])
    li = li[~li.index.duplicated()]
    j = pd.concat([b['return'].rename('book'), li.rename('nl'), b['gross'].rename('book_gross')], axis=1)
    return j

if __name__ == '__main__':
    j = load()
    out = {}
    # checks on calendars
    out['book_range'] = [str(j.book.dropna().index.min().date()), str(j.book.dropna().index.max().date())]
    out['nl_range'] = [str(j.nl.dropna().index.min().date()), str(j.nl.dropna().index.max().date())]
    jj = j.dropna(subset=['book', 'nl'])
    full = pd.date_range(jj.index.min(), jj.index.max(), freq='D')
    out['overlap_days'] = len(jj); out['missing_calendar_days'] = int(len(full) - len(jj))
    IS = jj['2023-08-01':'2024-12-31']; OOS = jj['2025-01-01':'2026-08-31']
    vol = IS[['book', 'nl']].std()
    w = (1 / vol) / (1 / vol).sum()
    out['w'] = w.to_dict()
    for nm, seg in (('IS', IS), ('OOS', OOS)):
        c = w.book * seg.book + w.nl * seg.nl
        lm = lomo_min(c)
        out[nm] = dict(days=len(seg), sharpe=sharpe(c), sharpe_book=sharpe(seg.book), sharpe_nl=sharpe(seg.nl),
                       corr=float(seg[['book', 'nl']].corr().iloc[0, 1]), cagr=cagr(c), maxdd=maxdd(c), years=years(c),
                       years_book=years(seg.book), years_nl=years(seg.nl), lomo_min=lm[0], lomo_month=lm[1],
                       vol_book=float(seg.book.std() * np.sqrt(365)), vol_nl=float(seg.nl.std() * np.sqrt(365)))
        for L in (1, 2, 3, 4, 5):
            cl = L * c
            out[nm][f'L{L}'] = dict(cagr=cagr(cl), maxdd_daily=maxdd(cl))
    print(json.dumps(out, indent=1))
    json.dump(out, open(os.path.join(HERE, 's0_repro.json'), 'w'), indent=1)
    jj.to_csv(os.path.join(HERE, 'joined.csv'))
