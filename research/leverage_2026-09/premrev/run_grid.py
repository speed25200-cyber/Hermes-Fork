"""Full grid: trade generation per coin/config, K=1 portfolio (one open pair at a time across coins, notional
per leg = L x equity) at leverages 1..20 under three margin models, IS 2022-01-01..2024-12-31 and OOS
2025-01-01..2026-08-31 simulated separately (each starts at equity 1).

Usage: python run_grid.py [--stress 1] [--kappa 0.05] [--tag name] [--execs taker,maker] [coins...]
Output: out/grid_results[_tag].csv (one row per exec x config x segment x margin x L)
"""
import sys, os, time, itertools, argparse
import numpy as np, pandas as pd
from numba import njit
from common import load, funding, minute_of, NMIN, T0
import engine as E

OUTDIR = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev/out'
os.makedirs(OUTDIR, exist_ok=True)
USDT_RATE = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/data/okx_usdt_lending_rate_hourly.csv'

# OKX public API (fetched 2026-09-24): swap tier-1 MMR/IMR, discount-rate haircut, liquidation penalty,
# cross-margin borrow MMR (tier 1), isolated spot-margin MMR (tier 1). m_pm: ASSUMED portfolio-margin
# maintenance ratio for a hedged pair (perp MMR + 1% basis charge BTC/ETH, + 2% alts) - not a published value.
PARAMS = {
    'BTCUSDT':  dict(slip=1e-4, haircut=.02, mm=.004, im=.01, lp=.02, bmmr=.02, cbmmr=.02, mmr_m=.02, m_pm=.014),
    'ETHUSDT':  dict(slip=1e-4, haircut=.02, mm=.004, im=.01, lp=.02, bmmr=.02, cbmmr=.02, mmr_m=.02, m_pm=.014),
    'SOLUSDT':  dict(slip=3e-4, haircut=.03, mm=.004, im=.01, lp=.02, bmmr=.02, cbmmr=.02, mmr_m=.02, m_pm=.024),
    'XRPUSDT':  dict(slip=3e-4, haircut=.03, mm=.004, im=.01, lp=.03, bmmr=.02, cbmmr=.02, mmr_m=.02, m_pm=.024),
    'DOGEUSDT': dict(slip=3e-4, haircut=.03, mm=.010, im=.02, lp=.03, bmmr=.02, cbmmr=.03, mmr_m=.03, m_pm=.030),
}
FEES = dict(fs_t=0.0010, fp_t=0.0005, fs_m=0.0008, fp_m=0.0002)
R_COIN = 0.20                 # coin borrow for the short-spot leg, %/yr (OKX basic rates 0.5-4% in 2026-09)
T_FILL, BUF = 2, 1e-4

IS_A, IS_B = minute_of('2022-01-01'), minute_of('2025-01-01')
OOS_A, OOS_B = minute_of('2025-01-01'), NMIN
IS_YEARS = 3.0
OOS_YEARS = (pd.Timestamp('2026-09-01') - pd.Timestamp('2025-01-01')).days / 365.25

