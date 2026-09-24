"""OKX-native tick-level test of the rule on the most likely days (out/okx_scan_days.json: every UTC day with a
Binance BTC/ETH basis deviation >= 40 bp, plus the 60 most volatile days per coin, 2022-01..2026-08).
For each (coin, UTC day D): OKX trade prints of <COIN>-USDT-SWAP and <COIN>-USDT (daily files are UTC+8 days,
so the files named D and D+1 are loaded, in memory), 1-second last-price grid, basis = swap/spot - 1,
baseline = trailing 4 h median of the 10 s-sampled basis (past only), dev = basis - baseline.
Rule (as the selected Binance config, x=0, H=480 min, both sides): enter when |dev| > k at the end of a second,
executed `lat` seconds later at the first print on the side we hit (sell -> taker-sell print, buy -> taker-buy),
exit when side*dev <= 0 or after 480 min, same execution. OKX VIP0 taker fees (perp 5 bp, spot 10 bp); funding
ignored (holds are minutes). One position at a time per coin; entries only inside day D.
Writes out/okx_scan_trades.csv (small)."""
import io, json, ssl, time, zipfile, urllib.request
from multiprocessing import Pool
import numpy as np, pandas as pd

CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev/out'
KS = [40e-4, 60e-4, 80e-4, 120e-4]
LATS = [0.5, 2.0, 10.0]
H_S = 480 * 60
FP, FS = 0.0005, 0.0010


def fetch(inst, day):
    u = f'https://static.okx.com/cdn/okex/traderecords/trades/daily/{day.replace("-", "")}/{inst}-trades-{day}.zip'
    for k in range(5):
        try:
            return urllib.request.urlopen(u, context=CTX, timeout=300).read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(3 * (k + 1))
        except Exception:
            time.sleep(3 * (k + 1))
    return None


def prints(inst, days, a_ms, b_ms):
    ts, ps, ss = [], [], []
    for d in days:
        b = fetch(inst, d)
        if b is None:
            continue
        z = zipfile.ZipFile(io.BytesIO(b))
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, usecols=['side', 'price', 'created_time'])
        t = df['created_time'].values.astype(np.int64)
        m = (t >= a_ms) & (t < b_ms)
        ts.append(t[m]); ps.append(df['price'].values[m].astype(float)); ss.append(df['side'].astype(str).str.lower().values[m] == 'sell')
    if not ts:
        return None
    t = np.concatenate(ts); o = np.argsort(t, kind='stable')
    return t[o], np.concatenate(ps)[o], np.concatenate(ss)[o]


def first_px(t, p, sell, at, want_sell):
    i = np.searchsorted(t, at, side='left')
    j = i + np.argmax(sell[i:i + 5000] == want_sell) if i < len(t) else len(t)
    if j >= len(t) or sell[j] != want_sell:
        return np.nan
    return p[j]


def job(args):
    coin, day = args
    base = coin.replace('USDT', '')
    D = pd.Timestamp(day, tz='UTC')
    a = D - pd.Timedelta(hours=4); b = D + pd.Timedelta(days=1, hours=8)
    a_ms, b_ms = int(a.value // 1e6), int(b.value // 1e6)
    files = [D.strftime('%Y-%m-%d'), (D + pd.Timedelta(days=1)).strftime('%Y-%m-%d')]
    P = prints(f'{base}-USDT-SWAP', files, a_ms, b_ms)
    S = prints(f'{base}-USDT', files, a_ms, b_ms)
    if P is None or S is None or len(P[0]) < 100 or len(S[0]) < 100:
        return coin, day, [], 'missing'
    grid = np.arange(a_ms + 999, b_ms, 1000)                       # end of each second
    ip = np.searchsorted(P[0], grid, side='right') - 1
    isp = np.searchsorted(S[0], grid, side='right') - 1
    valid = (ip >= 0) & (isp >= 0)
    fpx = np.where(valid, P[1][np.maximum(ip, 0)], np.nan)
    spx = np.where(valid, S[1][np.maximum(isp, 0)], np.nan)
    basis = fpx / spx - 1
    b10 = pd.Series(basis[::10])
    base10 = b10.shift(1).rolling(1440, min_periods=360).median().values
    baseline = np.repeat(base10, 10)[:len(basis)]
    dev = basis - baseline
    d0 = int((D.value // 1e6 - a_ms) // 1000); d1 = d0 + 86400
    trades = []
    for k in KS:
        for lat in LATS:
            s = d0
            while s < d1:
                if not np.isfinite(dev[s]) or abs(dev[s]) <= k:
                    s += 1
                    continue
                side = 1 if dev[s] > 0 else -1
                te = grid[s] + int(lat * 1000)
                F0 = first_px(*P, te, side == 1); S0 = first_px(*S, te, side != 1)
                # exit search
                seg = side * dev[s + 1:min(s + 1 + H_S, len(dev))]
                hit = np.where(np.isfinite(seg) & (seg <= 0))[0]
                s2 = s + 1 + (hit[0] if len(hit) else min(H_S, len(dev) - s - 2))
                tx = grid[s2] + int(lat * 1000)
                F1 = first_px(*P, tx, side != 1); S1 = first_px(*S, tx, side == 1)
                gross = side * ((F0 - F1) + (S1 - S0)) / S0
                fees = FP * F0 / S0 + FS + FP * F1 / S0 + FS * S1 / S0
                # worst mark-to-market during the hold from 1 s last prints (adverse basis move vs the entry basis)
                path = basis[s + 1:s2 + 1]
                worst = np.nanmin(side * ((F0 / S0 - 1) - path)) if np.isfinite(path).any() else np.nan
                trades.append(dict(coin=coin, day=day, k_bp=k * 1e4, lat=lat, t=pd.Timestamp(grid[s], unit='ms', tz='UTC'),
                                   t_exit=pd.Timestamp(grid[s2], unit='ms', tz='UTC'),
                                   side=side, dev_bp=dev[s] * 1e4, b0_bp=(F0 / S0 - 1) * 1e4, hold_s=int(s2 - s),
                                   gross_bp=gross * 1e4, net_bp=(gross - fees) * 1e4,
                                   fee_in_bp=(FP * F0 / S0 + FS) * 1e4, worst_bp=worst * 1e4))
                s = s2 + 1
    mx = np.nanmax(np.abs(dev[d0:d1])) * 1e4 if np.isfinite(dev[d0:d1]).any() else np.nan
    return coin, day, trades, f'max|dev|={mx:.0f}bp'


def main():
    days = json.load(open(f'{OUT}/okx_scan_days.json'))
    jobs = [(c, d) for c in days for d in days[c]]
    rows, info = [], []
    with Pool(4) as p:
        for coin, day, trades, msg in p.imap_unordered(job, jobs):
            rows += trades
            info.append(dict(coin=coin, day=day, msg=msg))
            print(coin, day, len(trades), msg, flush=True)
    pd.DataFrame(rows).to_csv(f'{OUT}/okx_scan_trades.csv', index=False, float_format='%.4g')
    pd.DataFrame(info).to_csv(f'{OUT}/okx_scan_days_info.csv', index=False)


if __name__ == '__main__':
    main()
