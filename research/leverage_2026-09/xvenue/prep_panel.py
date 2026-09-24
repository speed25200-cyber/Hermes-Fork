"""Build the aligned hourly panel (Binance vs OKX) used by the backtest.

Output: data/panel.npz with arrays [T, C]:
  bc, bh, bl         Binance last-price close/high/low (1h bar that ENDS at index time)
  bmh, bml, bmc      Binance mark-price high/low/close
  oc, oh, ol         OKX last-price close/high/low (scaled to Binance contract units, e.g. x1000 for SHIB/PEPE)
  omh, oml, omc      OKX mark-price high/low/close
  fb, fo             funding rate SETTLED at index time (0 if none) on Binance / OKX
  valid              both venues have prices at this hour
Index time t (UTC, hourly) = bar close time. So row t uses the bar that opened at t-1h.
Funding settled at t applies to positions held into t.

OKX funding: per-instrument monthly files (static.okx.com/.../swaprates/monthly/...), whose timestamps equal the
settlement time (verified identical to /api/v5/public/funding-rate-history 'realizedRate' on the overlap).
Gap 2022-01-01..2022-01-16 filled from the older all-swaps daily files, whose 'funding_time' column is the NEXT
settlement time (verified: shift of exactly -8h reproduces the per-instrument series), plus API for the last days.
"""
import os, json, ssl, urllib.request
import numpy as np, pandas as pd
from fetch_data import UNIVERSE, D

MULT = {'SHIB': 1000.0, 'PEPE': 1000.0}  # OKX quotes per-coin, Binance 1000SHIB/1000PEPE
IDX = pd.date_range('2022-01-01 01:00', '2026-09-01 00:00', freq='h', tz='UTC')
T0 = IDX[0]


def to_idx_ms(ms):
    return pd.to_datetime(np.asarray(ms, dtype='int64'), unit='ms', utc=True)


def bars_to_close_index(df, tcol, cols, mult=1.0):
    # bar open time -> index at bar close time (open + 1h)
    t = to_idx_ms(df[tcol].values) + pd.Timedelta(hours=1)
    out = pd.DataFrame({c: df[c].values * mult for c in cols}, index=t)
    out = out[~out.index.duplicated()]
    return out.reindex(IDX)


def okx_api_funding(inst):
    ctx = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
    req = urllib.request.Request(f'https://www.okx.com/api/v5/public/funding-rate-history?instId={inst}&limit=400',
                                 headers={'User-Agent': 'curl/8.0'})
    try:
        d = json.loads(urllib.request.urlopen(req, context=ctx, timeout=30).read())['data']
    except Exception:
        return pd.Series(dtype=float)
    return pd.Series([float(x['realizedRate']) for x in d], index=[int(x['fundingTime']) for x in d])


def funding_series(times_ms, rates):
    t = to_idx_ms(np.round(np.asarray(times_ms, dtype='float64') / 3600000.0).astype('int64') * 3600000)
    s = pd.Series(np.asarray(rates, dtype=float), index=t)
    s = s.groupby(level=0).last()
    return s.reindex(IDX).fillna(0.0)


TEST_MODE = False


def main():
    okx_inst = pd.read_parquet(os.path.join(D, 'okx_funding_inst.parquet'))
    okx_day = pd.read_parquet(os.path.join(D, 'okx_funding_all.parquet'))
    coins, arrs = [], {k: [] for k in ['bc', 'bh', 'bl', 'bmh', 'bml', 'bmc', 'oc', 'oh', 'ol', 'omh', 'oml', 'omc',
                                        'fb', 'fo', 'intb', 'into']}
    for coin, bsym in UNIVERSE:
        paths = [os.path.join(D, 'binance', f'{coin}_klines.parquet'), os.path.join(D, 'binance', f'{coin}_mark.parquet'),
                 os.path.join(D, 'okx', f'{coin}_candles.parquet'), os.path.join(D, 'okx', f'{coin}_mark.parquet')]
        if TEST_MODE and all(os.path.exists(p) for p in paths[:3]) and not os.path.exists(paths[3]):
            paths[3] = paths[2]  # TEST ONLY: OKX last price stands in for OKX mark price
        if not all(os.path.exists(p) for p in paths):
            print('skip (missing data)', coin)
            continue
        bk, bm, ok, om = [pd.read_parquet(p) for p in paths]
        m = MULT.get(coin, 1.0)
        B = bars_to_close_index(bk, 'open_time', ['close', 'high', 'low'])
        BM = bars_to_close_index(bm, 'open_time', ['close', 'high', 'low'])
        O = bars_to_close_index(ok, 'ts', ['close', 'high', 'low'], m)
        OM = bars_to_close_index(om, 'ts', ['close', 'high', 'low'], m)
        # funding
        bf = pd.read_parquet(os.path.join(D, 'binance', f'{coin}_funding.parquet'))
        fb = funding_series(bf.calc_time.values, bf.last_funding_rate.values)
        inst = f'{coin}-USDT-SWAP'
        oi = okx_inst[okx_inst.instId == inst]
        s1 = pd.Series(oi.funding_rate.values, index=oi.funding_time.values)
        od = okx_day[okx_day.instId == inst]
        s0 = pd.Series(od.real_funding_rate.values, index=od.funding_time.values - 8 * 3600000)  # next-time label -> settlement
        s0 = s0[s0.index < (s1.index.min() if len(s1) else 1 << 62)]
        s2 = okx_api_funding(inst)
        s = pd.concat([s0, s1, s2[s2.index > (s1.index.max() if len(s1) else 0)]])
        s = s[~s.index.duplicated(keep='last')].sort_index()
        fo = funding_series(s.index.values, s.values)
        coins.append(coin)
        for k, v in [('bc', B.close), ('bh', B.high), ('bl', B.low), ('bmh', BM.high), ('bml', BM.low), ('bmc', BM.close),
                     ('oc', O.close), ('oh', O.high), ('ol', O.low), ('omh', OM.high), ('oml', OM.low), ('omc', OM.close),
                     ('fb', fb), ('fo', fo)]:
            arrs[k].append(v.values.astype(float))
        # funding interval hours (for info)
        arrs['intb'].append(np.zeros(len(IDX)))
        arrs['into'].append(np.zeros(len(IDX)))
        print(coin, 'B', B.close.notna().sum(), 'O', O.close.notna().sum(), 'fb', (fb != 0).sum(), 'fo', (fo != 0).sum())
    out = {k: np.stack(v, axis=1) for k, v in arrs.items() if k not in ('intb', 'into')}
    valid = np.isfinite(out['bc']) & np.isfinite(out['oc']) & np.isfinite(out['bmc']) & np.isfinite(out['omc'])
    out['valid'] = valid
    out['coins'] = np.array(coins)
    out['time'] = IDX.as_unit('ns').asi8
    np.savez_compressed(os.path.join(D, 'panel_test.npz' if TEST_MODE else 'panel.npz'), **out)
    print('saved', out['bc'].shape)


if __name__ == '__main__':
    import sys
    TEST_MODE = len(sys.argv) > 1 and sys.argv[1] == 'test'
    main()
