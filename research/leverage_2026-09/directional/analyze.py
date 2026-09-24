import numpy as np, pandas as pd, json
from scipy.stats import norm
from bt import *
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 50); pd.set_option('display.max_rows', 300)
C = pd.read_csv(f'{OUT}/grid_results_cons.csv'); O = pd.read_csv(f'{OUT}/grid_results_opt.csv')
summary = {}

def dist(R):
    g = R.groupby('L').agg(n=('config', 'size'),
        IS_profitable=('IS_cagr', lambda x: int((x > 0).sum())),
        OOS_profitable=('OOS_cagr', lambda x: int((x > 0).sum())),
        OOS_profitable_both=('OOS_cagr', lambda x: int(((x > 0) & (R.loc[x.index, 'IS_cagr'] > 0)).sum())),
        IS_median_cagr=('IS_cagr', 'median'), OOS_median_cagr=('OOS_cagr', 'median'),
        OOS_best_cagr=('OOS_cagr', 'max'),
        OOS_frac_liquidated=('OOS_liq', lambda x: (x > 0).mean()),
        OOS_frac_down50=('OOS_final', lambda x: (x < 0.5).mean()),
        OOS_frac_liq_or_down50=('OOS_final', lambda x: ((x < 0.5) | (R.loc[x.index, 'OOS_liq'] > 0)).mean()),
        OOS_frac_maxdd50=('OOS_maxdd', lambda x: (x > 0.5).mean()),
        OOS_frac_ruined_lt1pct=('OOS_final', lambda x: (x < 0.01).mean()),
        IS_frac_liq_or_down50=('IS_final', lambda x: ((x < 0.5) | (R.loc[x.index, 'IS_liq'] > 0)).mean()))
    return g
for tag, R in [('conservative', C), ('optimistic', O)]:
    g = dist(R); print('=== distribution over the 288-config grid,', tag, 'intrabar ordering'); print(g.round(3).to_string())
    summary[f'distribution_{tag}'] = g.round(4).reset_index().to_dict(orient='records')
fam = C.groupby(['fam', 'tf', 'L']).agg(n=('config', 'size'), OOS_prof=('OOS_cagr', lambda x: int((x > 0).sum())),
                                        OOS_med=('OOS_cagr', 'median'), IS_med=('IS_cagr', 'median')).reset_index()
print(fam.pivot_table(index=['fam', 'tf'], columns='L', values='OOS_prof').to_string())
print(fam.pivot_table(index=['fam', 'tf'], columns='L', values='OOS_med').round(3).to_string())
summary['oos_profitable_by_family_tf'] = fam.round(4).to_dict(orient='records')

cols = ['L', 'IS_cagr', 'IS_maxdd', 'IS_worstday', 'IS_sharpe', 'IS_liq', 'IS_trades', 'OOS_cagr', 'OOS_maxdd', 'OOS_worstday',
        'OOS_sharpe', 'OOS_liq', 'OOS_trades', 'OOS_avglev', 'y2022', 'y2023', 'y2024', 'y2025', 'y2026']
# ---- Selection A (pre-registered): best IS Sharpe at 1x, then that config at every leverage
selA = {}
for scope, R in [('ALL', C), ('BTCUSDT', C[C.sym == 'BTCUSDT']), ('ETHUSDT', C[C.sym == 'ETHUSDT'])]:
    best = R[R.L == 1].sort_values('IS_sharpe', ascending=False).iloc[0].config
    T = C[C.config == best].sort_values('L')[cols]
    To = O[O.config == best].sort_values('L')[['L', 'OOS_cagr', 'OOS_maxdd', 'OOS_liq']].rename(columns=lambda c: c + '_optimistic' if c != 'L' else c)
    T = T.merge(To, on='L')
    print(f'=== Selection A [{scope}] best IS Sharpe@1x: {best}'); print(T.round(3).to_string(index=False))
    selA[scope] = (best, T)
