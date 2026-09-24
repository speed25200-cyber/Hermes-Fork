"""Verifier step 3: stress variants of the IS-selected slow config (no re-tuning), every leverage.
base | okx universe | funding x0.8 | okx + funding x0.8 | entry 1h later | exit 1h later | both later |
excl. top-1 / top-5 OOS coins | excl. top-5 IS coins | mark-price liquidation | close-only liquidation (optimistic bound)."""
import json, sys, os, time
import numpy as np, pandas as pd
from multiprocessing import Pool
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vslow as fs
HERE = os.path.dirname(os.path.abspath(__file__))
G = fs.G
CFG = (2.0, 0.0, 10, 'btc', 'both', 'all')
TOP_OOS = ['RAVEUSDT', 'FUNUSDT', 'MUSDT', 'TAIKOUSDT', 'ENJUSDT']
TOP_IS = ['TRBUSDT', 'RAYUSDT', 'RAREUSDT', 'CKBUSDT', 'BLZUSDT']
VARS = {
    'base': {}, 'okx_universe': {'cfg': (2.0, 0.0, 10, 'btc', 'both', 'okx')}, 'funding_x0.8': {'fund_mult': 0.8},
    'okx_and_funding_x0.8': {'cfg': (2.0, 0.0, 10, 'btc', 'both', 'okx'), 'fund_mult': 0.8},
    'entry_+1h': {'entry_delay': 1}, 'exit_+1h': {'exit_delay': 1}, 'entry_exit_+1h': {'entry_delay': 1, 'exit_delay': 1},
    'excl_top1_oos': {'excl': TOP_OOS[:1]}, 'excl_top5_oos': {'excl': TOP_OOS}, 'excl_top5_is': {'excl': TOP_IS},
    'mark_liq': {'use_mark': True}, 'close_only_liq': {'liq_mode': 'close_only'},
}


def run(args):
    name, L = args
    v = VARS[name]
    for k in ['fund_mult', 'entry_delay', 'exit_delay', 'use_mark', 'liq_mode', 'excl']:
        G.pop(k, None)
    for k, x in v.items():
        if k == 'excl':
            si = {s: i for i, s in enumerate(G['syms'])}
            G['excl'] = {si[s] for s in x}
        elif k != 'cfg':
            G[k] = x
    r = fs.run_one((v.get('cfg', CFG), L))
    r['variant'] = name
    return r


if __name__ == '__main__':
    fs.load()
    jobs = [(n, L) for n in VARS for L in fs.LEVS]
    t = time.time()
    with Pool(3) as p:
        rows = p.map(run, jobs, chunksize=1)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(HERE, 'v3_variants_slow.csv'), index=False)
    pd.set_option('display.width', 250)
    for L in fs.LEVS:
        x = df[df.L == L]
        print(f'--- L={L}')
        print(x[['variant', 'cagr_is', 'cagr_oos', 'maxdd_is', 'maxdd_oos', 'worst_day_oos', 'sharpe_oos', 'liq_is', 'liq_oos', 'trades_is', 'trades_oos']].round(3).to_string(index=False))
    print('elapsed', round(time.time() - t))