UNIVERSES = {'all5': ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT'], 'btceth': ['BTCUSDT', 'ETHUSDT']}
GRID = dict(W=[240, 1440], k=[15, 20, 30, 40, 60, 80], x=[0.0, 0.5], H=[30, 120, 480], both=[0, 1], confirm=[0, 1])
EXECS = [('taker', 0, 0, 1), ('taker', 1, 0, 1), ('maker', 0, 1, 1), ('maker_upper', 0, 3, 1)]   # (name, lat, mode, hedge_mid)


def prep(sym):
    X = load(sym)
    ok = X['s_ok'] & X['f_ok'] & np.isfinite(X['b_c'])
    bsig = np.where(ok, X['b_c'], np.nan)
    ff = lambda a: pd.Series(a).ffill().bfill().values
    bc = ff(np.where(ok, X['b_c'], np.nan))
    Sc = ff(X['s_c'])
    z = lambda a: np.nan_to_num(a, nan=0.0)
    fh, fl = z(X['f_h']), z(X['f_l'])
    P = dict(ok=ok, bc=bc, Sc=Sc, so=z(X['s_o']), bo=np.where(np.isfinite(X['b_o']), X['b_o'], bc),
             sh=z(X['s_h']), sl=z(X['s_l']), fh=fh, fl=fl,
             mpc=np.where(np.isfinite(X['mp_c']), X['mp_c'], bc), ph=z(X['p_h']), pl=z(X['p_l']),
             mh=np.where(np.isfinite(X['m_h']), X['m_h'], (1 + bc) * (1 + fh) - 1),
             ml=np.where(np.isfinite(X['m_l']), X['m_l'], (1 + bc) * (1 + fl) - 1),
             ic=z(X['i_c']))
    P['fund'], nf = funding(sym)
    r = pd.read_csv(USDT_RATE, parse_dates=['ts'])
    r = r.drop_duplicates('ts').set_index('ts')['rate']
    idx = T0 + pd.to_timedelta(np.arange(0, NMIN, 60), unit='min')
    rh = r.reindex(idx, method='ffill').bfill().values
    P['rusdt'] = np.repeat(rh, 60)[:NMIN]
    P['med'] = {}; P['pdev'] = {}
    pc = pd.Series(X['p_c'])
    for W in GRID['W']:
        P['med'][W] = pd.Series(bsig).shift(1).rolling(W, min_periods=W // 2).median().values
        P['pdev'][W] = np.nan_to_num((pc - pc.shift(1).rolling(W, min_periods=W // 2).median()).values, nan=0.0)
    return P


def trades_for(P, sym, cfg, mode, lat, stress=0, kappa=0.05, fees=FEES, t_fill=T_FILL, buf=BUF, a=None, b=None, hedge_mid=1):
    pr = PARAMS[sym]
    W, k, x, H, both, confirm = cfg
    return E.gen_trades(IS_A if a is None else a, OOS_B if b is None else b, P['ok'], P['bc'], P['bo'], P['Sc'],
                        P['so'], P['sh'], P['sl'], P['fh'], P['fl'],
                        P['mpc'], P['ph'], P['pl'], P['mh'], P['ml'], P['ic'], P['fund'], P['rusdt'],
                        P['med'][W], P['pdev'][W], k * 1e-4, x, H, both, confirm, mode, lat,
                        t_fill, buf, fees['fs_t'], fees['fp_t'], fees['fs_m'], fees['fp_m'], pr['slip'], kappa,
                        pr['haircut'], pr['mm'], pr['im'], pr['lp'], pr['bmmr'], pr['cbmmr'], pr['mmr_m'], pr['m_pm'],
                        R_COIN, E.LEVS, stress, hedge_mid)


@njit(cache=True)
def port_sim(sig, e, x, ret, worst, liq, rej, a, b, day0, ndays):
    """K=1 portfolio over trades sorted by entry: a trade is taken if the previous taken one has exited and it
    is not rejected (initial margin) at this leverage. Daily equity books each trade's P&L on its exit day.
    Returns (NL, 7): final, maxdd(intrabar), liquidations, trades, sharpe(daily, sqrt 365), worst day, rejected."""
    ntr, nl = ret.shape
    out = np.zeros((nl, 7))
    daily = np.ones((nl, ndays + 1))
    for li in range(nl):
        Eq = 1.0; peak = 1.0; mdd = 0.0; nliq = 0; ntk = 0; nrej = 0
        last_x = -1
        dayeq = np.ones(ndays + 1)
        cur_day = 0
        for i in range(ntr):
            if sig[i] < a or sig[i] >= b:
                continue
            if e[i] <= last_x:
                continue
            if rej[i, li] > 0:
                nrej += 1
                continue
            if Eq <= 1e-9:
                break
            ntk += 1
            w = Eq * max(0.0, 1.0 + worst[i, li])      # account equity cannot go below zero
            if w / peak - 1.0 < mdd:
                mdd = w / peak - 1.0
            Eq = Eq * (1.0 + ret[i, li])
            if Eq < 0:
                Eq = 0.0
            if liq[i, li] > 0:
                nliq += 1
            if Eq / peak - 1.0 < mdd:
                mdd = Eq / peak - 1.0
            if Eq > peak:
                peak = Eq
            last_x = x[i]
            dd = int(x[i] // 1440) - day0
            if dd > ndays:
                dd = ndays
            for j in range(cur_day + 1, dd):
                dayeq[j] = dayeq[cur_day]
            if dd > cur_day:
                cur_day = dd
            dayeq[cur_day] = Eq
        for j in range(cur_day + 1, ndays + 1):
            dayeq[j] = dayeq[cur_day]
        daily[li] = dayeq
        r = dayeq[1:] / np.maximum(dayeq[:-1], 1e-300) - 1.0
        sd = r.std()
        out[li, 0] = Eq; out[li, 1] = mdd; out[li, 2] = nliq; out[li, 3] = ntk
        out[li, 4] = r.mean() / sd * np.sqrt(365.0) if sd > 0 else 0.0
        out[li, 5] = r.min(); out[li, 6] = nrej
    return out, daily


def merge(trs):
    allt = np.concatenate([t for t in trs if len(t)]) if any(len(t) for t in trs) else np.zeros((0, E.NCOL))
    if len(allt):
        allt = allt[np.lexsort((allt[:, E.C_SIG], allt[:, E.C_E]))]
    return allt


def portfolio(allt, a, b, margins=('mc', 'pm', 'sep')):
    day0 = a // 1440
    ndays = (b // 1440) - day0
    res = {}
    for m in margins:
        cr, cw, cl, cj = E.col(m, 'ret'), E.col(m, 'worst'), E.col(m, 'liq'), E.col(m, 'rej')
        res[m] = port_sim(allt[:, E.C_SIG].copy(), allt[:, E.C_E].copy(), allt[:, E.C_X].copy(),
                          np.ascontiguousarray(allt[:, cr:cr + E.NL]), np.ascontiguousarray(allt[:, cw:cw + E.NL]),
                          np.ascontiguousarray(allt[:, cl:cl + E.NL]), np.ascontiguousarray(allt[:, cj:cj + E.NL]),
                          a, b, day0, ndays)
    return res


def year_returns(daily, a):
    d0 = T0 + pd.Timedelta(days=int(a // 1440))
    s = pd.Series(daily, index=pd.date_range(d0, periods=len(daily), freq='D'))
    out = {}
    for y in sorted(set(s.index.year)):
        sy = s[s.index.year == y]
        prev = s[s.index < sy.index[0]]
        start = prev.iloc[-1] if len(prev) else s.iloc[0]
        out[y] = sy.iloc[-1] / start - 1 if start > 0 else -1.0
    return out


def summarize(allt, a, b, yrs, base):
    rows = []
    sel = (allt[:, E.C_SIG] >= a) & (allt[:, E.C_SIG] < b) if len(allt) else np.zeros(0, bool)
    net1 = (allt[sel, E.C_GROSS] - allt[sel, E.C_FEES] - allt[sel, E.C_SLIP] + allt[sel, E.C_FUND]) if len(allt) else np.zeros(0)
    edge_bp = net1.mean() * 1e4 if len(net1) else np.nan
    edge_t = net1.mean() / net1.std() * np.sqrt(len(net1)) if len(net1) > 2 and net1.std() > 0 else np.nan
    gross_bp = allt[sel, E.C_GROSS].mean() * 1e4 if len(net1) else np.nan
    res = portfolio(allt, a, b)
    for m, (met, daily) in res.items():
        for li, L in enumerate(E.LEVS):
            fin, mdd, nliq, ntk, sh, wd, nrej = met[li]
            yr = year_returns(daily[li], a)
            rows.append(dict(base, margin=m, L=L, final=fin, cagr=(fin ** (1 / yrs) - 1) if fin > 0 else -1.0,
                             maxdd=mdd, liqs=int(nliq), trades=int(ntk), rejected=int(nrej), sharpe=sh, worst_day=wd,
                             years=';'.join(f'{y}:{v:+.4f}' for y, v in yr.items()),
                             n_all=int(sel.sum()), edge_bp=edge_bp, edge_t=edge_t, gross_bp=gross_bp))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stress', type=int, default=0)
    ap.add_argument('--kappa', type=float, default=0.05)
    ap.add_argument('--tag', default='')
    ap.add_argument('--execs', default='')
    ap.add_argument('coins', nargs='*')
    A = ap.parse_args()
    coins = A.coins or ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT']
    execs = [x for x in EXECS if not A.execs or x[0] in A.execs.split(',')]
    t0 = time.time()
    Ps = {c: prep(c) for c in coins}
    print('prep done', round(time.time() - t0), 's', flush=True)
    cfgs = list(itertools.product(GRID['W'], GRID['k'], GRID['x'], GRID['H'], GRID['both'], GRID['confirm']))
    rows = []
    for ex, lat, mode, hmid in execs:
        for cfg in cfgs:
            trd = {c: trades_for(Ps[c], c, cfg, mode, lat, stress=A.stress, kappa=A.kappa, hedge_mid=hmid) for c in coins}
            for un, uc in UNIVERSES.items():
                if not all(c in trd for c in uc):
                    continue
                allt = merge([trd[c] for c in uc])
                base = dict(exec=ex, lat=lat, univ=un, W=cfg[0], k=cfg[1], x=cfg[2], H=cfg[3], both=cfg[4], confirm=cfg[5])
                rows += summarize(allt, IS_A, IS_B, IS_YEARS, dict(base, seg='IS'))
                rows += summarize(allt, OOS_A, OOS_B, OOS_YEARS, dict(base, seg='OOS'))
        print(ex, lat, 'done', round(time.time() - t0), 's', flush=True)
    df = pd.DataFrame(rows)
    fn = f'{OUTDIR}/grid_results{"_" + A.tag if A.tag else ""}.csv.gz'
    df.to_csv(fn, index=False, float_format='%.6g')
    print('saved', fn, len(df), 'rows', round(time.time() - t0), 's')


if __name__ == '__main__':
    main()
