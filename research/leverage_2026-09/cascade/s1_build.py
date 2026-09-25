"""Step 1: per-coin 1m panel (OKX perp, OKX spot, Binance perp), features, liberal triggers, event windows.

Everything is downloaded into memory (no raw files on disk). Per coin we save:
  trig: one row per universe minute where a LIBERAL trigger holds (superset of every grid config's trigger):
        A: any w in {1,5,15}: |z_w| >= 6 and |R_w| >= 1%;  B: |db| >= 30 bp;  C: |dx| >= 30 bp
  win : 1m price paths for minutes [t-1, t+262] around every trigger minute t (covers entry latency <= 3 min + hold
        <= 240 min + exit latency), quantized: pc = round(ln perp close * 1e5) (int32); po/ph/pl = ln(x/perp close)*1e4
        (int16, 1 bp); sc, bc = ln(spot or Binance close / perp close)*1e5 (int32, 0.1 bp); sh/sl, bh/bl = ln(x/own close)
        *1e4 (int16); s_ok/b_ok = traded in that minute. Missing = -2^31 / -32768.
  gate: packed bits of 'valid z5 in universe' and sparse minute indices with z5 <= -3 / >= +3 (for breadth)
Minute index = unix minutes (open_time_ms // 60000).  All features at minute t use data <= t (medians/sigma end at t-15).
Usage: python s1_build.py [n_workers] [coin ...]
"""
import io, json, os, ssl, sys, time, zipfile, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool
import numpy as np, pandas as pd
import pyarrow as pa, pyarrow.parquet as pq

