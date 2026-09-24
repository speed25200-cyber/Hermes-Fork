"""Adversarial verification of the calendar-basis (short Binance USDT-M quarterly / long perp) result, config short_B.
Engine: cb_v.py = researcher's cb_backtest.py + options (execution delay, fill price mode, min contract age, headroom log).
This script produces the corrected table: quarterly marks cleaned ONLY in the first 24h after listing (mark replaced by last price
when |mark/last-1| > 50 bp; the archived mark of BTC/ETH 251226 sat ~260 bp below the traded price for ~8h after listing),
for three margin scenarios
(researcher's flat 0.5% future MMR, OKX live tiers inverse: BTC 0.65% / ETH 2.0%) and three reporting modes
(IS run, OOS run starting flat 2025-01-01, continuous 2022-2026 run with its 2025-01..2026-08 segment)."""
import pandas as pd, numpy as np, json, copy, multiprocessing as mp
from load import load
import cb_v as cb
AS0 = load()
AS = copy.deepcopy(AS0)
for a in AS:
    for c in AS[a]['cons']:
        bad = (np.abs(c['Fmc'] / c['F'] - 1) > 0.005) & (np.arange(len(c['F'])) < 288)   # listing-window artefact only (first 24h)
        for k in ('Fmo', 'Fmh', 'Fml', 'Fmc'):
            c[k] = np.where(bad, c['F'], c[k])
B = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
PER = {'IS': ('2022-01-01', '2024-12-31 23:55'), 'OOS': ('2025-01-01', '2026-08-31 23:55'), 'FULL': ('2022-01-01', '2026-08-31 23:55')}
SC = {'researcher_costs_mmr0.5': {'BTC': dict(cb.COST), 'ETH': dict(cb.COST)},
      'okx_tiers': {'BTC': dict(cb.COST, mmr_fut=0.0065), 'ETH': dict(cb.COST, mmr_fut=0.02)},
      'okx_tiers_worstfill_slip2x': {x: dict(cb.COST, mmr_fut=m, slip_perp=0.0002, slip_fut=0.0006) for x, m in (('BTC', 0.0065), ('ETH', 0.02))}}
def job(arg):
    sc, L, per = arg
    p = dict(B, fill='worst', mark_guard=0.005) if 'worstfill' in sc else B
    a, b = PER[per]
    sims = [cb.Sim(AS[x], p, L, 'cross', SC[sc][x], a, b).run() for x in ['BTC', 'ETH']]
    ec, el = cb.equity_series(sims, [.5, .5], a, b)
    m = cb.metrics_from(ec, el)
    r = dict(scenario=sc, L=L, period=per, cagr=m['cagr'], maxdd_intrabar=m['maxdd'], sharpe=m['sharpe'], worst_day=m['worst_day'],
             liquidations=sum(s.nliq for s in sims), trades=sum(s.nentry for s in sims), **{'y' + k: v for k, v in m['years'].items()})
    for s, nm in zip(sims, ['BTC', 'ETH']):
        e1, l1 = cb.equity_series([s], [1.0], a, b); mm = cb.metrics_from(e1, l1)
        r[nm + '_cagr'] = mm['cagr']; r[nm + '_maxdd'] = mm['maxdd']
    if per == 'FULL':
        ts0 = pd.Timestamp('2024-12-31 23:55'); e0 = ec[:ts0].iloc[-1]
        seg = pd.concat([pd.Series([e0], index=[ts0]), ec['2025-01-01':]]); segl = pd.concat([pd.Series([e0], index=[ts0]), el['2025-01-01':]])
        mm = cb.metrics_from(seg, segl)
        r.update(seg_oos_cagr=mm['cagr'], seg_oos_maxdd=mm['maxdd'], seg_oos_sharpe=mm['sharpe'], seg_oos_worst_day=mm['worst_day'],
                 seg_oos_liq=sum(1 for s in sims for t in s.trades if t['why'] == 'LIQ' and t['exit'] >= '2025'))
    return r
if __name__ == '__main__':
    args = [(sc, L, per) for sc in SC for L in [1, 3, 5, 10, 15, 20] for per in PER]
    with mp.Pool(4) as pool:
        res = pool.map(job, args)
    df = pd.DataFrame(res)
    df.to_csv('verify_results.csv', index=False)
    pct = lambda v: None if pd.isna(v) else round(float(v) * 100, 2)
    rows = []
    for (sc, L), d in df.groupby(['scenario', 'L'], sort=False):
        i = d[d.period == 'IS'].iloc[0]; o = d[d.period == 'OOS'].iloc[0]; f = d[d.period == 'FULL'].iloc[0]
        rows.append(dict(scenario=sc, L=L, cagr_IS=pct(i.cagr), maxdd_IS=pct(i.maxdd_intrabar), worstday_IS=pct(i.worst_day), liq_IS=int(i.liquidations),
                         cagr_OOS_flatstart=pct(o.cagr), maxdd_OOS=pct(o.maxdd_intrabar), sharpe_OOS=round(o.sharpe, 2), worstday_OOS=pct(o.worst_day),
                         liq_OOS=int(o.liquidations), ETH_maxdd_OOS=pct(o.ETH_maxdd), cagr_OOS_continuous=pct(f.seg_oos_cagr), maxdd_OOS_continuous=pct(f.seg_oos_maxdd),
                         liq_OOS_continuous=int(f.seg_oos_liq), trades_IS=int(i.trades), trades_OOS=int(o.trades),
                         **{f'y{y}_full': pct(f.get(f'y{y}')) for y in range(2022, 2027)}))
    s = pd.DataFrame(rows); pd.set_option('display.width', 400)
    print(s.to_string())
    s.to_csv('verify_summary.csv', index=False)
