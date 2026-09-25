"""Step 10 helper: hourly OKX funding array aligned to the panel (same stamping as Binance: bar ending at the funding
time). OKX archive covers 2022-01-01..2025-09-07; outside it, or for coins without OKX records, Binance funding is kept."""
import sys; sys.path.insert(0, "/home/user/Hermes/src")
import numpy as np, pandas as pd
from hermes.execution.okx.instruments import okx_inst_id
W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail"
s = np.load(f"{W}/data/simarrays.npz")
F = s["F"].copy(); hrs = s["hrs"]; syms = [str(x) for x in s["syms"]]
o = pd.read_parquet("/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/data/okx_funding_all.parquet")
end = int(o.funding_time.max() // 3600000)
cover = np.zeros(F.shape, bool)
n = 0
for j, sym in enumerate(syms):
    d = o[o.instId == okx_inst_id(sym)]
    if d.empty:
        continue
    h = (d.funding_time.to_numpy() // 3600000) - 1  # stamp on the bar that ends at the funding time
    pos = np.searchsorted(hrs, h)
    ok = (pos < len(hrs)) & (hrs[np.minimum(pos, len(hrs) - 1)] == h)
    first_h = h.min()
    rng = (hrs >= first_h) & (hrs <= end)
    F[rng, j] = 0.0
    np.add.at(F[:, j], pos[ok], d.real_funding_rate.fillna(d.funding_rate).to_numpy()[ok])
    cover[rng, j] = True
    n += 1
np.savez(f"{W}/data/okx_funding_hourly.npz", F=F, cover=cover, end_hour=end)
print("coins with OKX funding", n, "end", pd.to_datetime(end * 3600, unit="s"))