summary['selectionA_best_IS_sharpe_1x'] = {k: {'config': v[0], 'table': v[1].round(4).to_dict(orient='records')} for k, v in selA.items()}
# ---- Selection B: best IS CAGR at each leverage
rows = []
for L in LEVS:
    r = C[C.L == L].sort_values('IS_cagr', ascending=False).iloc[0]
    ro = O[(O.config == r.config) & (O.L == L)].iloc[0]
    rows.append(dict(L=L, config=r.config, **{k: r[k] for k in cols if k != 'L'}, OOS_cagr_optimistic=ro.OOS_cagr, OOS_liq_optimistic=ro.OOS_liq))
SB = pd.DataFrame(rows); print('=== Selection B: best IS CAGR at each leverage'); print(SB.round(3).to_string(index=False))
summary['selectionB_best_IS_cagr_per_L'] = SB.round(4).to_dict(orient='records')
# ---- expected max Sharpe under the null (selection-bias check)
T_is = 3.0
for N in [288, 144, 50, 20]:
    emax = (1 - 0.5772) * norm.ppf(1 - 1 / N) + 0.5772 * norm.ppf(1 - 1 / (N * np.e))
    print(f'E[max IS Sharpe | no edge, N={N} indep. trials, 3y] ~ {emax / np.sqrt(T_is):.2f}')
summary['expected_max_null_sharpe_3y'] = {N: float(((1 - 0.5772) * norm.ppf(1 - 1 / N) + 0.5772 * norm.ppf(1 - 1 / (N * np.e))) / np.sqrt(T_is)) for N in [288, 144, 50, 20]}

