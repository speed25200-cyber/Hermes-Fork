"""Venue check on OKX: for each selected trade (signal from Binance data), load OKX trade prints (perp SWAP and
spot, daily files from static.okx.com, days in UTC+8) IN MEMORY, and
  (a) the OKX perp-spot basis from last prints around the Binance signal boundary;
  (b) re-price the trade on OKX: entry at boundary + latency, exit at the Binance exit boundary + latency, each
      leg at the first OKX print on the side we would hit (sell -> taker-sell print = bid, buy -> taker-buy = ask),
      OKX VIP0 taker fees (perp 5 bp, spot 10 bp), Binance funding (from the 1m model).
Nothing but the small result CSVs is written to disk.
Usage: python okx_tick.py <trades_csv> <tag>"""
import io, sys, ssl, time, zipfile, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd

CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev_verify/out'
T0 = pd.Timestamp('2021-12-01', tz='UTC'); T0MS = int(T0.value // 1_000_000)
LATS = [0.0, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
FP, FS = 0.0005, 0.0010


def fetch(inst, day):
    d = day.replace('-', '')
    u = f'https://static.okx.com/cdn/okex/traderecords/trades/daily/{d}/{inst}-trades-{day}.zip'
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


def okx_day(t_ms):
    return (pd.Timestamp(t_ms, unit='ms') + pd.Timedelta(hours=8)).strftime('%Y-%m-%d')


def load(args):
    inst, day, windows = args
    b = fetch(inst, day)
    if b is None:
        return inst, day, None
    z = zipfile.ZipFile(io.BytesIO(b))
    with z.open(z.namelist()[0]) as f:
        df = pd.read_csv(f, usecols=['side', 'price', 'size', 'created_time'])
    t = df['created_time'].values.astype(np.int64)
    m = np.zeros(len(t), bool)
    for a, e in windows:
        m |= (t >= a) & (t <= e)
    df = df[m]
    return inst, day, (df['created_time'].values.astype(np.int64), df['price'].values.astype(float),
                       (df['side'].astype(str).str.lower().values == 'sell'))


def first_px(t, p, sell, at, want_sell):
    i = np.searchsorted(t, at, side='left')
    while i < len(t) and sell[i] != want_sell:
        i += 1
    return p[i] if i < len(t) else np.nan


def last_px(t, p, at):
    i = np.searchsorted(t, at, side='left') - 1
    return p[i] if i >= 0 else np.nan


def main():
    tr = pd.read_csv(sys.argv[1], parse_dates=['t'])
    tr = tr[(tr['exec'] == 'taker') & (tr['lat'] == 0)].reset_index(drop=True)
    ev = []
    need = {}
    for _, r in tr.iterrows():
        sig = int((r.t - T0).total_seconds() // 60)
        te = (sig + 1) * 60_000 + T0MS
        tx = (sig + 1 + int(r.hold_min)) * 60_000 + T0MS
        ev.append((r, te, tx))
        base = r.coin.replace('USDT', '')
        for (a, e) in [(te - 60_000, te + 120_000), (tx - 30_000, tx + 120_000)]:
            for d in {okx_day(a), okx_day(e)}:
                for inst in [f'{base}-USDT-SWAP', f'{base}-USDT']:
                    need.setdefault((inst, d), []).append((a, e))
    jobs = [(i, d, w) for (i, d), w in sorted(need.items())]
    print(len(jobs), 'OKX files', flush=True)
    data = {}
    with ThreadPoolExecutor(6) as ex:
        for inst, day, arr in ex.map(load, jobs):
            data[(inst, day)] = arr
            print(inst, day, None if arr is None else len(arr[0]), flush=True)

    def series(inst, t_ms):
        parts = [data.get((inst, d)) for d in sorted({okx_day(t_ms - 70_000), okx_day(t_ms), okx_day(t_ms + 130_000)})]
        parts = [p for p in parts if p is not None]
        if not parts:
            return None
        t = np.concatenate([p[0] for p in parts]); o = np.argsort(t, kind='stable')
        return t[o], np.concatenate([p[1] for p in parts])[o], np.concatenate([p[2] for p in parts])[o]

    rows, prof = [], []
    for r, te, tx in ev:
        base = r.coin.replace('USDT', '')
        P0, S0s = series(f'{base}-USDT-SWAP', te), series(f'{base}-USDT', te)
        P1, S1s = series(f'{base}-USDT-SWAP', tx), series(f'{base}-USDT', tx)
        side = int(r.side)
        d = dict(coin=r.coin, t=r.t, side=side, bn_dev_bp=r.dev_bp, bn_model_net_bp=r.net_bp, fund_bp=r.fund_bp)
        if P0 is None or S0s is None or P1 is None or S1s is None:
            rows.append(d); continue
        for s in [-30, -5, -1, 0, 1, 2, 5, 10, 30, 60, 90]:
            a = te + int(s * 1000)
            prof.append(dict(coin=r.coin, t=r.t, side=side, sec=s,
                             okx_basis_bp=(last_px(P0[0], P0[1], a + 1) / last_px(S0s[0], S0s[1], a + 1) - 1) * 1e4))
        # OKX 'median' reference: basis 30-60 s before the boundary is not a trailing 1-day median; report raw basis
        for lat in LATS:
            a = te + int(lat * 1000); b = tx + int(lat * 1000)
            F0 = first_px(*P0, a, side == 1)          # side +1 sells perp -> taker-sell print (bid)
            S0 = first_px(*S0s, a, side != 1)         # side +1 buys spot  -> taker-buy print (ask)
            F1 = first_px(*P1, b, side != 1)
            S1 = first_px(*S1s, b, side == 1)
            gross = side * ((F0 - F1) + (S1 - S0)) / S0
            fees = FP * F0 / S0 + FS + FP * F1 / S0 + FS * S1 / S0
            d[f'okx_b0_{lat}'] = (F0 / S0 - 1) * 1e4
            d[f'okx_net_{lat}'] = (gross - fees) * 1e4 + r.fund_bp
        rows.append(d)
    df = pd.DataFrame(rows); pr = pd.DataFrame(prof)
    df.to_csv(f'{OUT}/okx_tick_check_{sys.argv[2]}.csv', index=False, float_format='%.4g')
    pr.to_csv(f'{OUT}/okx_tick_profile_{sys.argv[2]}.csv', index=False, float_format='%.4g')
    pd.set_option('display.width', 250); pd.set_option('display.max_rows', 200)
    cols = ['coin', 't', 'side', 'bn_dev_bp', 'okx_b0_0.0', 'okx_b0_1.0', 'okx_b0_5.0', 'bn_model_net_bp'] + [f'okx_net_{l}' for l in LATS]
    print(df[[c for c in cols if c in df]].round(1).to_string(index=False))
    df['seg'] = np.where(df.t < pd.Timestamp('2025-01-01', tz='UTC'), 'IS', 'OOS')
    print(df.groupby('seg')[[c for c in df if c.startswith('okx_net') or c == 'bn_model_net_bp']].mean().round(1).to_string())
    print('all', df[[c for c in df if c.startswith('okx_net') or c == 'bn_model_net_bp']].mean().round(1).to_string())
    print(pr.pivot_table(index=['coin', 't', 'side'], columns='sec', values='okx_basis_bp').round(1).to_string())


if __name__ == '__main__':
    main()
