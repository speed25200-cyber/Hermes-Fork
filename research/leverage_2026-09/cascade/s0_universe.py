"""Step 0: point-in-time universe + OKX 1m candle archive inventory (no returns computed here).
Universe for month m: coins with trailing-30d Binance USDT-M quote-volume rank <= 60 on the last day of month m-1
(longtail/data/ranks.parquet: rank computed with data up to D-1, only coins OKX-listed on D-1 per the static.okx.com
trade-archive calendar; stable/index/tradfi excluded).  Needed months per coin = universe months +/- 1 (warm-up, exits).
Files are fetched directly from static.okx.com/cdn/okex/traderecords/candlesticks/monthly/ (404 = not archived)."""
import json, time, ssl, urllib.request, sys
import numpy as np, pandas as pd
sys.path.insert(0, "/home/user/Hermes/src")
from hermes.execution.okx.instruments import okx_inst_id, binance_price_factor

W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade"
SP = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad"
CTX = ssl.create_default_context(cafile="/root/.ccr/ca-bundle.crt")
RMAX = 60


def get(u):
    for k in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "curl/8.5.0"}), context=CTX, timeout=30) as r:
                j = json.loads(r.read())
            if j.get("code") == "0":
                return j["data"]
            print("err", j, flush=True); time.sleep(2 + k)
        except Exception as e:
            print("exc", e, flush=True); time.sleep(2 + 2 * k)
    raise RuntimeError(u)


def main():
    r = pd.read_parquet(f"{SP}/longtail/data/ranks.parquet")
    months = pd.date_range("2022-01-01", "2026-08-01", freq="MS", tz="UTC")
    rows = []
    for m in months:
        row = r.loc[m - pd.Timedelta(days=1)]
        for s, rk in row[row <= RMAX].items():
            rows.append((m.strftime("%Y-%m"), s, int(rk)))
    U = pd.DataFrame(rows, columns=["month", "sym", "rank"])
    U["base"] = [okx_inst_id(s).replace("-USDT-SWAP", "") for s in U.sym]
    U["factor"] = [binance_price_factor(s) for s in U.sym]
    U.to_csv(f"{W}/data/universe.csv", index=False)
    print("universe rows", len(U), "coins", U.sym.nunique())
    # needed months per coin: universe months +/- 1
    need = {}
    for s, g in U.groupby("sym"):
        ms = set()
        for mm in g.month:
            p = pd.Period(mm, "M")
            ms |= {str(p - 1), str(p), str(p + 1)}
        need[s] = sorted(x for x in ms if "2021-12" <= x <= "2026-09")
    json.dump({"need": need}, open(f"{W}/data/need.json", "w"))
    print("coin-months needed", sum(len(v) for v in need.values()))
    print(U.groupby("month").size().describe())


if __name__ == "__main__":
    main()
