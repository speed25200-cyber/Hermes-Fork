"""Adversarial checks on the premium-reversion grid (taker, lat 0, pm margin), reusing the researcher's engine:
 (1) widen the threshold grid beyond its edge (k = 100, 120, 150, 200 bp) and redo the IS-only selection over the
     union: does the IS winner stay at k=80, and what does the new IS winner do OOS?
 (2) leave-one-coin-out for the selected cfg (BTC only / ETH only) and leave-one-event-day-out in OOS/IS.
 (3) the selected cfg under the OKX published multi-currency margin (mc) and isolated (sep) models at 1..8x.
Output: out/ext_grid.csv.gz, printed tables."""
import itertools, sys, time
import numpy as np, pandas as pd
import run_grid as R, engine as E

t0 = time.time()
coins = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT']
Ps = {c: R.prep(c) for c in coins}
print('prep', round(time.time() - t0), flush=True)
KS = [100, 120, 150, 200]
cfgs = list(itertools.product(R.GRID['W'], KS, R.GRID['x'], R.GRID['H'], R.GRID['both'], R.GRID['confirm']))
rows = []
for cfg in cfgs:
    trd = {c: R.trades_for(Ps[c], c, cfg, 0, 0) for c in coins}
    for un, uc in R.UNIVERSES.items():
        allt = R.merge([trd[c] for c in uc])
        base = dict(exec='taker', lat=0, univ=un, W=cfg[0], k=cfg[1], x=cfg[2], H=cfg[3], both=cfg[4], confirm=cfg[5])
        rows += R.summarize(allt, R.IS_A, R.IS_B, R.IS_YEARS, dict(base, seg='IS'))
        rows += R.summarize(allt, R.OOS_A, R.OOS_B, R.OOS_YEARS, dict(base, seg='OOS'))
df = pd.DataFrame(rows)
df.to_csv(f'{R.OUTDIR}/ext_grid.csv.gz', index=False, float_format='%.6g')
print('ext grid done', round(time.time() - t0), flush=True)

# leave-one-coin-out for the selected config
sel = (1440, 80, 0.0, 480, 1, 0)
rows = []
for uc in (['BTCUSDT'], ['ETHUSDT'], ['BTCUSDT', 'ETHUSDT']):
    allt = R.merge([R.trades_for(Ps[c], c, sel, 0, 0) for c in uc])
    base = dict(univ='+'.join(uc))
    rows += R.summarize(allt, R.IS_A, R.IS_B, R.IS_YEARS, dict(base, seg='IS'))
    rows += R.summarize(allt, R.OOS_A, R.OOS_B, R.OOS_YEARS, dict(base, seg='OOS'))
lo = pd.DataFrame(rows)
lo.to_csv(f'{R.OUTDIR}/loco_sel.csv', index=False, float_format='%.6g')
pd.set_option('display.width', 250); pd.set_option('display.max_rows', 300)
print(lo[['univ', 'seg', 'margin', 'L', 'cagr', 'maxdd', 'liqs', 'trades', 'rejected', 'edge_bp', 'years']].round(4).to_string())