W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/cascade"
OUT = f"{W}/data/coins"
CTX = ssl.create_default_context(cafile="/root/.ccr/ca-bundle.crt")
M0 = int(pd.Timestamp("2021-11-30 16:00", tz="UTC").value // 60_000_000_000)   # first minute of OKX 2021-12 file
M1 = int(pd.Timestamp("2026-09-01 16:00", tz="UTC").value // 60_000_000_000)
NM = M1 - M0
SIGW, LAG = 1440, 15


def fetch(url):
    for k in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"}),
                                        context=CTX, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                return None
            time.sleep(1 + 2 * k)
        except Exception:
            time.sleep(1 + 2 * k)
    return None


def okx_url(inst, month):
    if month == "2026-09":
        return f"https://static.okx.com/cdn/okex/traderecords/candlesticks/daily/20260901/{inst}-candlesticks-2026-09-01.zip"
    return f"https://static.okx.com/cdn/okex/traderecords/candlesticks/monthly/{month.replace('-', '')}/{inst}-candlesticks-{month}.zip"


def bin_url(sym, month):
    return f"https://data.binance.vision/data/futures/um/monthly/klines/{sym}/1m/{sym}-1m-{month}.zip"


def parse_okx(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    d = pd.read_csv(z.open(z.namelist()[0]), usecols=["open", "high", "low", "close", "vol", "open_time"])
    return d.open_time.values // 60000, d[["open", "high", "low", "close", "vol"]].values.astype(np.float64)


def parse_bin(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    raw = z.open(z.namelist()[0]).read()
    d = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 1, 2, 3, 4, 5], low_memory=False)
    if not str(d.iloc[0, 0]).isdigit():
        d = d.iloc[1:]
    t = d[0].astype(np.int64).values
    if t.max() > 10**14:   # microseconds
        t = t // 1000
    return t // 60000, d[[1, 2, 3, 4, 5]].astype(np.float64).values


NA32 = -2**31


def q32(x, s):
    y = np.round(x * s)
    return np.where(np.isfinite(y), y, NA32).astype(np.int32)


def q16(x, s):
    y = np.clip(np.round(x * s), -32767, 32767)
    return np.where(np.isfinite(y), y, -32768).astype(np.int16)


def fill(arr, t, x):
    i = t - M0
    ok = (i >= 0) & (i < NM)
    arr[i[ok]] = x[ok]


def roll(x, w, fn, minp):
    s = pd.Series(x)
    r = getattr(s.rolling(w, min_periods=minp), fn)()
    return r.shift(LAG).values


def process(args):
    sym, base, factor, months, umonths = args
    fn = f"{OUT}/{sym}.cov.json"
    if os.path.exists(fn):
        return sym, "cached"
    t0 = time.time()
    jobs = []
    for m in months:
        jobs += [("P", m, okx_url(f"{base}-USDT-SWAP", m)), ("S", m, okx_url(f"{base}-USDT", m))]
        if m <= "2026-08":
            jobs.append(("B", m, bin_url(sym, m)))
    with ThreadPoolExecutor(12) as ex:
        blobs = list(ex.map(lambda j: fetch(j[2]), jobs))
    P = np.full((NM, 5), np.nan); S = np.full((NM, 5), np.nan); B = np.full((NM, 5), np.nan)
    miss = {"P": [], "S": [], "B": []}
    for (kind, m, _), b in zip(jobs, blobs):
        if b is None:
            miss[kind].append(m); continue
        try:
            t, x = parse_okx(b) if kind in "PS" else parse_bin(b)
        except Exception as e:
            miss[kind].append(m + ":parse"); continue
        fill({"P": P, "S": S, "B": B}[kind], t, x)
    del blobs
    B[:, :4] /= factor
    # universe mask (UTC months) with rank
    rank = np.zeros(NM, np.int16)
    for m, rk in umonths:
        a = int(pd.Timestamp(m + "-01", tz="UTC").value // 60_000_000_000) - M0
        b_ = int((pd.Timestamp(m + "-01", tz="UTC") + pd.offsets.MonthBegin(1)).value // 60_000_000_000) - M0
        rank[max(a, 0):min(b_, NM)] = rk
    univ = rank > 0
    pc = P[:, 3]
    lp = np.log(pc)
    r1 = np.diff(lp, prepend=np.nan)
    var = roll(r1 ** 2, SIGW, "mean", 720) - roll(r1, SIGW, "mean", 720) ** 2
    sig = np.maximum(np.sqrt(np.maximum(var, 0)), 1e-4)
    vol = P[:, 4]
    vbase = roll(vol, SIGW, "mean", 720)
    feats = {}
    for w in (1, 5, 15):
        R = lp - np.concatenate([np.full(w, np.nan), lp[:-w]])
        feats[f"R{w}"] = R
        feats[f"z{w}"] = R / (sig * np.sqrt(w))
        vs = pd.Series(vol).rolling(w, min_periods=w).sum().values
        feats[f"vr{w}"] = vs / (w * np.maximum(vbase, 1e-12))
    s_ok = S[:, 4] > 0
    b = np.where(s_ok & np.isfinite(pc), lp - np.log(S[:, 3]), np.nan)
    bmed = roll(b, SIGW, "median", 360)
    db = b - bmed
    b_ok = B[:, 4] > 0
    p_ok = vol > 0
    x = np.where(b_ok & p_ok, lp - np.log(B[:, 3]), np.nan)
    xmed = roll(x, SIGW, "median", 360)
    dx = x - xmed
    z5 = feats["z5"]
    az5 = np.abs(np.nan_to_num(z5))
    cgate = pd.Series(az5).rolling(16, min_periods=1).max().values >= 4
    trA = np.zeros(NM, bool)
    for w in (1, 5, 15):
        trA |= (np.abs(np.nan_to_num(feats[f"z{w}"])) >= 6) & (np.abs(np.nan_to_num(feats[f"R{w}"])) >= 0.01)
    trB = np.abs(np.nan_to_num(db)) >= 0.003
    trC = np.abs(np.nan_to_num(dx)) >= 0.003
    trig = univ & (trA | trB | trC) & np.isfinite(sig)
    ti = np.flatnonzero(trig)
    T = {"t": (ti + M0).astype(np.int32), "rank": rank[ti], "sig": sig[ti].astype(np.float32),
         "fA": trA[ti], "fB": trB[ti], "fC": trC[ti], "cgate": cgate[ti],
         "db": db[ti].astype(np.float32), "bmed": bmed[ti].astype(np.float32),
         "dx": dx[ti].astype(np.float32), "xmed": xmed[ti].astype(np.float32)}
    for k, v in feats.items():
        T[k] = v[ti].astype(np.float32)
    # windows
    mark = np.zeros(NM + 300, np.int32)
    np.add.at(mark, np.clip(ti - 1, 0, None), 1)
    np.add.at(mark, ti + 263, -1)
    wmask = np.cumsum(mark)[:NM] > 0
    wi = np.flatnonzero(wmask)
    lpc = lp[wi]
    Wd = {"t": (wi + M0).astype(np.int32), "pc": q32(lpc, 1e5)}
    for j, c in ((0, "o"), (1, "h"), (2, "l")):
        Wd["p" + c] = q16(np.log(P[wi, j]) - lpc, 1e4)
    for nm, arr in (("s", S), ("b", B)):
        lc = np.log(arr[wi, 3])
        Wd[nm + "c"] = q32(lc - lpc, 1e5)
        for j, c in ((1, "h"), (2, "l")):
            Wd[nm + c] = q16(np.log(arr[wi, j]) - lc, 1e4)
    Wd["s_ok"] = s_ok[wi]; Wd["b_ok"] = b_ok[wi]
    # gate contributions
    gvalid = univ & np.isfinite(z5)
    G = {"gvalid": np.packbits(gvalid), "gdn": np.flatnonzero(gvalid & (z5 <= -3)).astype(np.int32),
         "gup": np.flatnonzero(gvalid & (z5 >= 3)).astype(np.int32)}
    # coverage of universe minutes
    cov = {"univ_min": int(univ.sum()), "perp_min": int((univ & np.isfinite(pc)).sum()),
           "spot_min": int((univ & np.isfinite(S[:, 3])).sum()), "bin_min": int((univ & np.isfinite(B[:, 3])).sum()),
           "nA": int((univ & trA).sum()), "nB": int((univ & trB).sum()), "nC": int((univ & trC).sum()),
           "win_min": int(len(wi)), "miss": miss}
    pq.write_table(pa.table(T), f"{OUT}/{sym}.trig.parquet", compression="zstd", compression_level=12)
    pq.write_table(pa.table(Wd), f"{OUT}/{sym}.win.parquet", compression="zstd", compression_level=12,
                   use_dictionary=False, column_encoding={k: "DELTA_BINARY_PACKED" for k in ("t", "pc", "sc", "bc")})
    np.savez_compressed(f"{OUT}/{sym}.gate.npz", **G)
    json.dump(cov, open(fn, "w"))
    return sym, dict(cov, sec=round(time.time() - t0))


def main():
    os.makedirs(OUT, exist_ok=True)
    nw = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    U = pd.read_csv(f"{W}/data/universe.csv")
    need = json.load(open(f"{W}/data/need.json"))["need"]
    syms = sys.argv[2:] or sorted(need)
    args = []
    for s in syms:
        g = U[U.sym == s]
        args.append((s, g.base.iloc[0], float(g.factor.iloc[0]), need[s], list(zip(g.month, g["rank"]))))
    with Pool(nw) as pool:
        for sym, info in pool.imap_unordered(process, args):
            print(sym, json.dumps(info) if isinstance(info, dict) else info, flush=True)


if __name__ == "__main__":
    main()
