"""Verifier step 5 (1h, m1 bound): episode dependence of the OOS-positive IS picks.
  - leave-one-coin-out (pairs containing the coin removed, slot left empty)
  - leave-best-month(s)-out on the OOS daily equity
  - positions open at each OOS liquidation of the high-leverage picks (from the 1x run: same signals)"""
import os, sys, json, pickle
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_v2 import *
from sim3 import sim3
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
bounds = {k: (v[0].astype(np.float64), v[1].astype(np.float64)) for k, v in pickle.load(open('out/bounds_2schemes.pkl', 'rb')).items()}
O = P['o'].astype(np.float64); H = P['h'].astype(np.float64); Lw = P['l'].astype(np.float64)
Cc = P['c'].astype(np.float64); MH = P['mh'].astype(np.float64); ML = P['ml'].astype(np.float64)
FR = P['fr'].astype(np.float64); day_all = GRID.normalize(); names = list(P['syms'])
PER = {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}

def run(sc, sg, lev, period, ex, s_m=None, rec=0, tick=1e-4):
    method, hedge, Wf, K = sc; Wz, zin, zout, zstop = sg
    s_m = s_m or sel[(method, hedge, Wf)]
    PA, PB, BE, Z, SA, SB, NEWM = slot_arrays(P, U, s_m, K, Wz, base_slip)
    WLa, WSa = w_arrays(s_m, K, bounds)
    p0, p1 = PER[period]; i0 = GRID.get_loc(p0); i1 = GRID.get_loc(p1) if p1 < G1 else NB
    days = pd.date_range(p0, p1 - pd.Timedelta(days=1), freq='D'); DAY = ((day_all - p0).days).values.astype(np.int64)
    daily, st, TR = sim3(O, H, Lw, Cc, MH, ML, FR, DAY, i0, i1, len(days), PA, PB, BE, Z, SA, SB, NEWM, WLa, WSa, mmr, imr,
                         zin, zout, zstop, Wz, float(lev), False, FEE_T, FEE_M, RANGE_SLIP, ex == 'maker', True, rec, tick, 0)
    return metrics(daily, days), st, TR, pd.Series(daily, index=days)

def month_stats(d):
    mo = d.ffill().resample('ME').last(); mr = mo.pct_change(); mr.iloc[0] = mo.iloc[0] - 1
    out = {}
    for drop in [1, 2]:
        keep = mr.drop(mr.nlargest(drop).index)
        out[f'cagr_drop_best{drop}'] = float(np.prod(1 + keep) ** (12 / len(keep)) - 1)
    return mr, out

CASES = [('pick1x', ('coint', 'lvl', 60, 3), (336, 2.5, 0.5, 101.5), 1),
         ('pick10x', ('coint', 'ret', 60, 10), (72, 3.0, 0.5, 4.5), 10),
         ('pick10x_at1x', ('coint', 'ret', 60, 10), (72, 3.0, 0.5, 4.5), 1)]
