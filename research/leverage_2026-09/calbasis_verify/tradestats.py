import pandas as pd, numpy as np, multiprocessing as mp
from scipy import stats
from load import load
import cb_v as cb
AS = load()
B = dict(signal='net', h_in=0.04, h_out=-0.02, tau_min=30)
PER = {'IS': ('2022-01-01', '2024-12-31 23:55'), 'OOS': ('2025-01-01', '2026-08-31 23:55'), 'FULL': ('2022-01-01', '2026-08-31 23:55')}
# per-trade unlevered net return (1x), fees included
for per in ['IS', 'OOS']:
    r = []
    for a in ['BTC', 'ETH']:
        s = cb.Sim(AS[a], B, 1, 'cross', start=PER[per][0], end=PER[per][1]).run()
        r += [(a, t['entry'], t['Eend'] / t['E0'] - 1) for t in s.trades]
    d = pd.DataFrame(r, columns=['a', 'entry', 'ret'])
    tt = stats.ttest_1samp(d.ret, 0)
    print(per, 'n', len(d), 'mean %.3f%%' % (d.ret.mean()*100), 'median %.3f%%' % (d.ret.median()*100), 't=%.2f' % tt.statistic, 'win %.0f%%' % ((d.ret > 0).mean()*100))
    if per == 'OOS':
        e = d[~d.entry.str.startswith('2025-01-01')]
        tt = stats.ttest_1samp(e.ret, 0)
        print('OOS excl. the two trades opened at the 2025-01-01 start: n', len(e), 'mean %.3f%%' % (e.ret.mean()*100), 't=%.2f' % tt.statistic)
# combined realistic scenario
TIERS = {'BTC': dict(cb.COST, mmr_fut=0.0065), 'ETH': dict(cb.COST, mmr_fut=0.02)}
def run(tag, p, L, per, slipx=1.0):
    a, b = PER[per]
    sims = [cb.Sim(AS[x], p, L, 'cross', dict(TIERS[x], slip_perp=TIERS[x]['slip_perp']*slipx, slip_fut=TIERS[x]['slip_fut']*slipx), a, b).run() for x in ['BTC', 'ETH']]
    ec, el = cb.equity_series(sims, [.5, .5], a, b)
    m = cb.metrics_from(ec, el)
    out = dict(tag=tag, L=L, per=per, cagr=round(m['cagr']*100, 2), dd=round(m['maxdd']*100, 1), sharpe=round(m['sharpe'], 2), wd=round(m['worst_day']*100, 1),
               liq=sum(s.nliq for s in sims), trades=sum(s.nentry for s in sims), **{'y'+k: round(v*100, 1) for k, v in m['years'].items()})
    if per == 'FULL':
        e0 = ec[:'2024-12-31 23:55'].iloc[-1]; ts0 = pd.Timestamp('2024-12-31 23:55')
        seg = pd.concat([pd.Series([e0], index=[ts0]), ec['2025-01-01':]]); segl = pd.concat([pd.Series([e0], index=[ts0]), el['2025-01-01':]])
        mm = cb.metrics_from(seg, segl)
        out.update(seg_cagr=round(mm['cagr']*100, 2), seg_dd=round(mm['maxdd']*100, 1), seg_sharpe=round(mm['sharpe'], 2), seg_wd=round(mm['worst_day']*100, 1))
    return out
def job(arg): return run(*arg)
if __name__ == '__main__':
    args = []
    for L in [1, 3, 5, 10, 15, 20]:
        for per in PER:
            args.append(('okx_tiers+worstfill+slip1x', dict(B, fill='worst', mark_guard=0.005), L, per, 1.0))
            args.append(('okx_tiers+worstfill+slip2x', dict(B, fill='worst', mark_guard=0.005), L, per, 2.0))
    with mp.Pool(4) as pool:
        res = pool.map(job, args)
    df = pd.DataFrame(res); pd.set_option('display.width', 300)
    print(df.to_string())
    df.to_csv('realistic_combined.csv', index=False)
