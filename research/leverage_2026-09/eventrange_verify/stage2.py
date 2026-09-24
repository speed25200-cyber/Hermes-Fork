"""STAGE 2 (post-hoc, NOT a clean out-of-sample test): the stage-1 event study (event_study.py) printed IS and OOS
forward returns together and showed that buying 5-minute >=10-sigma dumps earns +45..80 bp gross over 15-240 min
when no tight stop is used, while every stage-1 wick config (tight stops 0.5-1x the move) lost. This stage tests the
obvious follow-up the IS event study alone would suggest: long-only dip-buy of 5-min >=10 sigma dumps, market entry
at the next open, no take-profit, exit after H minutes, stop either only the protective stop at 75% of the
liquidation distance ('liqcap') or at 2x the move. 12 configs, selection on IS only, same costs and rules as stage 1.
Because the design was informed by a table that also showed OOS numbers, its OOS result is optimistic by construction."""
import sys, os, json, itertools, time
sys.argv = ['run.py', 'main']
import numpy as np, pandas as pd
import run
from multiprocessing import Pool
CFGS = [dict(w=5, z=10, V=V, entry='mkt', tp=1e6, stop=s, hold=H, side='long')
        for V, H, s in itertools.product((3, 10), (15, 60, 240), (1e6, 2.0))]
if __name__ == '__main__':
    with Pool(4) as pool:
        res = pool.map(run.job, [('wick', 1000 + i, c) for i, c in enumerate(CFGS)])
    df = pd.DataFrame([r for rr in res for r in rr])
    df.to_csv(os.path.join(run.OUT, 'stage2_dipbuy.csv'), index=False)
    pd.set_option('display.width', 250); pd.set_option('display.max_colwidth', 100)
    p = df.pivot_table(index=['cfg_id', 'L'], columns='period', values=['cagr', 'maxdd', 'liq', 'sharpe', 'trades', 'mean_trade', 'worst_day']).reset_index()
    p.columns = ['_'.join([c for c in col if c]) for col in p.columns]
    cfg = df.drop_duplicates('cfg_id').set_index('cfg_id')['cfg']
    p['cfg'] = p.cfg_id.map(lambda i: json.loads(cfg[i])); p['cfg'] = p.cfg.map(lambda c: f"V{c['V']} H{c['hold']} stop{'liqcap' if c['stop'] > 100 else c['stop']}")
    print(p[['cfg', 'L', 'cagr_IS', 'cagr_OOS', 'maxdd_IS', 'maxdd_OOS', 'worst_day_OOS', 'sharpe_IS', 'sharpe_OOS', 'liq_IS', 'liq_OOS', 'trades_IS', 'trades_OOS', 'mean_trade_IS', 'mean_trade_OOS']].round(4).to_string())
