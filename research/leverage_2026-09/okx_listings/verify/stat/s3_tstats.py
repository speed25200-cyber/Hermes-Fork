"""OKX-extra alone: significance of the portfolio Sharpe and of the trade mean with clustering by listing and by month.
Uses the author's frozen-rule run (reproduced with the reviewer sim to 2026-09-25) and the author's okx_trades.csv.
'ret' = gross coin short return (author's trade measure); 'net' = PnL/notional incl. BTC hedge, fees, funding.
-> s3_tstats.json"""
import json
from statlib import *
from stat_variants import kw
END = '2026-09-25'
Db, Do = load()
U = merge(Db, Do)
okx_idx = np.where(U.ev.src.values == 'okx')[0]
O = take(U, okx_idx)
auth = pd.read_csv(SP + '/xlist/results/okx_trades.csv')
auth_json = json.load(open(SP + '/xlist/results/okx_events.json'))


def cr_se(e, g):
    """CR1 cluster-robust s.e. of a mean; e: residuals, g: cluster labels"""
    n = len(e)
    s = pd.Series(e).groupby(np.asarray(g)).sum().values
    G = len(s)
    return np.sqrt((s ** 2).sum() * G / (G - 1)) / n, G


def two_way_se(e, g1, g2):
    n = len(e)
    v = 0.0
    for g in (g1, g2, [f'{a}|{b}' for a, b in zip(g1, g2)]):
        s = pd.Series(e).groupby(np.asarray(g)).sum().values
        G = len(s)
        v += (1 if g is not None else 0) * 0  # placeholder to keep structure explicit
    s1 = pd.Series(e).groupby(np.asarray(g1)).sum().values
    s2 = pd.Series(e).groupby(np.asarray(g2)).sum().values
    s12 = pd.Series(e).groupby(np.asarray([f'{a}|{b}' for a, b in zip(g1, g2)])).sum().values
    G = min(len(s1), len(s2))
    v = ((s1 ** 2).sum() + (s2 ** 2).sum() - (s12 ** 2).sum()) * G / (G - 1)
    return np.sqrt(max(v, 0)) / n


def tstats(x, lst, mon):
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return None
    e = x - x.mean()
    se_iid = x.std(ddof=1) / np.sqrt(n)
    se_l, Gl = cr_se(e, lst)
    se_m, Gm = cr_se(e, mon)
    se_2 = two_way_se(e, lst, mon)
    ev = pd.Series(x).groupby(np.asarray(lst)).mean()
    mo = pd.Series(x).groupby(np.asarray(mon)).mean()
    return dict(n=n, mean=round(float(x.mean()), 4), t_iid=round(float(x.mean() / se_iid), 2),
                t_cl_listing=round(float(x.mean() / se_l), 2), n_listings=int(Gl),
                t_cl_month=round(float(x.mean() / se_m), 2), n_months=int(Gm),
                t_cl_twoway=round(float(x.mean() / se_2), 2),
                t_listing_means=round(float(ev.mean() / ev.std(ddof=1) * np.sqrt(len(ev))), 2),
                t_month_means=round(float(mo.mean() / mo.std(ddof=1) * np.sqrt(len(mo))), 2) if len(mo) > 2 else None,
                median=round(float(np.median(x)), 4), win=round(float((x > 0).mean()), 3))


def sr_stats(ret, nb=5000, seed=0):
    r = ret.values
    T = len(r) / 365
    s = sharpe(r)
    out = dict(sharpe=round(s, 3), years=round(T, 2), t_sr=round(s * np.sqrt(T), 2))
    # Lo (2002) with autocorrelation (10 lags) of daily returns
    x = r - r.mean()
    rho = [np.corrcoef(x[:-k], x[k:])[0, 1] for k in range(1, 11)]
    eta = 1 + 2 * sum((1 - k / 11) * rho[k - 1] for k in range(1, 11))
    out['t_sr_lo10'] = round(s * np.sqrt(T) / np.sqrt(max(eta, 1e-9)), 2)
    rng = np.random.default_rng(seed)
    for L in (10, 21):
        n = len(r)
        k = int(np.ceil(n / L))
        st = rng.integers(0, n - L + 1, (nb, k))
        ii = (st[:, :, None] + np.arange(L)[None, None, :]).reshape(nb, -1)[:, :n]
        X = r[ii]
        b = X.mean(1) / X.std(1, ddof=1) * np.sqrt(365)
        out[f'block{L}_ci90'] = [round(float(np.percentile(b, 5)), 2), round(float(np.percentile(b, 95)), 2)]
        out[f'block{L}_p_le0'] = round(float((b <= 0).mean()), 4)
    m = ret.index.to_period('M')
    mr = ret.groupby(m).apply(lambda v: np.prod(1 + v) - 1)
    out['monthly'] = dict(n=len(mr), mean=round(float(mr.mean()), 4), t=round(float(mr.mean() / mr.std(ddof=1) * np.sqrt(len(mr))), 2),
                          pos_months=int((mr > 0).sum()))
    return out


