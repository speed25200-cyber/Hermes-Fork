"""Bar battery for the pre-registrable ensembles on the verifier's hybrid2 prices (OKX REST candles + OKX OHLC rebuilt
from trade archives), researcher's simulator logic (sim_v). Members run as equal-capital sub-accounts; ensemble daily
return = mean of member daily returns. Total-gross convention: member coin cap = L / (1 + beta).
Stresses: costs x1.5 + fair 1-bar latency (scheduled entry/exit +1h, exchange stop still intrabar), stop fills +5%
worse / at the bar high, LOMO (drop days; and re-simulate without trades entered that month), LOCO (coin removed from
all members), leverage ladder with intrabar worst-case equity (all members' worst hours summed = conservative).
-> ens_robust.json"""
import sys, json, itertools
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/verify_listings')
import sim_v as S
from sim_v import *
from multiprocessing import Pool
g = pd.read_csv('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting/results/grid_hybrid.csv')
el = g[(g.is_trades >= 30) & (~g.is_liq)].sort_values('is_sharpe', ascending=False)
def cfg(r):
    return dict(side=int(r.side), d0=int(r.d0), d1=int(r.d1) * 24, beta=float(r.beta), stop=None if r.stop == 0 else float(r.stop), uni=r.uni, K=5)
SETS = {'selected': [cfg(r) for r in el.head(1).itertuples()],
        'IS top-10': [cfg(r) for r in el.head(10).itertuples()],
        'plateau BTC-hedged (16)': [dict(side=-1, d0=d0, d1=168, beta=1.0, stop=st, uni=u, K=5)
                                    for d0, st, u in itertools.product([24, 72], [None, 0.25, 0.5, 1.0], ['okx', 'newtok'])],
        'plateau all (32)': [dict(side=-1, d0=d0, d1=168, beta=b, stop=st, uni=u, K=5)
                             for d0, b, st, u in itertools.product([24, 72], [0.0, 1.0], [None, 0.25, 0.5, 1.0], ['okx', 'newtok'])]}
_D = None
def Dt():
    global _D
    if _D is None:
        _D = Data('hybrid2')
    return _D
def run(args):
    c, Ltot, start, end, kw, mode = args
    S.STOP_SLIP = 0.05 if mode == 'slip5' else 0.0
    S.STOP_WORST = mode == 'worst'
    cc = dict(c)
    if mode == 'fairlat':
        cc['d0'] += 1; cc['d1'] += 1
    r = simulate(Dt(), cc, start, end, L=Ltot / (1 + c['beta']), record=kw.pop('record', False), **kw)
    S.STOP_SLIP = 0.0; S.STOP_WORST = False
    o = dict(ret=r['ret'], liq=r['liq'], maxdd=r['maxdd'], trades=[(t['sym'], t['notional'] * t['ret']) for t in r['trades']])
    if 'eq' in r:
        o['eq'] = r['eq']; o['eq_worst'] = r['eq_worst'].fillna(r['eq'])
    return o
def sh(x):
    x = np.asarray(x, float); s = x.std(ddof=1); return float(x.mean() / s * np.sqrt(365)) if s > 0 else 0.0
def ens(results):
    return pd.concat([r['ret'] for r in results], axis=1).mean(axis=1)
