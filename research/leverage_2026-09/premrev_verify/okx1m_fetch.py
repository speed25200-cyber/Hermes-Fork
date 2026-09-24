"""Download OKX 1m last-price candles (history-candles) for {BTC,ETH}-USDT-SWAP and {BTC,ETH}-USDT spot over a
period, in memory, and store a compact per-minute panel per coin (disk is nearly full):
  sc (float32 spot close), b_c = F_c/S_c-1, b_o = F_o/S_o-1, s_o = S_o/S_c-1 (int16, 0.1 bp),
  s_rng, f_rng = (high-low)/close (int16, 1 bp), s_ok, f_ok (volume > 0).
Usage: python okx1m_fetch.py START END COIN [COIN...]     (UTC dates, END exclusive)"""
import sys, ssl, time, json, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd

CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev_verify/okxdata'
RATE = 9.0
_lock = threading.Lock(); _last = [0.0]


def throttle():
    with _lock:
        now = time.time(); wait = _last[0] + 1.0 / RATE - now
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()


def get(inst, after):
    u = f'https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=1m&limit=100&after={after}'
    for k in range(8):
        throttle()
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'curl/8.5.0'}), context=CTX, timeout=30) as r:
                j = json.loads(r.read())
            if j.get('code') == '0':
                return j['data']
            time.sleep(1 + k)
        except Exception:
            time.sleep(1 + 2 * k)
    raise RuntimeError(u)


def chunk(args):
    inst, a_ms, b_ms = args
    rows = []
    after = b_ms
    while after > a_ms:
        d = get(inst, after)
        if not d:
            break
        rows += d
        after = int(d[-1][0])
    return rows


def series(inst, a_ms, b_ms, nmin):
    edges = np.linspace(a_ms, b_ms, 9).astype(np.int64) // 60000 * 60000
    jobs = [(inst, int(edges[i]), int(edges[i + 1])) for i in range(8)]
    arr = np.full((nmin, 5), np.nan)
    with ThreadPoolExecutor(8) as ex:
        for rows in ex.map(chunk, jobs):
            if not rows:
                continue
            x = np.array([[float(v) for v in r[:6]] for r in rows])
            i = ((x[:, 0].astype(np.int64) - a_ms) // 60000)
            ok = (i >= 0) & (i < nmin)
            arr[i[ok]] = x[ok, 1:6]
    return arr   # o, h, l, c, vol


def main():
    a, b = pd.Timestamp(sys.argv[1], tz='UTC'), pd.Timestamp(sys.argv[2], tz='UTC')
    a_ms, b_ms = int(a.value // 10**6), int(b.value // 10**6)
    nmin = (b_ms - a_ms) // 60000
    for coin in sys.argv[3:]:
        t = time.time()
        F = series(f'{coin}-USDT-SWAP', a_ms, b_ms, nmin)
        S = series(f'{coin}-USDT', a_ms, b_ms, nmin)
        q = lambda x: np.where(np.isfinite(x), np.clip(np.round(x * 1e5), -32767, 32767), -32768).astype(np.int16)
        q1 = lambda x: np.where(np.isfinite(x), np.clip(np.round(x * 1e4), -32767, 32767), -32768).astype(np.int16)
        df = pd.DataFrame(dict(sc=S[:, 3].astype(np.float32), b_c=q(F[:, 3] / S[:, 3] - 1), b_o=q(F[:, 0] / S[:, 0] - 1),
                               s_o=q(S[:, 0] / S[:, 3] - 1), s_rng=q1((S[:, 1] - S[:, 2]) / S[:, 3]),
                               f_rng=q1((F[:, 1] - F[:, 2]) / F[:, 3]),
                               s_ok=np.isfinite(S[:, 3]) & (S[:, 4] > 0), f_ok=np.isfinite(F[:, 3]) & (F[:, 4] > 0)))
        fn = f'{OUT}/{coin}_{sys.argv[1]}_{sys.argv[2]}.parquet'
        df.to_parquet(fn, compression='zstd', compression_level=12, index=False)
        print(coin, 'coverage F %.4f S %.4f' % (np.isfinite(F[:, 3]).mean(), np.isfinite(S[:, 3]).mean()),
              'saved', fn, round(time.time() - t), 's', flush=True)


if __name__ == '__main__':
    import os; os.makedirs(OUT, exist_ok=True)
    main()
