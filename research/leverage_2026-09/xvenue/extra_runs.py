"""Extra diagnostics (all configs selected on IS only; OOS just reported):
 1. BTC/ETH-only sub-family: best IS config per L from stage B, run on OOS.
 2. 'Instant collateral' what-if: transfer delay D=0 and hourly rebalance checks (e.g. a unified / off-exchange
    settlement account), to show whether the transfer delay is the binding constraint.
"""
import json, os
import numpy as np, pandas as pd
from experiments import pmap, flat, IS, OOS, OUT
if __name__ == '__main__':
    B = pd.read_csv(os.path.join(OUT, 'stageB_IS.csv'))
    F = pd.read_csv(os.path.join(OUT, 'final_runs.csv'))
    jobs, tags = [], []
    keys = ['L', 'R', 'k_dl', 'delta', 'H', 'th_in', 'th_out', 'K', 'universe', 'balance']
    def cfg(r):
        c = {k: r[k] for k in keys}
        for k in ('R', 'H', 'K'):
            c[k] = int(c[k])
        c['balance'] = bool(c['balance']); c['L'] = float(c['L']); c['k_dl'] = float(c['k_dl'])
        return c
    be = B[B.universe == 'btceth'].sort_values('cagr', ascending=False).groupby('L').head(1)
    for _, r in be.iterrows():
        for per, pr in [('IS', IS), ('OOS', OOS)]:
            jobs.append(dict(cfg(r), **pr)); tags.append(('btceth_only', per))
            jobs.append(dict(cfg(r), D=0, R=1, **pr)); tags.append(('btceth_only_instant_transfer', per))
    ch = F[(F.variant == 'base') & (F.period == 'IS')]
    for _, r in ch.iterrows():
        for per, pr in [('IS', IS), ('OOS', OOS)]:
            jobs.append(dict(cfg(r), D=0, R=1, **pr)); tags.append(('chosen_instant_transfer', per))
    res = pmap(jobs)
    rows = []
    for (tag, per), r in zip(tags, res):
        d = flat(r); d.update(variant=tag, period=per); rows.append(d)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT, 'extra_runs.csv'), index=False)
    pd.set_option('display.width', 250)
    print(df.pivot_table(index=['variant', 'L'], columns='period', values=['cagr', 'maxdd', 'cuts', 'liq']).round(4).to_string())
