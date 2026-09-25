"""BTC hedge source comparison; event-clustered significance of the OKX-extra result."""
import json
from v_common import *
Do = load_okx(); Db = load_bn()
N = int(max(Db.g0.max(), Do.g0.max()) + 240)
def glob(D):
    g = np.full(N, np.nan)
    for i in range(D.n):
        k = np.arange(min(D.H, 240)); t = D.g0[i] + k
        v = D.bc[i, :len(k)]; ok = np.isfinite(v) & (t >= 0) & (t < N)
        g[t[ok]] = v[ok]
    return g
gb, go = glob(Db), glob(Do)
both = np.isfinite(gb) & np.isfinite(go)
rb = np.diff(np.log(gb)); ro = np.diff(np.log(go)); okr = np.isfinite(rb) & np.isfinite(ro)
out = dict(btc_common_hours=int(both.sum()), btc_level_mean_abs_rel_diff=float(np.nanmean(np.abs(gb[both] / go[both] - 1))),
           btc_hourly_ret_corr=float(np.corrcoef(rb[okr], ro[okr])[0, 1]), btc_hourly_ret_mean_abs_diff=float(np.mean(np.abs(rb[okr] - ro[okr]))))
print(out)
res = {}
for pn, a, b in [('2022-2026', '2022-01-01', '2026-09-25'), ('2022-2024', '2022-01-01', '2025-01-01'), ('2025-2026', '2025-01-01', '2026-09-25'), ('2026', '2026-01-01', '2026-09-25'), ('2026 Jan-Aug', '2026-01-01', '2026-09-01')]:
    r, tr = run(Do, a, b)
    ret = r['ret']
    ev_mean = tr.groupby('i').ret.mean()
    # daily Sharpe t ~ SR * sqrt(years)
    yrs = len(ret) / 365
    # block bootstrap of daily returns (20-day blocks) for Sharpe CI
    rng = np.random.default_rng(0); x = ret.values; nb = int(np.ceil(len(x) / 20)); srs = []
    for _ in range(2000):
        st = rng.integers(0, len(x) - 20, nb); s = np.concatenate([x[j:j + 20] for j in st])[:len(x)]
        srs.append(sharpe(s))
    res[pn] = dict(sharpe=round(sharpe(ret), 3), sharpe_t=round(sharpe(ret) * np.sqrt(yrs), 2), trades=len(tr), t_trades=round(tstat(tr.ret), 2),
                   events=int(ev_mean.size), t_events=round(tstat(ev_mean), 2), mean_event=round(float(ev_mean.mean()), 4),
                   boot90=[round(float(np.percentile(srs, 5)), 2), round(float(np.percentile(srs, 95)), 2)])
    print(pn, res[pn])
out['okx_alone_significance'] = res
json.dump(out, open(SP + '/xlist/verify/v7_stats.json', 'w'), indent=1)
