"""Cross-correlation of 1m log returns: OKX perp vs Binance perp and OKX perp vs OKX spot, lags -2..2, by year.
Uses the researcher's quantized windows (contiguous minutes only). Peak at lag 0 => aligned."""
import numpy as np, pandas as pd, pyarrow.parquet as pq, glob, os
W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade/data/coins"
syms = ["BTCUSDT","ETHUSDT","SOLUSDT","DOGEUSDT","XRPUSDT","LINKUSDT","ADAUSDT","AVAXUSDT","WIFUSDT","1000PEPEUSDT","ARBUSDT","OPUSDT"]
rows = []
for s in syms:
    w = pq.read_table(f"{W}/{s}.win.parquet", columns=["t","pc","bc","sc","b_ok","s_ok"]).to_pandas()
    t = w.t.values.astype(np.int64)
    lp = np.where(w.pc.values == -2**31, np.nan, w.pc.values * 1e-5)
    lb = np.where(w.bc.values == -2**31, np.nan, lp + w.bc.values * 1e-5)
    ls = np.where(w.sc.values == -2**31, np.nan, lp + w.sc.values * 1e-5)
    yr = pd.to_datetime(t * 60, unit="s").year
    for name, other in (("bin", lb), ("spot", ls)):
        for lag in (-2, -1, 0, 1, 2):
            # r_okx(t) vs r_other(t+lag); contiguous in time
            ok1 = np.r_[False, np.diff(t) == 1]
            ro = np.where(ok1, np.r_[np.nan, np.diff(lp)], np.nan)
            rx = np.where(ok1, np.r_[np.nan, np.diff(other)], np.nan)
            if lag > 0:
                rx2 = np.r_[rx[lag:], [np.nan]*lag]; tt = np.r_[t[lag:], [-1]*lag]; okl = (tt - t) == lag
            elif lag < 0:
                rx2 = np.r_[[np.nan]*(-lag), rx[:lag]]; tt = np.r_[[-1]*(-lag), t[:lag]]; okl = (t - tt) == -lag
            else:
                rx2 = rx; okl = np.ones(len(t), bool)
            for y in (2022, 2023, 2024, 2025, 2026):
                m = okl & (yr == y) & np.isfinite(ro) & np.isfinite(rx2)
                if m.sum() > 1000:
                    rows.append(dict(sym=s, other=name, lag=lag, year=y, cc=np.corrcoef(ro[m], rx2[m])[0, 1], n=int(m.sum())))
R = pd.DataFrame(rows)
P = R.groupby(["other", "year", "lag"])["cc"].median().unstack("lag")
print(P.round(3).to_string())
R.to_csv("v2_align.csv", index=False)
