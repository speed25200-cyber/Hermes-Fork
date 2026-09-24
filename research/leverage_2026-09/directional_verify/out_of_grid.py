"""
Out-of-grid probes (verifier), to test whether the negative verdict is an artefact of the researcher's grid:
 (1) daily-bar trend following (not in the grid), a-priori parameter set, selection on IS only
 (2) BTC+ETH 50/50 two-sub-account portfolio of every per-symbol config (diversification), selection on IS only
Uses the researcher's numba run_one/run_many, which the verifier matched bit-for-bit with an independent
pure-python simulator on raw zips (indep_sim.py).
"""
import numpy as np, pandas as pd, json, sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/directional_verify')
from bt_copy import *   # noqa  (run_one, run_many, signals, tf_frame, to_grid, to_grid_ffill, vol_mult, SIG_GRID, ...)

VOUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/directional_verify/out'
LEVS = [1, 3, 5, 10, 15, 20]


def daily_signals(d, fam, p):
    m = 288
    b = tf_frame(d, m); c = b.c; n5 = len(d)
    if fam == 'ema':
        f, s = p
        diff = c.ewm(span=f, adjust=False).mean() - c.ewm(span=s, adjust=False).mean()
        up = diff > 0; dn = diff < 0
        evL = up & ~up.shift(1, fill_value=False); evS = dn & ~dn.shift(1, fill_value=False); exL = dn; exS = up
    else:
        N = p; N2 = N // 2
        hiN = b.h.rolling(N).max().shift(1); loN = b.l.rolling(N).min().shift(1)
        hi2 = b.h.rolling(N2).max().shift(1); lo2 = b.l.rolling(N2).min().shift(1)
        evL = c > hiN; evS = c < loN; exL = c < lo2; exS = c > hi2
    tr = pd.concat([b.h - b.l, (b.h - c.shift()).abs(), (b.l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean() / c
    g = lambda x: to_grid(x.fillna(False).values.astype(np.int8), m, n5)
    return g(evL), g(evS), g(exL), g(exS), to_grid_ffill(atr.values, m, n5)


def prep(sym):
    d = load(sym); t = d.t.values
    day = ((t - t[0]) // 86400000).astype(np.int64)
    arrs = [d[k].values.astype(np.float64) for k in ['o', 'h', 'l', 'c', 'mo', 'mh', 'ml', 'fund']]
    ff = d.fund_flag.values.astype(np.int8)
    vm, _ = vol_mult(d, ms(IS0), ms(IS1))
    return d, t, day, arrs, ff, vm


def window(t, day, win):
    a0, a1 = (IS0, IS1) if win == 'IS' else (OOS0, OOS1)
    i0 = int(np.searchsorted(t, ms(a0))); i1 = int(np.searchsorted(t, ms(a1)))
    d0 = int(day[i0]); nd = int(day[i1 - 1] - d0 + 1)
    days = pd.DatetimeIndex(pd.to_datetime(t[0], unit='ms') + pd.to_timedelta(np.arange(d0, d0 + nd), unit='D'))
    return i0, i1, d0, nd, days


def met(eq_daily, days):
    eq = np.concatenate([[1.0], eq_daily])
    r = pd.Series(eq[1:] / np.where(eq[:-1] > 0, eq[:-1], np.nan) - 1).fillna(0.0)
    fin = eq_daily[-1]; nd = len(eq_daily)
    cagr = fin ** (365 / nd) - 1 if fin > 0 else -1.0
    sh = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else 0.0
    pk = np.maximum.accumulate(eq); mdd = float(np.max(1 - eq / pk))   # close-of-day DD (portfolio); intrabar DD is per leg
    s = pd.Series(eq_daily, index=days); yrs = {}; prev = 1.0
    for y in sorted(set(days.year)):
        last = s[s.index.year == y].iloc[-1]; yrs[f'y{y}'] = (last / prev - 1) if prev > 0 else -1.0; prev = last
    return dict(cagr=cagr, sharpe=sh, final=fin, maxdd_daily=mdd, worst_day=float(r.min()), **yrs)


def run_cfgs(sym, cfgs):
    """cfgs: list of dict(name, evL, evS, exL, exS, stopf, use_vol, tp, mh). Returns {win: (daily[R,L], stats, days)}."""
    d, t, day, arrs, ff, vm = prep(sym)
    out = {}
    for win in ['IS', 'OOS']:
        i0, i1, d0, nd, days = window(t, day, win)
        res = {}
        for c in cfgs:
            for L in LEVS:
                r = run_one(*arrs, ff, day, i0, i1, d0, nd, c['evL'], c['evS'], c['exL'], c['exS'], c['stopf'], vm,
                            c['use_vol'], float(L), c['tp'], c['mh'], True, False)
                res[(c['name'], L)] = (r[0], r[1], r[2], r[3])
        out[win] = (res, days)
    return out


def part1_daily():
    rows = []
    for sym in ['BTCUSDT', 'ETHUSDT']:
        d = load(sym); n5 = len(d)
        cfgs = []
        for fam, p in [('ema', (10, 50)), ('ema', (20, 100)), ('donch', 20), ('donch', 55)]:
            a, b_, c_, d_, atr = daily_signals(d, fam, p)
            for sk in ['atr3x', 'none']:
                stopf = np.nan_to_num(3 * atr, nan=0.05) if sk == 'atr3x' else np.full(n5, 10.0)  # 'none' => capped at 75% of liq distance
                for tp in [0.0]:
                    for uv in [False, True]:
                        cfgs.append(dict(name=f'{sym}|1d|{fam}|{p}|{sk}|tp{tp}|{"vol" if uv else "fixed"}', evL=a, evS=b_, exL=c_, exS=d_,
                                         stopf=stopf, use_vol=uv, tp=tp, mh=0))
        out = run_cfgs(sym, cfgs)
        for c in cfgs:
            for L in LEVS:
                row = dict(config=c['name'], sym=sym, L=L)
                for win in ['IS', 'OOS']:
                    res, days = out[win]
                    daily, mdd, ntr, nliq = res[(c['name'], L)]
                    m = met(daily, days)
                    row.update({f'{win}_{k}': v for k, v in m.items() if not k.startswith('y')})
                    row.update({k: v for k, v in m.items() if k.startswith('y')})
                    row[f'{win}_maxdd_intrabar'] = mdd; row[f'{win}_trades'] = ntr; row[f'{win}_liq'] = nliq
                rows.append(row)
    R = pd.DataFrame(rows)
    R.to_csv(f'{VOUT}/oog_daily_trend.csv', index=False)
    return R


def part2_portfolio():
    """50/50 BTC+ETH, two sub-accounts (no rebalancing), each run at leverage L on its half => portfolio notional/equity <= L."""
    per = {}
    for sym in ['BTCUSDT', 'ETHUSDT']:
        d = load(sym); n5 = len(d)
        cfgs = []
        ATRS = {}
        sigs = []
        for si, (tf, fam, p) in enumerate(SIG_GRID):
            a, b_, c_, d_, atr, mh = signals(d, tf, fam, p)
            ATRS[tf] = atr; sigs.append((tf, fam, p, a, b_, c_, d_, atr, mh))
        for (tf, fam, p, a, b_, c_, d_, atr, mh) in sigs:
            for sk in STOP_KINDS:
                stopf = np.full(n5, 0.015) if sk == 'fix1.5%' else np.nan_to_num(2 * atr, nan=0.015)
                for tp in TP_KINDS:
                    for sz in SIZING:
                        cfgs.append(dict(name=f'{tf}|{fam}|{p}|{sk}|tp{tp}|{sz}', evL=a, evS=b_, exL=c_, exS=d_, stopf=stopf,
                                         use_vol=(sz == 'volscaled'), tp=tp, mh=mh))
        per[sym] = (run_cfgs(sym, cfgs), [c['name'] for c in cfgs])
    names = per['BTCUSDT'][1]
    rows = []
    for nm in names:
        for L in LEVS:
            row = dict(config='BTC+ETH|' + nm, L=L)
            for win in ['IS', 'OOS']:
                resB, days = per['BTCUSDT'][0][win]; resE, _ = per['ETHUSDT'][0][win]
                eq = 0.5 * resB[(nm, L)][0] + 0.5 * resE[(nm, L)][0]
                m = met(eq, days)
                row.update({f'{win}_{k}': v for k, v in m.items() if not k.startswith('y')})
                row.update({k: v for k, v in m.items() if k.startswith('y')})
                row[f'{win}_liq'] = resB[(nm, L)][3] + resE[(nm, L)][3]
                row[f'{win}_trades'] = resB[(nm, L)][2] + resE[(nm, L)][2]
            rows.append(row)
    R = pd.DataFrame(rows)
    R.to_csv(f'{VOUT}/oog_btc_eth_portfolio.csv', index=False)
    return R


def summarize(R, label):
    print(f'===== {label}: {R.config.nunique()} configs')
    g = R.groupby('L').agg(IS_prof=('IS_cagr', lambda x: int((x > 0).sum())), OOS_prof=('OOS_cagr', lambda x: int((x > 0).sum())),
                           both=('OOS_cagr', lambda x: int(((x > 0) & (R.loc[x.index, 'IS_cagr'] > 0)).sum())),
                           OOS_med=('OOS_cagr', 'median'), OOS_best=('OOS_cagr', 'max'),
                           OOS_liq_frac=('OOS_liq', lambda x: (x > 0).mean()))
    print(g.round(3).to_string())
    out = {}
    bestA = R[R.L == 1].sort_values('IS_sharpe', ascending=False).iloc[0].config
    T = R[R.config == bestA].sort_values('L')
    print('Selection A (best IS Sharpe @1x):', bestA)
    print(T[['L', 'IS_cagr', 'IS_sharpe', 'OOS_cagr', 'OOS_sharpe', 'OOS_maxdd_daily', 'OOS_worst_day', 'OOS_liq', 'OOS_trades', 'y2025', 'y2026']].round(3).to_string(index=False))
    out['A'] = dict(config=bestA, table=T.round(4).to_dict(orient='records'))
    rowsB = []
    for L in LEVS:
        r = R[R.L == L].sort_values('IS_cagr', ascending=False).iloc[0]
        rowsB.append(dict(L=L, config=r.config, IS_cagr=r.IS_cagr, OOS_cagr=r.OOS_cagr, OOS_maxdd_daily=r.OOS_maxdd_daily, OOS_liq=r.OOS_liq))
    B = pd.DataFrame(rowsB); print('Selection B (best IS CAGR at each L):'); print(B.round(3).to_string(index=False))
    out['B'] = B.round(4).to_dict(orient='records'); out['dist'] = g.round(4).reset_index().to_dict(orient='records')
    return out


if __name__ == '__main__':
    summ = {}
    R1 = part1_daily(); summ['daily_trend'] = summarize(R1, 'Daily-bar trend following (out of grid)')
    R2 = part2_portfolio(); summ['btc_eth_5050'] = summarize(R2, 'BTC+ETH 50/50 portfolio of the 144 per-symbol configs')
    json.dump(summ, open(f'{VOUT}/oog_summary.json', 'w'), indent=1, default=lambda x: float(x) if isinstance(x, (np.floating, np.integer)) else str(x))