res = {}
pd.set_option('display.width', 250)
for name, sc, sg, lev in CASES:
    key = sc[:3]
    m, st, TR, d = run(sc, sg, lev, 'OOS', 'maker', rec=20000)
    mr, ms = month_stats(d)
    print(f'=== {name}: OOS CAGR {m["cagr"]:.4f}, monthly returns:'); print(mr.round(4).to_string())
    print(name, {k: round(v, 4) for k, v in ms.items()})
    tr = pd.DataFrame(TR, columns=['slot', 'a', 'b', 'tent', 'texit', 'pnl', 'why', 'dir', 'eq0'])
    tr = tr[tr.why != 5]
    tr['pair'] = [f'{names[int(a)]}/{names[int(b)]}' for a, b in zip(tr.a, tr.b)]
    tr['pnl_eq'] = tr.pnl * (lev / sc[3])     # pnl was per slot basis? -> keep raw too
    top = tr.sort_values('pnl', ascending=False)
    print(name, 'trades', len(tr), 'top-5 trades (net pnl / equity at entry):')
    print(top.head(5)[['pair', 'pnl', 'why']].assign(when=[str(GRID[int(x)]) for x in top.head(5).tent]).to_string(index=False))
    print(name, 'bottom-3:'); print(top.tail(3)[['pair', 'pnl', 'why']].assign(when=[str(GRID[int(x)]) for x in top.tail(3).tent]).to_string(index=False))
    # by pair contribution (sum of log(1+pnl) approx)
    bp = tr.groupby('pair').pnl.sum().sort_values()
    print(name, 'pair P&L sum (fraction of equity), best 5:', bp.tail(5).round(3).to_dict())
    coins = sorted({s for t, v in sel[key].items() if t >= OOS0 for (a, b, be) in v[:sc[3]] for s in (a, b)})
    loo = []
    for c in coins:
        s2 = {t: [p for p in v if c not in p[:2]] if t >= OOS0 - pd.offsets.MonthBegin(1) else v for t, v in sel[key].items()}
        # keep top-K membership as in the original (drop without replacement): truncate to K first
        s2 = {t: [p for p in v[:sc[3]] if c not in p[:2]] for t, v in sel[key].items()}
        m2, st2, _, _ = run(sc, sg, lev, 'OOS', 'maker', s_m=s2)
        loo.append(dict(coin=names[c], cagr_oos=m2['cagr'], trades=int(st2[2]), liqs=int(st2[3])))
    loo = pd.DataFrame(loo).sort_values('cagr_oos')
    print(name, 'LOO coins', len(loo), 'min %.4f median %.4f max %.4f n<=0 %d' % (loo.cagr_oos.min(), loo.cagr_oos.median(),
          loo.cagr_oos.max(), (loo.cagr_oos <= 0).sum()))
    print(loo.head(6).round(4).to_string(index=False))
    res[name] = dict(cagr_oos=m['cagr'], **ms, loo_min=loo.cagr_oos.min(), loo_median=loo.cagr_oos.median(),
                     loo_n_nonpos=int((loo.cagr_oos <= 0).sum()), loo_n=len(loo), worst_loo=loo.iloc[0].to_dict())
    loo.to_csv(f'out/v5_loo_{name}.csv', index=False)
# positions open at the OOS liquidation hours (1x run of the same config = same signals)
ev = pd.read_csv('out/v2_liq_events.csv'); ev = ev[(ev.variant == 'base') & (ev.period == 'OOS')]
PK = {'pick1_3': (('coint', 'lvl', 60, 3), (336, 2.5, 0.5, 101.5)), 'pick5': (('coint', 'lvl', 60, 3), (336, 2.5, 0.5, 4.0)),
      'pick10': (('coint', 'ret', 60, 10), (72, 3.0, 0.5, 4.5)), 'pick15': (('coint', 'lvl', 60, 10), (72, 3.0, 0.5, 4.5)),
      'pick20': (('coint', 'ret', 60, 10), (72, 2.0, 0.5, 3.5))}
openpos = []
for pk, (sc, sg) in PK.items():
    for ex in ['maker', 'taker']:
        m, st, TR, d = run(sc, sg, 1, 'OOS', ex, rec=20000)
        tr = pd.DataFrame(TR, columns=['slot', 'a', 'b', 'tent', 'texit', 'pnl', 'why', 'dir', 'eq0'])
        for e in ev[(ev.pick == pk) & (ev.exec == ex)].itertuples():
            i = GRID.get_loc(pd.Timestamp(e.when))
            op = tr[(tr.tent <= i) & (tr.texit > i)]
            for r in op.itertuples():
                t = [t for t in FORM_DATES if t <= GRID[int(r.tent)]][-1]
                be = [p[2] for p in sel[sc[:3]][t] if p[0] == int(r.a) and p[1] == int(r.b)][0]
                openpos.append(dict(pick=pk, exec=ex, lev=e.lev, when=e.when, a=names[int(r.a)], b=names[int(r.b)],
                                    beta=be, dir=int(r.dir)))
op = pd.DataFrame(openpos)
print(op.to_string(index=False))
op.to_csv('out/v5_open_at_liq.csv', index=False)
json.dump(res, open('out/v5_loo_summary.json', 'w'), indent=1, default=str)
