"""Verifier robustness battery for the IS-selected config (short d0=72h d1=7d, BTC hedge, stop 50%, newtok, K=5).
Usage: robust.py <price: hybrid|binance|hybrid2> -> robust_<price>.json"""
import sys, json
import sim_v as S
from sim_v import *
price = sys.argv[1]
Dt = Data(price)
cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
out = {}
def sm(**kw):
    return summarize(simulate(Dt, cfg, OOS_START, OOS_END, **kw))
b = simulate(Dt, cfg, OOS_START, OOS_END, record=True)
base = summarize(b)
out['base'] = base
r = b['ret']
m = r.index.to_period('M')
lomo = {str(p): sharpe(r[m != p]) for p in m.unique()}
out['lomo_drop_days_min'] = min(lomo.values()); out['lomo_drop_days_month'] = min(lomo, key=lomo.get)
# stricter LOMO: re-simulate without trades ENTERED in that month (their P&L spills into the next month otherwise)
lomo2 = {}
for p in m.unique():
    lomo2[str(p)] = sharpe(simulate(Dt, cfg, OOS_START, OOS_END, excl_months={str(p)})['ret'])
out['lomo_resim_min'] = min(lomo2.values()); out['lomo_resim_month'] = min(lomo2, key=lomo2.get)
out['lomo_resim_sorted'] = dict(sorted(lomo2.items(), key=lambda kv: kv[1])[:5])
syms = sorted({t['sym'] for t in b['trades']})
lc = {s: sharpe(simulate(Dt, cfg, OOS_START, OOS_END, exclude={s})['ret']) for s in syms}
out['loco_min'] = min(lc.values()); out['loco_coin'] = min(lc, key=lc.get); out['loco_n'] = len(lc)
# leave-two-coins-out: drop the two best coins by trade P&L contribution
tr = pd.DataFrame(b['trades']); tr['pnl'] = tr.notional * tr.ret
best = tr.groupby('sym').pnl.sum().sort_values(ascending=False)
out['top_coin_pnl_share'] = {k: float(v) for k, v in (best.head(5) / tr.pnl.sum()).items()}
out['drop_top2'] = sharpe(simulate(Dt, cfg, OOS_START, OOS_END, exclude=set(best.index[:2]))['ret'])
out['drop_top5'] = sharpe(simulate(Dt, cfg, OOS_START, OOS_END, exclude=set(best.index[:5]))['ret'])
out['cost15_lat1'] = sm(cost_mult=1.5, lat=1)['sharpe']
out['cost15'] = sm(cost_mult=1.5)['sharpe']
out['lat1'] = sm(lat=1)['sharpe']
out['slip30bp'] = sm(slip_coin=0.003)['sharpe']
out['slip50bp'] = sm(slip_coin=0.005)['sharpe']
S.STOP_WORST = True
w = sm(); out['stopworst'] = dict(sharpe=w['sharpe'], years=w['years'], maxdd=w['maxdd'])
out['stopworst_cost15_lat1'] = sm(cost_mult=1.5, lat=1)['sharpe']
S.STOP_WORST = False
# leverage, total-gross convention (coin cap = L/2) and short-leg convention; with stress variants
lev = {}
for Lt in [1, 2, 3, 5, 8, 10, 15, 20]:
    row = {}
    for nm, kw in (('base', {}), ('cost15_lat1', dict(cost_mult=1.5, lat=1)), ('cost15', dict(cost_mult=1.5))):
        s = summarize(simulate(Dt, cfg, OOS_START, OOS_END, L=Lt / 2, **kw))
        row[nm] = dict(maxdd=s['maxdd'], liq=s['liq'], sharpe=s['sharpe'], cagr=s['cagr'])
    S.STOP_WORST = True
    s = summarize(simulate(Dt, cfg, OOS_START, OOS_END, L=Lt / 2)); row['stopworst'] = dict(maxdd=s['maxdd'], liq=s['liq'], sharpe=s['sharpe'])
    S.STOP_WORST = False
    s = summarize(simulate(Dt, cfg, OOS_START, OOS_END, L=Lt)); row['shortleg_conv'] = dict(maxdd=s['maxdd'], liq=s['liq'])
    lev[Lt] = row
out['leverage_total_gross'] = lev
ri = simulate(Dt, cfg, IS_START, OOS_START, L=0.5)['ret']
out['half_kelly_total'] = float(0.5 * ri.mean() / ri.var())
rr = simulate(Dt, cfg, OOS_START, OOS_END, L=1.5, record=True)
out['realized_avg_gross_at_3x_total'] = float(rr['gross'].mean())
out['realized_p99_gross_at_3x_total'] = float(rr['gross'].quantile(0.99))
json.dump(out, open(f'robust_{price}.json', 'w'), indent=1, default=str)
print(json.dumps({k: v for k, v in out.items() if k != 'leverage_total_gross'}, indent=1, default=str))
for L, v in lev.items():
    print(L, {k: (round(x['maxdd'], 3), x['liq']) for k, x in v.items()})
