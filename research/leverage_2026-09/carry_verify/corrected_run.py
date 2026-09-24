"""Corrected re-run of the always-on BTC+ETH cash-and-carry (same simulator as the researcher, carry_sim.py, with only the
intrabar liquidation check replaced):
  - OKX's own 1m mark (BTC/ETH-USDT-SWAP) and index (BTC/ETH-USDT) in 20 stress windows (+-12h),
  - Binance 1m mark/index everywhere else (the researcher used 1h mark-high/index-high as the proxy),
  - hours with missing 1m data fall back to the researcher's 1h proxy.
Rebalance band re-selected on IS (2022-01-01..2024-12-31) under the corrected model; OOS 2025-01-01..2026-08-31 reported with that band.
Also reported with the researcher's original bands.  Two intrabar variants: minute closes + minute highs ('closes+highs'), minute closes only."""
import sys, json
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry_verify')
import carry_sim_v as cv, pandas as pd, numpy as np
from multiprocessing import Pool
PER = {'IS': ('2022-01-01', '2024-12-31 23:00'), 'OOS': ('2025-01-01', '2026-08-31 23:00'), 'FULL': ('2022-01-01', '2026-08-31 23:00')}
ORIG = {1: 0.2, 3: 0.2, 5: 0.2, 8: 0.2, 10: 0.2, 15: 0.2, 20: 0.05}
LEVS = [1, 3, 5, 8, 10, 15, 20]
BANDS = [0.02, 0.05, 0.10, 0.20]
OKX = None


def build_okx():
    P = cv.load(); idx = P['s_c'].index
    o = pd.concat([pd.read_parquet(f) for f in ['okx_1m_windows.parquet', 'okx_1m_windows_extra.parquet']])
    okx = {}
    for w, g in o.groupby('win'):
        b = g[g.coin == 'BTC'].drop(columns=['coin', 'win']); e = g[g.coin == 'ETH'].drop(columns=['coin', 'win'])
        b = b[~b.index.duplicated()]; e = e[~e.index.duplicated()]
        for t_h in pd.date_range(b.index.min().ceil('h') + pd.Timedelta(hours=1), b.index.max().floor('h') - pd.Timedelta(hours=1), freq='h'):
            mins = pd.date_range(t_h, periods=60, freq='min'); prev = t_h - pd.Timedelta(minutes=1)
            try:
                bb = b.loc[mins]; ee = e.loc[mins]; bp = b.loc[prev]; ep = e.loc[prev]
            except KeyError:
                continue
            if bb.isna().any().any() or ee.isna().any().any():
                continue
            d = {k: np.stack([bb[k].values, ee[k].values], axis=1) for k in ['mk_c', 'mk_h', 'ix_c', 'ix_h']}
            d['mk_prev'] = np.array([bp.mk_c, ep.mk_c]); d['ix_prev'] = np.array([bp.ix_c, ep.ix_c])
            okx[idx.get_loc(t_h)] = d
    return okx


def init():
    global OKX
    cv.load(); cv.load_m1(); OKX = build_okx()


def run(a):
    L, band, per, co = a
    r = cv.sim(['BTCUSDT', 'ETHUSDT'], L, band=band, start=PER[per][0], end=PER[per][1], intrabar='1m', okx=OKX, close_only=co)
    y = r.pop('years')
    return dict(L=L, band=band, per=per, intrabar=('closes' if co else 'closes+highs'), cagr=r['cagr'], maxdd=r['maxdd'], worst_day=r['worst_day'],
                sharpe=r['sharpe'], liq=r['liquidations'], liq_dates=r['liq_dates'], trades=r['trades'], imr_rejects=r['imr_violations'],
                **{f'ret_{k}': v for k, v in y.items()})


if __name__ == '__main__':
    with Pool(4, initializer=init) as p:
        is_jobs = [(L, b, 'IS', co) for co in [False, True] for L in LEVS for b in BANDS]
        g = pd.DataFrame(p.map(run, is_jobs, chunksize=1))
        g.to_csv('corrected_is_grid.csv', index=False)
        sel = {(co, L): float(g[(g.L == L) & (g.intrabar == co)].sort_values('cagr', ascending=False).iloc[0].band)
               for co in ['closes+highs', 'closes'] for L in LEVS}
        jobs = []
        for co in [False, True]:
            lab = 'closes' if co else 'closes+highs'
            for L in LEVS:
                for per in ['IS', 'OOS', 'FULL']:
                    jobs.append((L, sel[(lab, L)], per, co))
                    if sel[(lab, L)] != ORIG[L]:
                        jobs.append((L, ORIG[L], per, co))
        res = pd.DataFrame(p.map(run, jobs, chunksize=1))
    res['band_source'] = [('IS-reselected' if b == sel[(ib, L)] else 'researcher') for b, ib, L in zip(res.band, res.intrabar, res.L)]
    res.to_csv('corrected_results.csv', index=False)
    pd.set_option('display.width', 300)
    for ib in ['closes+highs', 'closes']:
        print('=== intrabar:', ib)
        piv = []
        for L in LEVS:
            for bs in ['IS-reselected', 'researcher']:
                r = res[(res.L == L) & (res.intrabar == ib) & (res.band_source == bs)]
                if bs == 'researcher' and len(r) == 0:
                    r = res[(res.L == L) & (res.intrabar == ib)]; bs = 'both'
                if len(r) == 0:
                    continue
                i, o, f = [r[r.per == x].iloc[0] for x in ['IS', 'OOS', 'FULL']]
                piv.append(dict(L=L, band=i.band, src=bs, cagr_is=round(i.cagr, 4), cagr_oos=round(o.cagr, 4), maxdd_is=round(i.maxdd, 3), maxdd_oos=round(o.maxdd, 3),
                                wd_oos=round(o.worst_day, 4), sharpe_oos=round(o.sharpe, 2), liq_is=i.liq, liq_oos=o.liq, liq_full=f.liq, liq_dates=f.liq_dates,
                                trades_oos=o.trades, **{f'y{k}': round(f[f'ret_{k}'], 3) for k in range(2022, 2027)}))
        print(pd.DataFrame(piv).to_string())
    json.dump({f'{k[0]}|{k[1]}': v for k, v in sel.items()}, open('corrected_selected_bands.json', 'w'), indent=1)
