"""(a) OKX-extra vs Binance-event trades (net PnL/notional) per period, standalone sleeves, listing-clustered two-sample t;
(b) period heterogeneity of the OKX-extra mean (2022-24 vs 2025-26);
(c) delete-one-cluster jackknife of Sharpe(union variant) - Sharpe(binance_only): SE and most influential OKX tokens;
(d) shadow-period power: events needed to reach listing-clustered t = 2 at the observed event-level mean/sd.
-> s4_extra.json"""
import json
from statlib import *
from stat_variants import kw, VARIANTS
Db, Do = load()
U = merge(Db, Do)
src = U.ev.src.values
base = U.ev.base.values
res = {}


def trades(m, a, b, **k):
    r = sim_live(m, start=a, end=b, **k)
    t = pd.DataFrame(r['trades'])
    t['cl'] = m.ev.base.values[t.i]
    return t, r


def cl_mean_se(x, g):
    x = np.asarray(x, float)
    e = x - x.mean()
    s = pd.Series(e).groupby(np.asarray(g)).sum().values
    G = len(s)
    return x.mean(), np.sqrt((s ** 2).sum() * G / (G - 1)) / len(x), G


# (a), (b)
Bn = take(U, np.where(src == 'binance')[0])
Ok = take(U, np.where(src == 'okx')[0])
for pn, a, b in [('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-01'), ('2022-2026', '2022-01-01', '2026-09-01')]:
    tb, rb = trades(Bn, a, b)
    to, ro = trades(Ok, a, b)
    mb, sb, gb = cl_mean_se(tb.net, tb.cl)
    mo, so, go = cl_mean_se(to.net, to.cl)
    res[f'a {pn}'] = dict(binance=dict(n=len(tb), listings=gb, mean_net=round(mb, 4), se=round(sb, 4), sr=round(sharpe(rb['ret']), 3)),
                          okx=dict(n=len(to), listings=go, mean_net=round(mo, 4), se=round(so, 4), sr=round(sharpe(ro['ret']), 3)),
                          diff=round(mo - mb, 4), t_diff=round((mo - mb) / np.sqrt(sb ** 2 + so ** 2), 2))
    print('a', pn, res[f'a {pn}'], flush=True)
t1, _ = trades(Ok, '2022-01-01', '2025-01-01')
t2, _ = trades(Ok, '2025-01-01', '2026-09-25')
m1, s1, _ = cl_mean_se(t1.net, t1.cl)
m2, s2, _ = cl_mean_se(t2.net, t2.cl)
res['b okx 2025-26 minus 2022-24'] = dict(diff=round(m2 - m1, 4), t=round((m2 - m1) / np.sqrt(s1 ** 2 + s2 ** 2), 2))
print('b', res['b okx 2025-26 minus 2022-24'])

# (d) power: event-level (listing) means of net, 2022-2026-09-25 and 2025-26
for pn, a, b in [('2022-2026', '2022-01-01', '2026-09-25'), ('2025-2026', '2025-01-01', '2026-09-25')]:
    t, _ = trades(Ok, a, b)
    ev = t.groupby('cl').net.mean()
    mu, sd = ev.mean(), ev.std(ddof=1)
    n2 = (2 * sd / mu) ** 2 if mu > 0 else np.inf
    months = (pd.Timestamp(b) - pd.Timestamp(a)).days / 30.4
    rate = len(ev) / months
    res[f'd power {pn}'] = dict(listings=len(ev), mean=round(mu, 4), sd=round(sd, 4), listings_needed_t2=round(float(n2), 1),
                                listings_per_month=round(rate, 2),
                                months_needed_at_rate=round(float(n2 / rate), 1) if np.isfinite(n2) else None)
    print('d', pn, res[f'd power {pn}'])

# (c) jackknife over OKX clusters (drop one OKX token), full-run slices
SL = {pn: (a, (pd.Timestamp(b) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')) for pn, a, b in PERIODS}


def srs(m, v):
    k = kw(m, v)
    keep = k.pop('keep', None)
    if keep is not None:
        idx = np.where(keep)[0]
        m, k = take(m, idx), {kk: vv[idx] for kk, vv in k.items()}
    r = sim_live(m, start='2022-01-01', end='2026-09-01', **k)['ret']
    return {pn: sharpe(r[a:b]) for pn, (a, b) in SL.items()}


okx_nt = np.where((src == 'okx') & U.newtok)[0]
cls_ = sorted(set(base[okx_nt]))
bo = srs(U, 'binance_only')
for v in ('union', 'okx_half', 'okx_nobn', 'okx_exit_bn24'):
    full = srs(U, v)
    d0 = {p: full[p] - bo[p] for p in SL}
    J = []
    for c in cls_:
        keep = ~((src == 'okx') & (base == c))
        m = take(U, np.where(keep)[0])
        s = srs(m, v)
        J.append({p: s[p] - bo[p] for p in SL})
    rec = {}
    for p in SL:
        d = np.array([j[p] for j in J])
        n = len(d)
        se = np.sqrt((n - 1) / n * ((d - d.mean()) ** 2).sum())
        infl = pd.Series(d - d0[p], index=cls_).sort_values()
        rec[p] = dict(d=round(d0[p], 3), jk_se=round(float(se), 3), z=round(d0[p] / se, 2) if se > 0 else None,
                      most_negative_tokens=[(k_, round(v_, 3)) for k_, v_ in infl.iloc[::-1].head(3).items()],
                      most_positive_tokens=[(k_, round(v_, 3)) for k_, v_ in infl.head(3).items()])
    res[f'c jackknife {v}'] = rec
    print('c', v, json.dumps(rec), flush=True)
json.dump(res, open(f'{OUT}/s4_extra.json', 'w'), indent=1, default=str)
