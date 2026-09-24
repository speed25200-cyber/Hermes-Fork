"""Diagnostics of the slow family (iii) for the configs chosen on IS (no re-tuning):
P&L concentration by coin, win rate, sensitivity to OKX-level funding (x0.8, measured on OKX-listed coins in
okx_vs_binance.py), doubled slippage, and mark-price liquidation. -> diag_slow.csv"""
import os, sys, json
import numpy as np, pandas as pd
import fe_slow as fs

HERE = os.path.dirname(os.path.abspath(__file__))
fs.load()
G = fs.G
CFGS = [tuple(json.loads(a)) for a in sys.argv[1:]] or [[2.0, 0.0, 10, 'btc', 'both', 'all']]
rows = []
for cfg in CFGS:
    cfg = tuple(cfg)
    for L in [1, 3, 5]:
        for variant in ['base', 'funding_x0.8', 'mark_liq', 'slip_x2']:
            G['fund_mult'] = 0.8 if variant == 'funding_x0.8' else 1.0
            G['use_mark'] = variant == 'mark_liq'
            if variant == 'slip_x2':
                _sc = fs.slip_coin
                fs.slip_coin = lambda i, h, _f=_sc: 2 * _f(i, h)
            for per, (t0, t1) in (('IS', (fs.IS0, fs.IS1)), ('OOS', (fs.IS1, fs.OOS1))):
                G['log_trades'] = []
                eqc, eqt, ntr, nliq, fsum, csum = fs.simulate(cfg, L, t0, t1)
                cagr, dd, wd, sh, fin, yrs = fs.metrics(eqc, eqt, t0, t1)
                tr = pd.DataFrame(G['log_trades'], columns=['h0', 'h1', 'sym', 's', 'm0', 'mend'])
                tr['pnl'] = tr.mend - tr.m0
                by = tr.groupby('sym').pnl.sum().sort_values()
                rows.append({'config': json.dumps(cfg), 'L': L, 'variant': variant, 'period': per, 'cagr': cagr, 'maxdd': dd,
                             'worst_day': wd, 'sharpe': sh, 'trades': ntr, 'liq': nliq,
                             'win_rate': float((tr.pnl > 0).mean()) if len(tr) else np.nan,
                             'pnl_total': float(tr.pnl.sum()), 'pnl_top5_coins': float(by.tail(5).sum()),
                             'pnl_worst5_coins': float(by.head(5).sum()),
                             'top5': ','.join(f'{k}:{v:.2f}' for k, v in by.tail(5).items()),
                             'worst5': ','.join(f'{k}:{v:.2f}' for k, v in by.head(5).items()),
                             'mean_hold_h': float((tr.h1 - tr.h0).mean()) if len(tr) else np.nan, 'years': json.dumps(yrs)})
            if variant == 'slip_x2':
                fs.slip_coin = _sc
            print(rows[-2]['config'], L, variant, 'IS %.3f OOS %.3f  ddOOS %.3f liqOOS %d' % (rows[-2]['cagr'], rows[-1]['cagr'], rows[-1]['maxdd'], rows[-1]['liq']), flush=True)
df = pd.DataFrame(rows)
df.to_csv(os.path.join(HERE, 'diag_slow.csv'), index=False)
pd.set_option('display.width', 250); pd.set_option('display.max_colwidth', 80)
print(df.drop(columns=['years', 'config']).round(3).to_string())
