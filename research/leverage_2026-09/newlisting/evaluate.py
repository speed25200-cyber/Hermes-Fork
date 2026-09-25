"""Success-bar evaluation of the IS-selected config (selection rule applied to results/grid.csv: highest IS Sharpe
among configs with >= 30 IS trades and no IS liquidation), plus the next IS-ranked configs for context.
Bar items: (1) OOS Sharpe >= 1.5 at 1x, 2025 and 2026 > 0; (2) LOMO / LOCO min OOS Sharpe >= 1.0, costs x1.5 +
1 bar latency OOS Sharpe >= 0.8; (4) supportable leverage: largest L in {1,2,3,5,8,10,15,20} with OOS intrabar
max DD <= 35%, no liquidation, and L <= half-Kelly from IS daily returns. (3) is checked in executability.py.
-> results/evaluation.json, results/oos_daily_selected.csv, results/trades_selected.csv"""
import json, os, sys
from sim import *

OUT = os.path.join(BASE, 'results')
LEVS = [1, 2, 3, 5, 8, 10, 15, 20]


def cfg_of(row):
    return dict(side=int(row.side), d0=int(row.d0), d1=int(row.d1) * 24, beta=float(row.beta),
                stop=None if row.stop == 0 else float(row.stop), uni=row.uni, K=int(row.K))


def lomo(ret):
    m = ret.index.to_period('M')
    return {str(p): sharpe(ret[m != p]) for p in m.unique()}


def full_eval(Dt, cfg, label, loco=True):
    ev = {}
    ris = simulate(Dt, cfg, IS_START, OOS_START)
    roos = simulate(Dt, cfg, OOS_START, OOS_END, record=True)
    ev['cfg'] = {k: v for k, v in cfg.items()}
    ev['is'] = summarize(ris)
    ev['oos'] = summarize(roos)
    r = roos['ret']
    lm = lomo(r)
    ev['lomo_min'] = min(lm.values())
    ev['lomo_min_month'] = min(lm, key=lm.get)
    ev['lomo'] = lm
    if loco:
        syms = sorted({t['sym'] for t in roos['trades']})
        lc = {s: sharpe(simulate(Dt, cfg, OOS_START, OOS_END, exclude={s})['ret']) for s in syms}
        ev['loco_min'] = min(lc.values())
        ev['loco_min_coin'] = min(lc, key=lc.get)
        ev['loco_n'] = len(lc)
    ev['cost15'] = sharpe(simulate(Dt, cfg, OOS_START, OOS_END, cost_mult=1.5)['ret'])
    ev['lat1'] = sharpe(simulate(Dt, cfg, OOS_START, OOS_END, lat=1)['ret'])
    ev['cost15_lat1'] = sharpe(simulate(Dt, cfg, OOS_START, OOS_END, cost_mult=1.5, lat=1)['ret'])
    ev['cost2_lat1'] = sharpe(simulate(Dt, cfg, OOS_START, OOS_END, cost_mult=2.0, lat=1)['ret'])
    # half-Kelly on IS daily returns at 1x (continuous-time approximation f* = mu / sigma^2)
    ri = ris['ret']
    ev['kelly_is'] = float(ri.mean() / ri.var())
    ev['half_kelly_is'] = 0.5 * ev['kelly_is']
    lev = {}
    for L in LEVS:
        a = simulate(Dt, cfg, OOS_START, OOS_END, L=L)
        b = simulate(Dt, cfg, IS_START, OOS_START, L=L)
        lev[L] = dict(oos=summarize(a), is_=summarize(b))
    ev['leverage'] = {L: dict(oos_sharpe=v['oos']['sharpe'], oos_cagr=v['oos']['cagr'], oos_maxdd=v['oos']['maxdd'],
                              oos_liq=v['oos']['liq'], is_maxdd=v['is_']['maxdd'], is_liq=v['is_']['liq'],
                              is_cagr=v['is_']['cagr']) for L, v in lev.items()}
    ok = [L for L in LEVS if lev[L]['oos']['maxdd'] <= 0.35 and not lev[L]['oos']['liq'] and L <= ev['half_kelly_is']]
    ev['supportable_L'] = max(ok) if ok else 0
    o = ev['oos']
    ev['bar1'] = bool(o['sharpe'] >= 1.5 and o['years'].get(2025, -1) > 0 and o['years'].get(2026, -1) > 0)
    ev['bar2'] = bool(ev['lomo_min'] >= 1.0 and ev.get('loco_min', -9) >= 1.0 and ev['cost15_lat1'] >= 0.8)
    ev['label'] = label
    return ev, roos, ris


if __name__ == '__main__':
    PRICE = sys.argv[1] if len(sys.argv) > 1 else 'hybrid'
    g = pd.read_csv(os.path.join(OUT, f'grid_{PRICE}.csv'))
    el = g[(g.is_trades >= 30) & (~g.is_liq)].sort_values('is_sharpe', ascending=False)
    Dt = Data(PRICE)
    res = []
    for rank, (_, row) in enumerate(el.head(5).iterrows()):
        cfg = cfg_of(row)
        ev, roos, ris = full_eval(Dt, cfg, f'IS rank {rank + 1}', loco=True)
        res.append(ev)
        print(json.dumps({k: v for k, v in ev.items() if k not in ('lomo',)}, default=str), flush=True)
        if rank == 0:
            d = pd.DataFrame({'ret': roos['ret']})
            d.to_csv(os.path.join(OUT, f'oos_daily_selected_{PRICE}.csv'))
            pd.DataFrame({'ret': ris['ret']}).to_csv(os.path.join(OUT, f'is_daily_selected_{PRICE}.csv'))
            tr = pd.DataFrame(roos['trades'] + ris['trades'])
            tr['entry_time'] = pd.to_datetime(G0 + tr.entry_t * HMS, unit='ms')
            tr['exit_time'] = pd.to_datetime(G0 + tr.exit_t * HMS, unit='ms')
            tr.drop(columns=['E_before']).sort_values('entry_time').to_csv(os.path.join(OUT, f'trades_selected_{PRICE}.csv'), index=False)
            roos['eq'].resample('D').last().to_csv(os.path.join(OUT, f'oos_equity_selected_{PRICE}.csv'))
    json.dump(res, open(os.path.join(OUT, f'evaluation_{PRICE}.json'), 'w'), indent=1, default=str)