# ---- Kelly for the Selection-A configs (from IS 1x returns only)
def rerun(config, L, win, cons=True):
    sym, tf, fam, param, stop, tp, sizing = config.split('|')
    tp = float(tp[2:]); p = eval(param) if fam != 'donch' else int(param)
    d = load(sym); t = d.t.values; n5 = len(d)
    day = ((t - t[0]) // 86400000).astype(np.int64)
    arrs = [d[k].values.astype(np.float64) for k in ['o', 'h', 'l', 'c', 'mo', 'mh', 'ml', 'fund']]
    ff = d.fund_flag.values.astype(np.int8)
    vm, _ = vol_mult(d, ms(IS0), ms(IS1))
    a0, a1 = (IS0, IS1) if win == 'IS' else (OOS0, OOS1)
    i0 = int(np.searchsorted(t, ms(a0))); i1 = int(np.searchsorted(t, ms(a1)))
    d0 = int(day[i0]); nd = int(day[i1 - 1] - d0 + 1)
    a, b_, c_, d_, atr, mh = signals(d, tf, fam, p)
    stopf = np.full(n5, 0.015) if stop == 'fix1.5%' else np.nan_to_num(2 * atr, nan=0.015)
    r = run_one(*arrs, ff, day, i0, i1, d0, nd, a, b_, c_, d_, stopf, vm, sizing == 'volscaled', float(L), tp, mh, cons, True)
    days = pd.to_datetime(t[0], unit='ms') + pd.to_timedelta(np.arange(d0, d0 + nd), unit='D')
    return r, pd.DatetimeIndex(days), d, i0, i1
kelly = {}
for scope, (best, T) in selA.items():
    r, days, d, i0, i1 = rerun(best, 1, 'IS')
    eq = pd.Series(np.concatenate([[1.0], r[0]]))
    dr = eq.pct_change().dropna().values
    mu, var = dr.mean() * 365, dr.var() * 365
    k_cont = mu / var
    unit = r[8] / r[9]            # per-trade return per unit of notional (1x)
    Ls = np.arange(0.25, 60.01, 0.25)
    g = np.array([np.sum(np.log(np.maximum(1 + L * unit, 1e-300))) for L in Ls])
    k_disc = Ls[np.argmax(g)]
    # growth at 10/15/20 relative to Kelly
    gl = {int(L): float(np.sum(np.log(np.maximum(1 + L * unit, 1e-300))) / 3.0) for L in [1, 3, 5, 10, 15, 20]}
    # OOS (ex post, information only)
    ro, _, _, _, _ = rerun(best, 1, 'OOS')
    uo = ro[8] / ro[9]
    go = np.array([np.sum(np.log(np.maximum(1 + L * uo, 1e-300))) for L in Ls])
    eqo = pd.Series(np.concatenate([[1.0], ro[0]])); dro = eqo.pct_change().dropna().values
    kelly[scope] = dict(config=best, IS_trades=int(len(unit)), IS_win_rate=float((unit > 0).mean()),
                        IS_mean_unit_trade=float(unit.mean()), IS_worst_unit_trade=float(unit.min()), IS_best_unit_trade=float(unit.max()),
                        IS_ann_mu_1x=float(mu), IS_ann_vol_1x=float(np.sqrt(var)), kelly_continuous_IS=float(k_cont),
                        kelly_per_trade_IS=float(k_disc), half_kelly_per_trade_IS=float(k_disc / 2),
                        IS_log_growth_per_year_at_L=gl,
                        OOS_ann_mu_1x=float(dro.mean() * 365), OOS_ann_vol_1x=float(dro.std() * np.sqrt(365)),
                        kelly_continuous_OOS_ex_post=float(dro.mean() * 365 / (dro.var() * 365)),
                        kelly_per_trade_OOS_ex_post=float(Ls[np.argmax(go)]) if go.max() > 0 else 0.0,
                        OOS_mean_unit_trade=float(uo.mean()), OOS_trades=int(len(uo)))
    print('=== Kelly', scope, json.dumps(kelly[scope], indent=1, default=float))
summary['kelly'] = kelly

# ---- liquidation / worst events of the Selection-A ALL config at 10/15/20x
best = selA['ALL'][0]
ev = []
for L in [10, 15, 20]:
    for win in ['IS', 'OOS']:
        r, days, d, i0, i1 = rerun(best, L, win)
        s = pd.Series(r[0], index=days)
        eq = np.concatenate([[1.0], r[0]])
        dr = pd.Series(eq[1:] / np.where(eq[:-1] > 0, eq[:-1], np.nan) - 1, index=days)
        dead = s[s <= 1e-6]
        ev.append(dict(L=L, win=win, final=float(s.iloc[-1]), liq=int(r[3]), stops=int(r[4]), tps=int(r[5]), trades=int(r[2]),
                       worst_day=str(dr.idxmin().date()), worst_day_ret=float(dr.min()),
                       dead_on=str(dead.index[0].date()) if len(dead) else None))
print(pd.DataFrame(ev).to_string())
summary['selectionA_events'] = ev

# ---- buy & hold context (1x, no leverage, no costs)
bh = {}
for sym in ['BTCUSDT', 'ETHUSDT']:
    d = load(sym); t = d.t.values
    for win, (a0, a1) in {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}.items():
        i0 = int(np.searchsorted(t, ms(a0))); i1 = int(np.searchsorted(t, ms(a1)))
        tot = d.c.values[i1 - 1] / d.o.values[i0]
        days = (i1 - i0) / 288
        bh[f'{sym}_{win}'] = dict(total=float(tot - 1), cagr=float(tot ** (365 / days) - 1))
print('buy&hold 1x', bh); summary['buy_hold_1x'] = bh
summary['grid'] = dict(signals=[f'{a}|{b}|{c}' for a, b, c in SIG_GRID], stops=STOP_KINDS, tp_R=TP_KINDS, sizing=SIZING,
                       per_symbol=len(SIG_GRID) * len(STOP_KINDS) * len(TP_KINDS) * len(SIZING), symbols=2, leverages=LEVS)
json.dump(summary, open(f'{OUT}/summary.json', 'w'), indent=1, default=lambda x: float(x) if isinstance(x, (np.floating, np.integer)) else str(x))
