"""Independent re-check of the selected config's taker trades with Binance aggTrades (perp + spot), processed
entirely in memory (disk is full). For each trade, entry at the signal-minute boundary + latency and exit at the
exit-minute boundary + latency, as in the researcher's tick_check.py, but with two fill models:
  touch : first print on the side we hit (sell -> seller-initiated print = bid; buy -> buyer-initiated = ask)
  walkN : VWAP of the consecutive prints on that side starting at boundary+latency until the cumulative quote
          notional reaches N USDT (a proxy for walking the book with an N-USDT market order).
Fees: OKX VIP0 taker on both legs (perp 5 bp, spot 10 bp). Funding from the 1m model.
Usage: python tick_verify.py <trades_csv> <tag> [oos_only]
"""
import io, sys, ssl, time, zipfile, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd

CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
BASE = 'https://data.binance.vision/'
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev_verify/out'
T0 = pd.Timestamp('2021-12-01', tz='UTC'); T0MS = int(T0.value // 1_000_000)
LATS = [0.0, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]
SIZES = [20_000, 100_000, 500_000]
FP, FS = 0.0005, 0.0010


def fetch(path):
    for k in range(6):
        try:
            with urllib.request.urlopen(BASE + path, context=CTX, timeout=600) as r:
                return r.read()
        except Exception:
            time.sleep(3 * (k + 1))
    raise RuntimeError(path)


def load(args):
    mk, sym, day, windows = args
    kind = 'futures/um' if mk == 'perp' else 'spot'
    b = fetch(f'data/{kind}/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip')
    z = zipfile.ZipFile(io.BytesIO(b))
    with z.open(z.namelist()[0]) as f:
        first = f.readline().decode()
    header = not first.split(',')[0].strip().isdigit()
    keep = []
    with z.open(z.namelist()[0]) as f:
        for ch in pd.read_csv(f, header=0 if header else None, usecols=[1, 2, 5, 6], chunksize=2_000_000):
            ch.columns = ['price', 'qty', 'time', 'ibm']
            t = ch['time'].values.astype(np.int64)
            t = np.where(t > 10**14, t // 1000, t)
            m = np.zeros(len(t), bool)
            for a, e in windows:
                m |= (t >= a) & (t <= e)
            if m.any():
                c = ch[m].copy(); c['time'] = t[m]
                c['ibm'] = c['ibm'].astype(str).str.lower().isin(['true', '1'])
                keep.append(c)
    del b, z
    df = pd.concat(keep) if keep else pd.DataFrame(columns=['price', 'qty', 'time', 'ibm'])
    return mk, sym, day, df


def fill(t, p, q, ibm, at, want_ibm, size):
    """touch (size=None) or VWAP over same-side prints from `at` until quote notional >= size."""
    i = np.searchsorted(t, at, side='left')
    idx = np.nonzero(ibm[i:] == want_ibm)[0] + i
    if not len(idx):
        return np.nan, np.nan
    if size is None:
        return p[idx[0]], t[idx[0]] - at
    notl = np.cumsum(p[idx] * q[idx])
    j = np.searchsorted(notl, size)
    if j >= len(idx):
        return np.nan, np.nan
    w = p[idx[:j + 1]] * q[idx[:j + 1]]
    # the last print is only partly used
    over = notl[j] - size
    w_last = w[-1] - over
    qq = q[idx[:j + 1]].astype(float).copy(); qq[-1] = w_last / p[idx[j]]
    vw = (p[idx[:j + 1]] * qq).sum() / qq.sum()
    return vw, t[idx[j]] - at


def last_px(t, p, at):
    i = np.searchsorted(t, at, side='left') - 1
    return p[i] if i >= 0 else np.nan


def main():
    tr = pd.read_csv(sys.argv[1], parse_dates=['t'])
    tr = tr[(tr['exec'] == 'taker') & (tr['lat'] == 0)].reset_index(drop=True)
    if len(sys.argv) > 3 and sys.argv[3] == 'oos_only':
        tr = tr[tr.t >= pd.Timestamp('2025-01-01', tz='UTC')].reset_index(drop=True)
    if len(sys.argv) > 3 and sys.argv[3] == 'is_only':
        tr = tr[tr.t < pd.Timestamp('2025-01-01', tz='UTC')].reset_index(drop=True)
    need = {}
    ev = []
    for _, r in tr.iterrows():
        sig = int((r.t - T0).total_seconds() // 60)
        te = (sig + 1) * 60_000 + T0MS
        tx = (sig + 1 + int(r.hold_min)) * 60_000 + T0MS
        ev.append((r, te, tx))
        for (a, e) in [(te - 90_000, te + 150_000), (tx - 60_000, tx + 150_000)]:
            for d in {pd.Timestamp(a, unit='ms').strftime('%Y-%m-%d'), pd.Timestamp(e, unit='ms').strftime('%Y-%m-%d')}:
                for mk in ['perp', 'spot']:
                    need.setdefault((mk, r.coin, d), []).append((a, e))
    jobs = [(mk, s, d, w) for (mk, s, d), w in sorted(need.items())]
    print(len(jobs), 'files', flush=True)
    data = {}
    with ThreadPoolExecutor(2) as ex:
        for mk, sym, day, df in ex.map(load, jobs):
            data[(mk, sym, day)] = df
            print(mk, sym, day, len(df), flush=True)

    def ticks(mk, sym, t_ms):
        days = sorted({pd.Timestamp(t_ms + d, unit='ms').strftime('%Y-%m-%d') for d in (-90_000, 0, 150_000)})
        parts = [data[(mk, sym, d)] for d in days if (mk, sym, d) in data]
        df = pd.concat(parts).sort_values('time', kind='stable').drop_duplicates()
        return df['time'].values, df['price'].values.astype(float), df['qty'].values.astype(float), df['ibm'].values.astype(bool)

    rows, prof = [], []
    for r, te, tx in ev:
        side = int(r.side)
        tp, pp, qp, bp = ticks('perp', r.coin, te)
        ts, ps, qs, bs = ticks('spot', r.coin, te)
        txp, pxp, qxp, bxp = ticks('perp', r.coin, tx)
        txs, pxs, qxs, bxs = ticks('spot', r.coin, tx)
        for s in [-2, -1, -0.5, -0.1, 0, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30]:
            a = te + int(s * 1000)
            prof.append(dict(coin=r.coin, t=r.t, side=side, sec=s,
                             basis_bp=(last_px(tp, pp, a + 1) / last_px(ts, ps, a + 1) - 1) * 1e4))
        d = dict(coin=r.coin, t=r.t, side=side, dev_bp=r.dev_bp, model_b0_bp=r.b0_bp, model_net_bp=r.net_bp,
                 fund_bp=r.fund_bp, hold_min=r.hold_min,
                 close_basis_bp=(last_px(tp, pp, te) / last_px(ts, ps, te) - 1) * 1e4)
        # quote notional traded on our side in the first second after the boundary
        for nm, (t_, p_, q_, b_, want) in {'perp_in': (tp, pp, qp, bp, side == 1), 'spot_in': (ts, ps, qs, bs, side != 1)}.items():
            m = (t_ >= te) & (t_ < te + 1000) & (b_ == want)
            d[f'notl_1s_{nm}'] = float((p_[m] * q_[m]).sum())
        for lat in LATS:
            a = te + int(lat * 1000); b = tx + int(lat * 1000)
            for sz in [None] + SIZES:
                tag = 'touch' if sz is None else f'w{sz // 1000}k'
                F0, dF = fill(tp, pp, qp, bp, a, side == 1, sz)
                S0, dS = fill(ts, ps, qs, bs, a, side != 1, sz)
                F1, _ = fill(txp, pxp, qxp, bxp, b, side != 1, sz)
                S1, _ = fill(txs, pxs, qxs, bxs, b, side == 1, sz)
                gross = side * ((F0 - F1) + (S1 - S0)) / S0
                fees = FP * F0 / S0 + FS + FP * F1 / S0 + FS * S1 / S0
                d[f'net_{tag}_{lat}'] = (gross - fees) * 1e4 + r.fund_bp
                if sz is None:
                    d[f'b0_{lat}'] = (F0 / S0 - 1) * 1e4
                else:
                    d[f'fillms_{tag}_{lat}'] = max(dF, dS)
        rows.append(d)
    df = pd.DataFrame(rows)
    df.to_csv(f'{OUT}/tick_verify_{sys.argv[2]}.csv', index=False, float_format='%.5g')
    pd.DataFrame(prof).to_csv(f'{OUT}/tick_verify_profile_{sys.argv[2]}.csv', index=False, float_format='%.4g')
    pd.set_option('display.width', 250); pd.set_option('display.max_rows', 200); pd.set_option('display.max_columns', 60)
    df['seg'] = np.where(df.t < pd.Timestamp('2025-01-01', tz='UTC'), 'IS', 'OOS')
    print(df[['coin', 't', 'side', 'dev_bp', 'model_b0_bp', 'close_basis_bp', 'b0_0.0', 'b0_0.1', 'b0_0.2', 'b0_0.5', 'b0_1.0', 'model_net_bp',
              'net_touch_0.0', 'net_touch_0.2', 'net_touch_0.5', 'net_touch_2.0', 'net_w100k_0.5', 'net_w500k_0.5',
              'notl_1s_perp_in', 'notl_1s_spot_in']].round(1).to_string(index=False))
    cols = ['model_net_bp'] + [c for c in df if c.startswith('net_')]
    print(df.groupby('seg')[cols].mean().T.round(1).to_string())


if __name__ == '__main__':
    main()
