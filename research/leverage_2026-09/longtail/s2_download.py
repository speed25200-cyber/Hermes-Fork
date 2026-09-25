"""Step 2: 1h klines + funding for symbols that were ever in the PIT OKX-listed top-150 but are not in the Hermes
1h cache (/home/user/data/parsed/1h, read-only). Binance USDT-M monthly archives, parsed in memory, compact parquet."""
import io, os, ssl, time, zipfile, urllib.request, urllib.error, json
from concurrent.futures import ThreadPoolExecutor
import pandas as pd, numpy as np

W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail"
CTX = ssl.create_default_context(cafile="/root/.ccr/ca-bundle.crt")
R = "https://data.binance.vision/data/futures/um/monthly"


def get(url, tries=6):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1 + 2 * k)
        except Exception:
            time.sleep(1 + 2 * k)
    raise RuntimeError("failed " + url)


def rz(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.read(z.namelist()[0])
    return pd.read_csv(io.BytesIO(raw), header=0 if not raw[:1].isdigit() else None)


def do_sym(sym, months):
    fh = f"{W}/data/h/{sym}.parquet"
    if os.path.exists(fh):
        return sym, "cached"
    K, F = [], []
    for m in months:
        b = get(f"{R}/klines/{sym}/1h/{sym}-1h-{m}.zip")
        if b is not None:
            df = rz(b).iloc[:, [0, 1, 2, 3, 4, 7]]
            df.columns = ["t", "open", "high", "low", "close", "quote_volume"]
            K.append(df[pd.to_numeric(df.t, errors="coerce").notna()].astype(float))
        b = get(f"{R}/fundingRate/{sym}/{sym}-fundingRate-{m}.zip")
        if b is not None:
            df = rz(b)
            df = df.iloc[:, [0, df.shape[1] - 1]]
            df.columns = ["t", "rate"]
            F.append(df[pd.to_numeric(df.t, errors="coerce").notna()].astype(float))
    if not K:
        return sym, "nodata"
    k = pd.concat(K).drop_duplicates("t").sort_values("t")
    k["t"] = k["t"].astype("int64")
    k.to_parquet(fh, index=False)
    if F:
        f = pd.concat(F).drop_duplicates("t").sort_values("t")
        f.to_parquet(f"{W}/data/f/{sym}.parquet", index=False)
    return sym, len(k)


if __name__ == "__main__":
    os.makedirs(f"{W}/data/h", exist_ok=True)
    os.makedirs(f"{W}/data/f", exist_ok=True)
    rk = pd.read_parquet(f"{W}/data/ranks.parquet")
    ever = (rk <= 150).any()
    syms = sorted(ever[ever].index)
    have = set(f[:-8] for f in os.listdir("/home/user/data/parsed/1h"))
    miss = [s for s in syms if s not in have]
    daily = pd.read_parquet("/home/user/data/daily_volume_24cdb49d3f.parquet")
    need = {}
    for s in miss:
        d = daily[s].dropna()
        d = d[d > 0]
        a, b = max(d.index[0], pd.Timestamp("2021-10-01", tz="UTC")), min(d.index[-1], pd.Timestamp("2026-08-31", tz="UTC"))
        need[s] = [str(p) for p in pd.period_range(a.tz_localize(None), b.tz_localize(None), freq="M")]
    json.dump(need, open(f"{W}/data/need_months.json", "w"))
    print(len(need), "symbols,", sum(len(v) for v in need.values()), "months")
    with ThreadPoolExecutor(16) as ex:
        for s, n in ex.map(lambda kv: do_sym(*kv), need.items()):
            print(s, n, flush=True)
