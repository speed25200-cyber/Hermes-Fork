"""Step 12: Binance 1h MARK price klines (high/low) for every panel symbol: exchanges liquidate on the mark price, so
single-trade last-price wicks (e.g. BABYUSDT 2026-06-05 09:00, high 110x the previous close) must not decide
liquidations. Stored as hourly arrays aligned to the panel."""
import os, numpy as np, pandas as pd
from concurrent.futures import ThreadPoolExecutor
from s2_download import get, rz, W, R
daily = pd.read_parquet("/home/user/data/daily_volume_24cdb49d3f.parquet")
s = np.load(f"{W}/data/simarrays.npz")
hrs = s["hrs"]; syms = [str(x) for x in s["syms"]]
os.makedirs(f"{W}/data/m", exist_ok=True)

def one(sym):
    fn = f"{W}/data/m/{sym}.parquet"
    if os.path.exists(fn):
        return sym, "cached"
    d = daily[sym].dropna(); d = d[d > 0]
    a = max(d.index[0], pd.Timestamp("2021-10-01", tz="UTC")); b = min(d.index[-1], pd.Timestamp("2026-08-31", tz="UTC"))
    parts = []
    for m in pd.period_range(a.tz_localize(None), b.tz_localize(None), freq="M"):
        x = get(f"{R}/markPriceKlines/{sym}/1h/{sym}-1h-{m}.zip")
        if x is None:
            continue
        df = rz(x).iloc[:, [0, 2, 3]]
        df.columns = ["t", "mh", "ml"]
        parts.append(df[pd.to_numeric(df.t, errors="coerce").notna()].astype(float))
    if not parts:
        return sym, "nodata"
    k = pd.concat(parts).drop_duplicates("t").sort_values("t")
    k.to_parquet(fn, index=False)
    return sym, len(k)

with ThreadPoolExecutor(16) as ex:
    res = list(ex.map(one, syms))
print("nodata:", [r for r in res if r[1] == "nodata"])
MH = np.full((len(hrs), len(syms)), np.nan); ML = np.full((len(hrs), len(syms)), np.nan)
for j, sym in enumerate(syms):
    fn = f"{W}/data/m/{sym}.parquet"
    if not os.path.exists(fn):
        continue
    k = pd.read_parquet(fn)
    h = (k.t.to_numpy() // 3600000).astype(np.int64)
    pos = np.searchsorted(hrs, h); ok = (pos < len(hrs)) & (hrs[np.minimum(pos, len(hrs) - 1)] == h)
    MH[pos[ok], j] = k.mh.to_numpy()[ok]; ML[pos[ok], j] = k.ml.to_numpy()[ok]
np.savez(f"{W}/data/mark_hl.npz", MH=MH, ML=ML)
print("mark coverage (where close exists):", float(np.isfinite(MH[np.isfinite(s['H'])]).mean()))
