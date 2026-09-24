"""Re-price the selected config's taker trades with Binance aggTrades (ms timestamps, aggressor side).
Entry at the signal-minute boundary + latency, exit at the exit-minute boundary + latency, each leg at the
first trade on the side we would hit (sell -> a seller-initiated print = bid; buy -> buyer-initiated = ask).
Fees: taker on both legs (perp 5 bp, spot 10 bp); no extra slippage (touch prices, small size).
Usage: python tick_check.py <trades_csv> <tag>"""
import sys, glob, numpy as np, pandas as pd

OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev_verify/out'
T0 = pd.Timestamp('2021-12-01', tz='UTC'); T0MS = int(T0.value // 1_000_000)
LATS = [0.0, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
FP, FS = 0.0005, 0.0010
_cache = {}


def ticks(mk, sym, t_ms):
    days = {pd.Timestamp(t_ms + d, unit='ms').strftime('%Y-%m-%d') for d in (-130_000, 0, 130_000)}
    parts = []
    for d in sorted(days):
        k = (mk, sym, d)
        if k not in _cache:
            fs = glob.glob(f'{OUT}/ticks/{mk}_{sym}_{d}.parquet')
            _cache[k] = pd.read_parquet(fs[0]) if fs else None
        if _cache[k] is not None:
            parts.append(_cache[k])
    df = pd.concat(parts).sort_values('time', kind='stable')
    return df['time'].values, df['price'].values.astype(float), df['ibm'].values.astype(bool)


def first_px(t, p, ibm, at, want_ibm):
    i = np.searchsorted(t, at, side='left')
    while i < len(t) and ibm[i] != want_ibm:
        i += 1
    return (p[i], t[i] - at) if i < len(t) else (np.nan, np.nan)


def last_px(t, p, at):
    i = np.searchsorted(t, at, side='left') - 1
    return p[i] if i >= 0 else np.nan


def main():
    tr = pd.read_csv(sys.argv[1], parse_dates=['t'])
    tr = tr[(tr['exec'] == 'taker') & (tr['lat'] == 0)].reset_index(drop=True)
    rows, prof = [], []
    for _, r in tr.iterrows():
        sig = int((r.t - T0).total_seconds() // 60)
        te = (sig + 1) * 60_000 + T0MS
        tx = (sig + 1 + int(r.hold_min)) * 60_000 + T0MS
        tp, pp, bp = ticks('perp', r.coin, te)
        ts, ps, bs = ticks('spot', r.coin, te)
        txp, pxp, bxp = ticks('perp', r.coin, tx)
        txs, pxs, bxs = ticks('spot', r.coin, tx)
        side = int(r.side)
        # basis profile around the entry boundary from last prints of each market
        for s in [-30, -5, -1, 0, 1, 2, 5, 10, 30, 60, 90]:
            a = te + int(s * 1000)
            prof.append(dict(coin=r.coin, t=r.t, side=side, sec=s,
                             basis_bp=(last_px(tp, pp, a + 1) / last_px(ts, ps, a + 1) - 1) * 1e4))
        d = dict(coin=r.coin, t=r.t, side=side, dev_bp=r.dev_bp, model_b0_bp=r.b0_bp, model_gross_bp=r.gross_bp,
                 model_net_bp=r.net_bp, fund_bp=r.fund_bp, hold_min=r.hold_min)
        for lat in LATS:
            a = te + int(lat * 1000); b = tx + int(lat * 1000)
            # side +1: sell perp (hit bid: seller-initiated print, ibm=True), buy spot (lift ask: ibm=False)
            F0, dF = first_px(tp, pp, bp, a, side == 1)
            S0, dS = first_px(ts, ps, bs, a, side != 1)
            F1, _ = first_px(txp, pxp, bxp, b, side != 1)
            S1, _ = first_px(txs, pxs, bxs, b, side == 1)
            gross = side * ((F0 - F1) + (S1 - S0)) / S0
            fees = FP * F0 / S0 + FS + FP * F1 / S0 + FS * S1 / S0
            d[f'b0_{lat}'] = (F0 / S0 - 1) * 1e4
            d[f'net_{lat}'] = (gross - fees) * 1e4 + r.fund_bp
            d[f'wait_ms_{lat}'] = max(dF, dS)
        rows.append(d)
    df = pd.DataFrame(rows)
    pr = pd.DataFrame(prof)
    df.to_csv(f'{OUT}/tick_check_{sys.argv[2]}.csv', index=False, float_format='%.4g')
    pr.to_csv(f'{OUT}/tick_profile_{sys.argv[2]}.csv', index=False, float_format='%.4g')
    pd.set_option('display.width', 250); pd.set_option('display.max_rows', 200)
    cols = ['coin', 't', 'side', 'dev_bp', 'model_b0_bp', 'b0_0.0', 'b0_0.5', 'b0_2.0', 'b0_10.0', 'model_net_bp'] + [f'net_{l}' for l in LATS]
    print(df[cols].round(1).to_string(index=False))
    df['seg'] = np.where(df.t < pd.Timestamp('2025-01-01', tz='UTC'), 'IS', 'OOS')
    summ = df.groupby('seg')[['model_net_bp'] + [f'net_{l}' for l in LATS]].mean().round(1)
    print(summ.to_string())
    print('all', df[['model_net_bp'] + [f'net_{l}' for l in LATS]].mean().round(1).to_string())
    print(pr.pivot_table(index=['coin', 't', 'side'], columns='sec', values='basis_bp').round(1).to_string())


if __name__ == '__main__':
    main()