if __name__ == '__main__':
    pool = Pool(4)
    out = {}
    for nm, members in SETS.items():
        o = {}
        def batch(Ltot=1.0, start=OOS_START, end=OOS_END, mode='base', **kw):
            return pool.map(run, [(c, Ltot, start, end, dict(kw), mode) for c in members])
        base = batch(1.0)
        r = ens(base)
        ri = ens(batch(1.0, IS_START, OOS_START))
        y = {str(k): float(np.prod(1 + v) - 1) for k, v in r.groupby(r.index.year)}
        o['is_sharpe'] = sh(ri); o['oos_sharpe'] = sh(r); o['oos_years'] = y
        m = r.index.to_period('M')
        lm = {str(p): sh(r[m != p]) for p in m.unique()}
        o['lomo_min'] = min(lm.values()); o['lomo_month'] = min(lm, key=lm.get)
        # re-simulated LOMO for the 3 worst months
        worst3 = sorted(lm, key=lm.get)[:3]
        o['lomo_resim'] = {p: sh(ens(batch(1.0, excl_months={p}))) for p in worst3}
        # LOCO
        pnl = {}
        for rr in base:
            for s_, p_ in rr['trades']:
                pnl[s_] = pnl.get(s_, 0.0) + p_
        syms = sorted(pnl)
        lc = {s_: sh(ens(batch(1.0, exclude={s_}))) for s_ in syms}
        o['loco_min'] = min(lc.values()); o['loco_coin'] = min(lc, key=lc.get); o['loco_n'] = len(lc)
        top = sorted(pnl, key=pnl.get, reverse=True)
        o['drop_top2'] = sh(ens(batch(1.0, exclude=set(top[:2])))); o['drop_top5'] = sh(ens(batch(1.0, exclude=set(top[:5]))))
        o['top5_pnl_share'] = float(sum(pnl[s_] for s_ in top[:5]) / sum(pnl.values()))
        o['cost15_fairlat1'] = sh(ens(batch(1.0, mode='fairlat', cost_mult=1.5)))
        o['cost15'] = sh(ens(batch(1.0, cost_mult=1.5)))
        o['stop_slip5'] = sh(ens(batch(1.0, mode='slip5')))
        rw = ens(batch(1.0, mode='worst'))
        o['stop_worst'] = sh(rw); o['stop_worst_years'] = {str(k): float(np.prod(1 + v) - 1) for k, v in rw.groupby(rw.index.year)}
        o['stop_worst_cost15_fairlat1'] = sh(ens(batch(1.0, mode='worst', cost_mult=1.5)))  # stop at bar high + costs x1.5
        o['half_kelly_is_total'] = float(0.5 * ri.mean() / ri.var())
        lev = {}
        for Ltot in [1, 2, 3, 4, 5, 6, 8, 10]:
            row = {}
            for mode in ('base', 'worst'):
                rs = batch(float(Ltot), mode=mode, record=True)
                E = pd.concat([x['eq'] for x in rs], axis=1).mean(axis=1)
                W = pd.concat([x['eq_worst'] for x in rs], axis=1).mean(axis=1)
                dd = float((1 - W / E.cummax()).max())
                re = ens(rs)
                row[mode] = dict(maxdd_intrabar=dd, any_member_liq=any(x['liq'] for x in rs), sharpe=sh(re), cagr=float(np.prod(1 + re) ** (365 / len(re)) - 1),
                                 y2025=float(np.prod(1 + re[re.index.year == 2025]) - 1), y2026=float(np.prod(1 + re[re.index.year == 2026]) - 1))
            rs = batch(float(Ltot), IS_START, OOS_START, record=True)
            E = pd.concat([x['eq'] for x in rs], axis=1).mean(axis=1); W = pd.concat([x['eq_worst'] for x in rs], axis=1).mean(axis=1)
            row['is_maxdd_intrabar'] = float((1 - W / E.cummax()).max())
            lev[Ltot] = row
            print(nm, Ltot, {k: (round(v['maxdd_intrabar'], 3), v['any_member_liq'], round(v['cagr'], 3)) for k, v in row.items() if isinstance(v, dict)}, round(row['is_maxdd_intrabar'], 3), flush=True)
        o['leverage'] = lev
        ok = [L for L, v in lev.items() if v['base']['maxdd_intrabar'] <= 0.35 and not v['base']['any_member_liq'] and L <= o['half_kelly_is_total']]
        okw = [L for L, v in lev.items() if v['worst']['maxdd_intrabar'] <= 0.35 and not v['worst']['any_member_liq'] and L <= o['half_kelly_is_total']]
        o['supportable_L_total_base'] = max(ok) if ok else 0; o['supportable_L_total_stopworst'] = max(okw) if okw else 0
        out[nm] = o
        print(nm, json.dumps({k: v for k, v in o.items() if k != 'leverage'}, default=str), flush=True)
        pd.DataFrame({'ret': r}).to_csv(f'oos_daily_{nm.split()[0]}_{len(members)}.csv')
        pd.DataFrame({'ret': ri}).to_csv(f'is_daily_{nm.split()[0]}_{len(members)}.csv')
    json.dump(out, open('ens_robust.json', 'w'), indent=1, default=str)