res = {}
periods = [('2022-2026', '2022-01-01', END), ('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', END),
           ('2026', '2026-01-01', END)]
for pn, a, b in periods:
    r = sim_live(O, start=a, end=b)
    tr = pd.DataFrame(r['trades'])
    tr['month'] = gtime(tr.entry_t).to_period('M').astype(str)
    tr['cls'] = O.ev.cls.values[tr.i]
    tr['bn_g'] = O.bn_g[tr.i]
    tr['bn_during_hold'] = (tr.bn_g > tr.entry_t) & (tr.bn_g <= tr.exit_t)
    tr['bn_before_entry'] = tr.bn_g <= tr.entry_t
    rec = dict(portfolio=sr_stats(r['ret']), author_sharpe=auth_json[{'2022-2026': 'all 2022-2026'}.get(pn, pn)]['sharpe'],
               n_trades=len(tr), author_n=auth_json[{'2022-2026': 'all 2022-2026'}.get(pn, pn)]['n_trades'])
    for meas in ('ret', 'net'):
        rec[meas] = tstats(tr[meas], tr.sym, tr.month)
        for c, g in tr.groupby('cls'):
            rec[f'{meas}_{c}'] = tstats(g[meas], g.sym, g.month)
        # okx_only minus okx_first, cluster by listing (difference via regression on a dummy)
        d = tr.cls.eq('okx_only').astype(float).values
        y = tr[meas].values
        X = np.column_stack([np.ones_like(d), d])
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        e = y - X @ beta
        XtXi = np.linalg.inv(X.T @ X)
        S = np.zeros((2, 2))
        for s_, gi in tr.groupby('sym').groups.items():
            u = (X[tr.index.get_indexer(gi)] * e[tr.index.get_indexer(gi), None]).sum(0)
            S += np.outer(u, u)
        G = tr.sym.nunique()
        V = XtXi @ S @ XtXi * G / (G - 1)
        rec[f'{meas}_only_minus_first'] = dict(diff=round(float(beta[1]), 4), t_cl_listing=round(float(beta[1] / np.sqrt(V[1, 1])), 2))
        sub = tr[tr.bn_during_hold]
        rec[f'{meas}_bn_listed_during_hold'] = tstats(sub[meas], sub.sym, sub.month)
        sub = tr[tr.bn_before_entry]
        rec[f'{meas}_bn_listed_before_entry'] = tstats(sub[meas], sub.sym, sub.month)
    if pn == '2022-2026':
        # consistency with the author's trade file
        m_ = tr.merge(auth, on=['sym', 'tranche', 'entry_t'], suffixes=('', '_a'))
        rec['match_author_trades'] = dict(matched=len(m_), max_abs_ret_diff=float((m_.ret - m_.ret_a).abs().max()))
        tr.to_csv(f'{OUT}/s3_trades_alone.csv', index=False)
        # yearly breakdown
        tr['year'] = gtime(tr.entry_t).year
        rec['by_year'] = {int(y): tstats(g.net, g.sym, g.month) for y, g in tr.groupby('year')}
    res[pn] = rec
    p = rec['portfolio']
    print(pn, 'SR', p['sharpe'], 'auth', rec['author_sharpe'], 'n', len(tr), rec['author_n'], 't_sr', p['t_sr'], 'lo', p['t_sr_lo10'],
          'blk21', p['block21_ci90'], 'p<=0', p['block21_p_le0'], 'monthly', p['monthly'], flush=True)
    for meas in ('ret', 'net'):
        print('   ', meas, rec[meas])
        print('   ', meas, 'only-first', rec[f'{meas}_only_minus_first'], 'first', rec.get(f'{meas}_okx_first'), 'only', rec.get(f'{meas}_okx_only'))
        print('   ', meas, 'bn during hold', rec[f'{meas}_bn_listed_during_hold'], 'bn before entry', rec[f'{meas}_bn_listed_before_entry'])
json.dump(res, open(f'{OUT}/s3_tstats.json', 'w'), indent=1, default=str)
