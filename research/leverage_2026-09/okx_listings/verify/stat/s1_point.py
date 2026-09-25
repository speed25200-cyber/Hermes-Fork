"""Point estimates for every variant (union-type portfolio and OKX-extra alone), per period, plus paired daily
block bootstraps of Sharpe(variant) - Sharpe(binance_only) and an incremental-alpha regression of the OKX-alone
sleeve on the Binance-only sleeve. -> s1_point.json, daily returns -> s1_daily.parquet"""
import json
from statlib import *
from stat_variants import kw, VARIANTS
Db, Do = load()
U = merge(Db, Do)
okx = U.ev.src.values == 'okx'


def run(m, v, a, b, alone=False):
    k = kw(m, v)
    keep = k.pop('keep', None)
    if alone:
        keep = (m.ev.src.values == 'okx') if keep is None else keep & (m.ev.src.values == 'okx')
    if keep is not None:
        idx = np.where(keep)[0]
        mm = take(m, idx)
        k = {kk: vv[idx] for kk, vv in k.items()}
    else:
        mm = m
    return sim_live(mm, start=a, end=b, **k), mm


def block_boot(x, y, L, nb=5000, seed=0):
    """paired moving-block bootstrap of Sharpe(x)-Sharpe(y) (daily, annualised)"""
    rng = np.random.default_rng(seed)
    n = len(x)
    k = int(np.ceil(n / L))
    st = rng.integers(0, n - L + 1, (nb, k))
    ii = (st[:, :, None] + np.arange(L)[None, None, :]).reshape(nb, -1)[:, :n]
    X, Y = x[ii], y[ii]
    sx = X.mean(1) / X.std(1, ddof=1) * np.sqrt(365)
    sy = Y.mean(1) / Y.std(1, ddof=1) * np.sqrt(365)
    return sx - sy


def month_boot(x, y, idx, nb=5000, seed=1):
    rng = np.random.default_rng(seed)
    m = idx.to_period('M')
    groups = [np.where(m == p)[0] for p in m.unique()]
    out = np.empty(nb)
    for j in range(nb):
        g = rng.integers(0, len(groups), len(groups))
        ii = np.concatenate([groups[q] for q in g])
        out[j] = sharpe(x[ii]) - sharpe(y[ii])
    return out


def summ(d, d0):
    """percentile CI and bootstrap p-value (share of centred draws beyond the estimate, one-sided H1: d>0 or d<0)"""
    c = d - d.mean()
    p_two = float((np.abs(c) >= abs(d0)).mean())
    return dict(se=round(float(d.std()), 3), ci90=[round(float(np.percentile(d, 5)), 2), round(float(np.percentile(d, 95)), 2)],
                ci95=[round(float(np.percentile(d, 2.5)), 2), round(float(np.percentile(d, 97.5)), 2)],
                p_two_sided=round(p_two, 4), share_draws_below_0=round(float((d < 0).mean()), 3))


def hac_alpha(y, x, lags=10):
    """OLS y = a + b x, Newey-West SE; annualised alpha, t."""
    X = np.column_stack([np.ones_like(x), x])
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    e = y - X @ beta
    n = len(y)
    XtXi = np.linalg.inv(X.T @ X)
    S = (X * e[:, None]).T @ (X * e[:, None])
    for l in range(1, lags + 1):
        wl = 1 - l / (lags + 1)
        G = (X[l:] * e[l:, None]).T @ (X[:-l] * e[:-l, None])
        S += wl * (G + G.T)
    V = XtXi @ S @ XtXi
    return float(beta[0] * 365), float(beta[0] / np.sqrt(V[0, 0])), float(beta[1])


res = {}
daily = {}
for pn, a, b in PERIODS:
    base, _ = run(U, 'binance_only', a, b)
    rb = base['ret']
    daily[('binance_only', pn)] = rb
    for v in VARIANTS:
        r, mm = run(U, v, a, b)
        tr = pd.DataFrame(r['trades'])
        src = mm.ev.src.values[tr.i] if len(tr) else np.array([])
        ra, mma = run(U, v, a, b, alone=True) if v != 'binance_only' else (None, None)
        rec = dict(sharpe=round(sharpe(r['ret']), 3), maxdd=round(r['maxdd'], 3), final=round(r['final'], 3),
                   vol=round(float(r['ret'].std() * np.sqrt(365)), 3), trades=len(tr), okx_trades=int((src == 'okx').sum()),
                   blocked_bn=r['stats']['blocked_bn'])
        if v != 'binance_only':
            x, y = r['ret'].values, rb.values
            d0 = sharpe(x) - sharpe(y)
            rec['d_sharpe'] = round(d0, 3)
            for L in (10, 21, 63):
                rec[f'boot_block{L}'] = summ(block_boot(x, y, L), d0)
            rec['boot_month'] = summ(month_boot(x, y, r['ret'].index), d0)
            ta = pd.DataFrame(ra['trades'])
            al = ra['ret'].values
            alpha, t_alpha, beta = hac_alpha(al, y)
            rec['okx_alone'] = dict(sharpe=round(sharpe(al), 3), trades=len(ta), maxdd=round(ra['maxdd'], 3),
                                    mean_ret=round(float(ta.ret.mean()), 4) if len(ta) else None,
                                    mean_net=round(float(ta.net.mean()), 4) if len(ta) else None,
                                    corr_with_binance_only=round(float(np.corrcoef(al, y)[0, 1]), 3),
                                    alpha_ann_vs_bn=round(alpha, 4), alpha_t_nw10=round(t_alpha, 2), beta_vs_bn=round(beta, 3),
                                    needed_sr_for_gain=round(float(np.corrcoef(al, y)[0, 1] * sharpe(y)), 3))
            daily[(v, pn)] = r['ret']
            daily[(v + '|alone', pn)] = ra['ret']
            if pn == '2022-2026':
                ta.to_csv(f'{OUT}/s1_trades_alone_{v}.csv', index=False)
                tr.assign(src=src).to_csv(f'{OUT}/s1_trades_port_{v}.csv', index=False)
        res[f'{v} | {pn}'] = rec
        o = rec.get('okx_alone', {})
        print(f"{pn:12s} {v:14s} SR {rec['sharpe']:.2f} dSR {rec.get('d_sharpe', 0):+.2f} "
              f"blk21 CI90 {rec.get('boot_block21', {}).get('ci90')} p2 {rec.get('boot_block21', {}).get('p_two_sided')} "
              f"mon CI90 {rec.get('boot_month', {}).get('ci90')} maxDD {rec['maxdd']:.3f} tr {rec['trades']} okx {rec['okx_trades']} "
              f"| alone SR {o.get('sharpe')} n {o.get('trades')} alpha_t {o.get('alpha_t_nw10')} rho {o.get('corr_with_binance_only')}", flush=True)
json.dump(res, open(f'{OUT}/s1_point.json', 'w'), indent=1)
pd.DataFrame({f'{k[0]} | {k[1]}': v for k, v in daily.items()}).to_parquet(f'{OUT}/s1_daily.parquet')
