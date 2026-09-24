"""Verifier step 2 (1-minute bound): recompute the 1m MARK bounds from data.binance.vision (in memory) for the two
pair schemes that contain every IS-selected config (coint/lvl/Wf60 and coint/ret/Wf60), reproduce the reported m1
numbers, then run adversarial variants:
  A  maker trade-through 5 bp and 10 bp instead of 1 bp
  B  lenient liquidation (cross account keeps worst-case equity - 0.05% of notional and continues)
  C  leave-one-coin-out and leave-best-month-out for the OOS-positive picks
  D  liquidation timestamps (which event kills the high-leverage picks)
Outputs: out/v2_m1_runs.csv, out/v2_loo.csv, out/v2_liq_events.csv, out/v2_trades_*.csv"""
import os, sys, json, time, copy, pickle
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_v2 import *
from sim3 import sim3
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
KEYS = [('coint', 'lvl', 60), ('coint', 'ret', 60)]
t = time.time()
pm = sorted(all_pair_months({k: sel[k] for k in KEYS}))
bounds = minute_bound.compute_bounds(pm, list(P['syms']), P['o'].astype(np.float64), GRID, FORM_DATES, log=lambda s: None)
print('bounds', len(bounds), f'{time.time()-t:.0f}s', flush=True)
O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
FR = P['fr'].astype(np.float64)
day_all = GRID.normalize()
PER = {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}
names = list(P['syms'])

def run(sc, sg, lev, period, ex, tick=1e-4, liqmode=0, sel_override=None, rec=0, use_w=True):
    method, hedge, Wf, K = sc; Wz, zin, zout, zstop = sg
    s_m = (sel_override or sel)[(method, hedge, Wf)]
    PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, s_m, K, Wz, base_slip)
    WLa, WSa = w_arrays(s_m, K, bounds)
    p0, p1 = PER[period]
    i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < G1 else NB
    days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D')
    DAY = ((day_all - p0).days).values.astype(np.int64)
    daily, st, TR = sim3(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, WLa, WSa, mmr, imr,
                         zin, zout, zstop, Wz, float(lev), False, FEE_T, FEE_M, RANGE_SLIP, ex == 'maker', use_w, rec,
                         tick, liqmode)
    m = metrics(daily, days)
    return m, st, TR, pd.Series(daily, index=days)

# IS-selected configs (per leverage, from grid) + the L=1 choice
PICKS = {'pick1_3': (('coint', 'lvl', 60, 3), (336, 2.5, 0.5, 101.5)),
         'pick5': (('coint', 'lvl', 60, 3), (336, 2.5, 0.5, 4.0)),
         'pick10': (('coint', 'ret', 60, 10), (72, 3.0, 0.5, 4.5)),
         'pick15': (('coint', 'lvl', 60, 10), (72, 3.0, 0.5, 4.5)),
         'pick20': (('coint', 'ret', 60, 10), (72, 2.0, 0.5, 3.5))}
g = pd.concat([pd.read_csv('../pairs/out/grid_v2.csv.gz'), pd.read_csv('../pairs/out/grid_v2_low.csv.gz')])
g = g[(g.liq == 'm1') & (g.margin == 'cross')]
rows = []; liq_ev = []
for name, (sc, sg) in PICKS.items():
    for period in ['IS', 'OOS']:
        for ex in ['maker', 'taker']:
            for lev in LEVS:
                for variant, kw in [('base', {}), ('tick5bp', dict(tick=5e-4)), ('tick10bp', dict(tick=1e-3)),
                                    ('lenient_liq', dict(liqmode=1))]:
                    if variant.startswith('tick') and ex == 'taker':
                        continue
                    m, st, TR, dser = run(sc, sg, lev, period, ex, rec=5000, **kw)
                    r = dict(pick=name, method=sc[0], hedge=sc[1], Wf=sc[2], K=sc[3], Wz=sg[0], zin=sg[1], zout=sg[2],
                             zstop=sg[3], exec=ex, lev=lev, period=period, variant=variant, cagr=m['cagr'],
                             final=m['final'], sharpe=m['sharpe'], worst_day=m['worst_day'], maxdd=st[1], trades=int(st[2]),
                             liqs=int(st[3]), legged=int(st[11]), maker_miss=int(st[12]), wbars=int(st[13]), fbbars=int(st[14]),
                             per_year=json.dumps({str(k): round(v, 4) for k, v in m['per_year'].items()}))
                    if variant == 'base':
                        q = g[(g.method == sc[0]) & (g.hedge == sc[1]) & (g.Wf == sc[2]) & (g.K == sc[3]) & (g.Wz == sg[0]) &
                              (g.zin == sg[1]) & (g.zout == sg[2]) & (g.zstop == sg[3]) & (g.exec == ex) & (g.lev == lev) &
                              (g.period == period)]
                        r['cagr_reported'] = float(q.cagr.iloc[0]) if len(q) else np.nan
                        r['final_reported'] = float(q.final.iloc[0]) if len(q) else np.nan
                    for tr in TR:
                        if tr[6] == 5:
                            liq_ev.append(dict(pick=name, exec=ex, lev=lev, period=period, variant=variant,
                                               when=str(GRID[int(tr[4])]),
                                               slots=';'.join(f'{names[int(x[1])]}/{names[int(x[2])]}' for x in TR
                                                              if x[6] != 5 and x[4] >= tr[4] - 1 and x[3] <= tr[4])))
                    rows.append(r)
    print(name, 'done', flush=True)
R = pd.DataFrame(rows)
R.to_csv('out/v2_m1_runs.csv', index=False)
pd.DataFrame(liq_ev).to_csv('out/v2_liq_events.csv', index=False)
pd.set_option('display.width', 250)
b = R[R.variant == 'base']
b = b.assign(diff=(b.final - b.final_reported).abs())
print('REPRO max |final - reported|', b['diff'].max(), 'rows', len(b), 'missing', b.cagr_reported.isna().sum())
print(b[['pick', 'exec', 'lev', 'period', 'cagr', 'cagr_reported', 'maxdd', 'worst_day', 'liqs', 'trades', 'sharpe', 'per_year']].round(4).to_string(index=False))
pv = R.pivot_table(index=['pick', 'exec', 'period', 'lev'], columns='variant', values='cagr').round(4)
print(pv.to_string())
# keep bounds for later steps (compact pickle in /dev/shm-free location: memory only -> dump small file)
with open('out/bounds_2schemes.pkl', 'wb') as f:
    pickle.dump({k: (v[0].astype(np.float32), v[1].astype(np.float32)) for k, v in bounds.items()}, f)
print('saved bounds', os.path.getsize('out/bounds_2schemes.pkl') / 1e6, 'MB')
