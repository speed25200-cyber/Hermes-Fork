"""Step 4: hourly panel (T x N) for every symbol ever in the PIT OKX-listed top-150, 2021-10-01 .. 2026-08-31.
Sources: Hermes 1h cache (read-only) + step-2 downloads. Funding is stamped on the bar that ENDS at the funding time
(Hermes convention): the position held during bar h pays funding[h]."""
import os, numpy as np, pandas as pd

W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail"
idx = pd.date_range("2021-10-01", "2026-08-31 23:00", freq="1h", tz="UTC")
rk = pd.read_parquet(f"{W}/data/ranks.parquet")
ever = (rk <= 150).any()
syms = sorted(ever[ever].index)
T, N = len(idx), len(syms)
A = {k: np.full((T, N), np.nan, np.float32) for k in ["open", "high", "low", "close", "qv"]}
F = np.zeros((T, N), np.float32)
src = {}
for j, s in enumerate(syms):
    p = f"/home/user/data/parsed/1h/{s}.parquet"
    if os.path.exists(p):
        d = pd.read_parquet(p, columns=["open", "high", "low", "close", "quote_volume", "funding_rate"])
        d = d[~d.index.duplicated()].reindex(idx)
        fr = d.funding_rate.fillna(0).to_numpy()
        src[s] = "hermes"
    else:
        d = pd.read_parquet(f"{W}/data/h/{s}.parquet")
        d.index = pd.to_datetime(d.t, unit="ms", utc=True)
        d = d[~d.index.duplicated()].reindex(idx)
        fr = np.zeros(T)
        fp = f"{W}/data/f/{s}.parquet"
        if os.path.exists(fp):
            f = pd.read_parquet(fp)
            ft = pd.to_datetime(f.t, unit="ms", utc=True).dt.floor("1h") - pd.Timedelta(hours=1)
            ser = pd.Series(f.rate.to_numpy(), index=ft).groupby(level=0).sum().reindex(idx).fillna(0)
            fr = ser.to_numpy()
        src[s] = "download"
    A["open"][:, j] = d.open; A["high"][:, j] = d.high; A["low"][:, j] = d.low; A["close"][:, j] = d.close
    A["qv"][:, j] = d.quote_volume
    F[:, j] = fr
np.savez(f"{W}/data/panel.npz", idx=((idx - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(hours=1)).to_numpy().astype(np.int64), syms=np.array(syms), funding=F, **A)
c = A["close"]
print("panel", T, N, "non-nan close frac", np.isfinite(c).mean().round(3), "sources", pd.Series(src).value_counts().to_dict())
# sanity: extreme hourly returns
r = c[1:] / c[:-1] - 1
print("hourly |ret|>50%:", int(np.nansum(np.abs(r) > 0.5)), " >100%:", int(np.nansum(r > 1.0)))
