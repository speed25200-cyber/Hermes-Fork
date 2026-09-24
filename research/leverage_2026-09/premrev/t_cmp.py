import numpy as np, pandas as pd, run_grid as R, engine as E
coins = ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT']
Ps = {c: R.prep(c) for c in coins}
for cfg in [(240,80,0.0,480,1,1),(1440,30,0.0,120,1,0),(1440,60,0.5,120,1,0)]:
    for name, lat, mode, hm in R.EXECS:
        out = []
        for c in coins:
            tr = R.trades_for(Ps[c], c, cfg, mode, lat, hedge_mid=hm)
            isx = tr[:, 0] < R.IS_B
            net = (tr[:, E.C_GROSS] - tr[:, E.C_FEES] - tr[:, E.C_SLIP] + tr[:, E.C_FUND]) * 1e4
            out.append(f"{c[:4]} IS n{isx.sum()} {net[isx].mean():+.0f} OOS n{(~isx).sum()} {net[~isx].mean() if (~isx).sum() else np.nan:+.0f}")
        print(cfg, name, lat, ' | '.join(out))
