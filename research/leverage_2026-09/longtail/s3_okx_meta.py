"""Step 3: OKX execution metadata today (2026-09-25): contract specs, quoted spreads (repeated ticker snapshots),
order-book depth (cost of a market order of 50..2500 USDT vs mid), and tier-1 maintenance margin per contract."""
import json, time, ssl, urllib.request
from concurrent.futures import ThreadPoolExecutor
import pandas as pd, numpy as np

W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail"
CTX = ssl.create_default_context(cafile="/root/.ccr/ca-bundle.crt")


def get(url, tries=6):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
                j = json.loads(r.read())
            if j.get("code") == "0":
                return j["data"]
            time.sleep(1 + k)
        except Exception:
            time.sleep(1 + 2 * k)
    return None


inst = get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
json.dump(inst, open(f"{W}/venue/okx_instruments_now.json", "w"))
I = pd.DataFrame(inst)
I = I[I.instId.str.endswith("-USDT-SWAP") & (I.state == "live") & (I.instCategory == "1")].copy()
for c in ["ctVal", "lotSz", "minSz", "tickSz", "lever"]:
    I[c] = I[c].astype(float)
print("live crypto USDT swaps:", len(I))

# spreads: 8 ticker snapshots, 20 s apart
snaps = []
for k in range(8):
    t = get("https://www.okx.com/api/v5/market/tickers?instType=SWAP")
    d = pd.DataFrame(t)
    d = d[d.instId.isin(I.instId)]
    for c in ["bidPx", "askPx", "last", "volCcy24h", "vol24h"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["spr_bp"] = (d.askPx - d.bidPx) / ((d.askPx + d.bidPx) / 2) * 1e4
    snaps.append(d[["instId", "spr_bp", "last", "volCcy24h", "vol24h"]])
    time.sleep(20)
S = pd.concat(snaps).groupby("instId").agg(spr_bp=("spr_bp", "median"), last=("last", "last"),
                                            volCcy24h=("volCcy24h", "last"), vol24h=("vol24h", "last"))
I = I.merge(S, left_on="instId", right_index=True, how="left")
I["ct_usd"] = I.ctVal * I["last"]
I["min_usd"] = I.minSz * I.ct_usd
I["lot_usd"] = I.lotSz * I.ct_usd
I["vol24h_usd"] = I.volCcy24h * I["last"]


# depth: cost (bp vs mid) of a taker order of X USDT, average of buy and sell, from 400-level book
def depth(iid):
    b = get(f"https://www.okx.com/api/v5/market/books?instId={iid}&sz=400")
    if not b:
        return iid, {}
    b = b[0]
    ct = float(I.loc[I.instId == iid, "ctVal"].iloc[0])
    asks = np.array([[float(x[0]), float(x[1]) * ct] for x in b["asks"]])
    bids = np.array([[float(x[0]), float(x[1]) * ct] for x in b["bids"]])
    if len(asks) == 0 or len(bids) == 0:
        return iid, {}
    mid = (asks[0, 0] + bids[0, 0]) / 2
    out = {}
    for X in [50, 250, 1000, 2500, 10000]:
        cs = []
        for side in (asks, bids):
            usd = side[:, 0] * side[:, 1]
            cum = np.cumsum(usd)
            if cum[-1] < X:
                cs.append(np.nan)
                continue
            j = np.searchsorted(cum, X)
            filled_q = side[:j, 1].sum() + (X - (cum[j - 1] if j else 0)) / side[j, 0]
            vwap = X / filled_q
            cs.append(abs(vwap / mid - 1) * 1e4)
        out[f"cost{X}_bp"] = np.nanmean(cs) if not all(np.isnan(cs)) else np.nan
    return iid, out


D = {}
with ThreadPoolExecutor(4) as ex:
    for iid, o in ex.map(depth, I.instId.tolist()):
        D[iid] = o
        time.sleep(0.05)
I = I.merge(pd.DataFrame(D).T, left_on="instId", right_index=True, how="left")


# tier-1 maintenance margin (cross) per instFamily
def tier(fam):
    d = get(f"https://www.okx.com/api/v5/public/position-tiers?instType=SWAP&tdMode=cross&instFamily={fam}")
    time.sleep(0.25)
    if not d:
        return fam, None
    t = pd.DataFrame(d)
    for c in ["tier", "mmr", "imr", "maxSz", "minSz", "maxLever"]:
        t[c] = pd.to_numeric(t[c], errors="coerce")
    t = t.sort_values("tier")
    return fam, t[["tier", "minSz", "maxSz", "mmr", "imr", "maxLever"]].to_dict("records")


fams = sorted(I.instFamily.unique())
T = {}
with ThreadPoolExecutor(2) as ex:
    for fam, t in ex.map(tier, fams):
        T[fam] = t
json.dump(T, open(f"{W}/venue/okx_tiers_now.json", "w"))
I["mmr1"] = I.instFamily.map(lambda f: T.get(f)[0]["mmr"] if T.get(f) else np.nan)
I["tier1_max_ct"] = I.instFamily.map(lambda f: T.get(f)[0]["maxSz"] if T.get(f) else np.nan)
I["tier1_max_usd"] = I.tier1_max_ct * I.ct_usd
keep = ["instId", "instFamily", "listTime", "ctVal", "lotSz", "minSz", "tickSz", "lever", "last", "ct_usd", "min_usd",
        "lot_usd", "vol24h_usd", "spr_bp", "cost50_bp", "cost250_bp", "cost1000_bp", "cost2500_bp", "cost10000_bp",
        "mmr1", "tier1_max_usd"]
I[keep].to_csv(f"{W}/out/okx_meta_now.csv", index=False)
print(I[keep].describe().T.to_string())
