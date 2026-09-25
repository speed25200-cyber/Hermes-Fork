from common import *
ev = pd.read_parquet(os.path.join(D, 'events.parquet'))
P = dict(np.load(os.path.join(D, 'panel.npz')))
ev['year'] = pd.to_datetime(ev.t0, unit='ms').dt.year
ev['newtok'] = ev.spot_first.isna() | ((pd.to_datetime(ev.t0, unit='ms') - ev.spot_first).dt.days <= 30)
ok_cat = ~ev.okx_cat.isin(['3', '4'])
rows = []
for d0 in [1 / 24, 1, 3, 7, 14, 30]:
    for d1 in [7, 14, 30, 60, 90]:
        if d1 <= d0:
            continue
        a, b = int(round(d0 * 24)), int(d1 * 24)
        p0, p1 = P['o'][:, a], P['o'][:, b]
        b0, b1 = P['bo'][:, a], P['bo'][:, b]
        r = p1 / p0 - 1
        rb = b1 / b0 - 1
        fund = P['fund'][:, a:b].sum(1)
        exe = P['okx_on'][:, a]
        for uni, m in [('all', ok_cat.values), ('okx', exe), ('okx_newtok', exe & ev.newtok.values)]:
            for per, pm in [('IS', ev.year.between(2022, 2024).values), ('OOS', ev.year.between(2025, 2026).values)]:
                mm = m & pm & np.isfinite(r)
                s = -r[mm] + fund[mm]           # short, unhedged, funding received
                sh = -(r[mm] - rb[mm]) + fund[mm]   # short hedged beta 1
                rows.append(dict(d0=d0, d1=d1, uni=uni, per=per, n=int(mm.sum()), short=s.mean(), short_med=np.median(s) if len(s) else np.nan,
                                 hedged=sh.mean(), hedged_med=np.median(sh) if len(sh) else np.nan, t_h=sh.mean() / sh.std() * np.sqrt(len(sh)) if len(sh) > 2 else np.nan,
                                 fund=fund[mm].mean(), hit=(sh > 0).mean()))
R = pd.DataFrame(rows)
pd.set_option('display.width', 250); pd.set_option('display.max_rows', 500)
print(R.round(3).to_string())
