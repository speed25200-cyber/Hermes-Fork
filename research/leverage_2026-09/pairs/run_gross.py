"""Diagnostic: the same grid at L=1 with zero fees and zero slippage (gross edge of the signal)."""
import sys
sys.path.insert(0, '.')
from pairs_bt import *
P, syms, first_bar, U, mmr, imr = prepare()
sel = build_selections(P, U, first_bar)
schemes = [(m, h, w, k) for m in ['coint', 'corr'] for h in ['lvl', 'ret'] for w in [60, 120] for k in [3, 5, 10]]
schemes += [('fixed', h, w, 5) for h in ['lvl', 'ret'] for w in [60, 120]]
sig = [(Wz, zin, zout, zin + dz) for Wz in [72, 168, 336] for zin in [1.5, 2.0, 2.5, 3.0] for zout in [0.0, 0.5] for dz in [1.5, 3.0, 99.0]]
df = run_grid(P, syms, U, sel, mmr, imr, schemes, sig, [1], ['cross'], {'IS': (IS0, IS1), 'OOS': (OOS0, OOS1)}, 'gross', fee=0.0, slip_mult=0.0)
df.to_csv(os.path.join(OUT, 'grid_gross_1x.csv'), index=False)
