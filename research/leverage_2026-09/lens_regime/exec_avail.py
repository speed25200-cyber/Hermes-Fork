"""(e) Availability and funding. Share of new-token Binance USDT-M listings that OKX lists (point-in-time archive
calendar) by +24h / +72h, per year; realized funding received by the short over +72h..+168h and +24h..+168h
(OKX realized funding where recorded, else Binance), per year; extreme funding events. -> exec_avail.json"""
import sys, json
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
from sim import *
Dt = Data('hybrid')
P = np.load(os.path.join(D, 'panel2.npz'))
fo = P['fund_okx']
t0 = pd.to_datetime(Dt.ev.t0, unit='ms')
yr = t0.dt.year.values
out = {'availability': {}, 'funding': {}}
for y in range(2022, 2027):
    m = Dt.newtok & (yr == y) & (Dt.ev.t0.values < int(pd.Timestamp('2026-08-24').value // 10 ** 6))
    a = dict(n_newtok=int(m.sum()), on_okx_24h=float(Dt.okx_on[m, 24].mean()), on_okx_72h=float(Dt.okx_on[m, 72].mean()),
             on_okx_168h=float(Dt.okx_on[m, 167].mean()), n_on_72h=int(Dt.okx_on[m, 72].sum()))
    out['availability'][str(y)] = a
    print(y, a)
for d0 in (24, 72):
    rows = []
    for i in range(Dt.n):
        if not (Dt.newtok[i] and Dt.okx_on[i, d0] and np.isfinite(Dt.o[i, d0])):
            continue
        f = Dt.fund[i, d0:168]
        rows.append(dict(y=yr[i], sum=float(np.nansum(f)), min=float(np.nanmin(f)), okx_rec=bool(np.isfinite(fo[i, d0:168]).any())))
    F = pd.DataFrame(rows)
    g = F.groupby('y').agg(n=('sum', 'size'), mean_sum=('sum', 'mean'), med_sum=('sum', 'median'), p5_sum=('sum', lambda v: v.quantile(0.05)),
                           min_sum=('sum', 'min'), frac_neg=('sum', lambda v: (v < 0).mean()), worst_single=('min', 'min'), okx_rec=('okx_rec', 'mean'))
    print(f'\nfunding received by the short, sum of rates over +{d0}h..+168h'); print(g.round(4).to_string())
    out['funding'][f'd0={d0}'] = g.reset_index().to_dict('records')
    oos = F[F.y >= 2025]
    out['funding'][f'd0={d0}_oos_mean'] = float(oos['sum'].mean())
json.dump(out, open('exec_avail.json', 'w'), indent=1, default=str)
