"""Step 5: entry-latency check on OKX trade prints for the selected perp-only config (family A or perp-only B/C).
For each accepted trade (OOS, and an equal-size random IS sample), load OKX perp trade prints (static.okx.com daily
files, UTC+8 days) in memory and re-price the ENTRY at signal close + delta seconds, delta in {1,2,5,10,30,60}:
long -> first taker-BUY print (ask side) at/after that time, short -> first taker-SELL print. Exit, fees and exit
slippage are kept from the 1m model; the entry slippage allowance is the rank floor (3/6 bp) instead of the 1m model.
Usage: python s5_ticks.py <fam>"""
import io, json, ssl, sys, time, zipfile, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
import core

W = core.W
CTX = ssl.create_default_context(cafile="/root/.ccr/ca-bundle.crt")
DELTAS = [1, 2, 5, 10, 30, 60]


def fetch(inst, day):
    u = f"https://static.okx.com/cdn/okex/traderecords/trades/daily/{day.replace('-', '')}/{inst}-trades-{day}.zip"
    for k in range(5):
        try:
            return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "curl/8.5.0"}),
                                          context=CTX, timeout=300).read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(3 * (k + 1))
        except Exception:
            time.sleep(3 * (k + 1))
    return None


def okx_day(t_ms):
    return (pd.Timestamp(t_ms, unit="ms") + pd.Timedelta(hours=8)).strftime("%Y-%m-%d")


def load(args):
    inst, day, wins = args
    b = fetch(inst, day)
    if b is None:
        return inst, day, None
    z = zipfile.ZipFile(io.BytesIO(b))
    with z.open(z.namelist()[0]) as f:
        df = pd.read_csv(f, usecols=["side", "price", "created_time"], encoding_errors="ignore")
    t = df["created_time"].values.astype(np.int64)
    m = np.zeros(len(t), bool)
    for a, e in wins:
        m |= (t >= a) & (t <= e)
    df = df[m].sort_values("created_time")
    return inst, day, (df["created_time"].values.astype(np.int64), df["price"].values.astype(float),
                       df["side"].astype(str).str.lower().values == "buy")


def main():
    fam = sys.argv[1]
    sel = json.load(open(f"{W}/out/selection.json"))["per_family"][fam]
    tr = pd.read_csv(f"{W}/out/trades_{fam}.csv")
    U = pd.read_csv(f"{W}/data/universe.csv").drop_duplicates("sym").set_index("sym")
    import glob, os
    syms = sorted(os.path.basename(f).replace(".trig.parquet", "") for f in glob.glob(f"{W}/data/coins/*.trig.parquet"))
    tr["sym"] = np.array(syms)[tr.coin.values]
    tr["dt"] = pd.to_datetime(tr.te.values.astype(np.int64) * 60, unit="s", utc=True)
    oos = tr[tr.dt >= core.OOS0]
    isx = tr[tr.dt < core.IS1]
    isx = isx.sample(min(len(isx), len(oos)), random_state=0)
    sub = pd.concat([oos.assign(per="OOS"), isx.assign(per="IS")], ignore_index=True)
    need = {}
    for r in sub.itertuples():
        sig_end = (r.t + 1) * 60_000
        inst = f"{U.loc[r.sym, 'base']}-USDT-SWAP"
        a, e = sig_end - 5_000, sig_end + 70_000
        for d in {okx_day(a), okx_day(e)}:
            need.setdefault((inst, d), []).append((a, e))
    print("trades", len(sub), "files", len(need), flush=True)
    data = {}
    with ThreadPoolExecutor(8) as ex:
        for inst, day, x in ex.map(load, [(k[0], k[1], v) for k, v in need.items()]):
            if x is not None:
                data.setdefault(inst, []).append(x)
    ticks = {}
    for inst, lst in data.items():
        t = np.concatenate([x[0] for x in lst]); p = np.concatenate([x[1] for x in lst]); b = np.concatenate([x[2] for x in lst])
        k = np.argsort(t, kind="stable"); ticks[inst] = (t[k], p[k], b[k])
    rows = []
    for r in sub.itertuples():
        inst = f"{U.loc[r.sym, 'base']}-USDT-SWAP"
        if inst not in ticks:
            continue
        t, p, b = ticks[inst]
        sig_end = (r.t + 1) * 60_000
        floor = 3.0 if r.rank <= 20 else 6.0
        base_entry_cost = r.cost_e * 1e4 - core.FEE["perp"]
        out = dict(per=r.per, sym=r.sym, dt=r.dt, d=r.d, net_1m=r.net * 1e4)
        for dl in DELTAS:
            i = np.searchsorted(t, sig_end + dl * 1000, side="left")
            want_buy = r.d == 1
            while i < len(t) and b[i] != want_buy:
                i += 1
            if i >= len(t) or t[i] > sig_end + dl * 1000 + 30_000:
                out[f"net_{dl}s"] = np.nan; continue
            lpe = np.log(p[i])
            gross = r.d * (np.exp(r.px - lpe) - 1)
            cost = r.cost * 1e4 - base_entry_cost + floor
            out[f"net_{dl}s"] = gross * 1e4 - cost + r.fund * 1e4
        rows.append(out)
    R = pd.DataFrame(rows)
    R.to_csv(f"{W}/out/ticks_latency_{fam}.csv", index=False)
    summ = R.groupby("per")[[c for c in R.columns if c.startswith("net_")]].agg(["mean", "count"]).T
    print(summ.round(1).to_string())
    json.dump({"cid": sel, "summary": {f"{a}|{b}": {k: float(v) for k, v in summ.loc[(a, b)].items()}
                                       for a, b in summ.index}}, open(f"{W}/out/ticks_latency_{fam}.json", "w"), indent=1)


if __name__ == "__main__":
    main()
