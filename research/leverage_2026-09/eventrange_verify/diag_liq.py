"""Verifier diagnostic: where do the grid / wick liquidations come from? For each config at a given L and period,
list (coin, date) of liquidation days (daily sleeve return == -1)."""
import sys, json, collections
sys.argv = ['run.py', 'main']
import numpy as np, pandas as pd
import run
from common import COINS
fam = 'grid'
for L in (3, 5):
    cnt = collections.Counter(); per_cfg = {}
    for ci, cfg in enumerate(run.GRID_GRID):
        ev = []
        for coin in COINS:
            r, tr, st, _ = run.run_sleeve(fam, cfg, coin, L, 'OOS')
            dd = np.nonzero(r <= -0.999999)[0]
            for d in dd:
                ev.append((coin, str(run.DAYS['OOS'][d].date())))
        per_cfg[ci] = ev
        cnt.update(ev)
        print(L, ci, cfg, len(ev), ev[:8])
    print('L', L, 'most common liquidation (coin,day):', cnt.most_common(15))
