"""Build aligned hourly panels (2021-12-01 .. 2026-08-31 UTC) from Binance archive zips."""
import os, zipfile, io, glob
import numpy as np, pandas as pd
D = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/data'
SYMS = ['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','BNBUSDT','ADAUSDT','LINKUSDT','AVAXUSDT','LTCUSDT']
KCOLS = ['open_time','open','high','low','close','volume','close_time','qv','n','tb','tq','ig']
def read_zip(p):
    with zipfile.ZipFile(p) as z:
        name = z.namelist()[0]
        raw = z.read(name)
    first = raw.split(b'\n', 1)[0]
    header = not first[:1].isdigit()
    df = pd.read_csv(io.BytesIO(raw), header=0 if header else None)
    return df
def klines(prefix, sym):
    files = sorted(glob.glob(os.path.join(D, prefix.format(s=sym))))
    out = []
    for f in files:
        df = read_zip(f)
        df = df.iloc[:, :6]; df.columns = KCOLS[:6]
        ot = df['open_time'].astype('int64')
        ot = np.where(ot > 1e14, ot // 1000, ot)  # spot 2025+ in microseconds
        df['open_time'] = pd.to_datetime(ot, unit='ms', utc=True)
        out.append(df)
    df = pd.concat(out).drop_duplicates('open_time').set_index('open_time').sort_index()
    return df[['open','high','low','close']].astype(float)
idx = pd.date_range('2021-12-01', '2026-08-31 23:00', freq='h', tz='UTC')
panels = {k: {} for k in ['s_o','s_h','s_l','s_c','f_o','f_h','f_l','f_c','p_h','p_l','p_c','m_h','m_c','i_c','fund']}
for s in SYMS:
    sp = klines('data__spot__monthly__klines__{s}__1h__*.zip', s).reindex(idx)
    fu = klines('data__futures__um__monthly__klines__{s}__1h__*.zip', s).reindex(idx)
    pr = klines('data__futures__um__monthly__premiumIndexKlines__{s}__1h__*.zip', s).reindex(idx)
    mk = klines('data__futures__um__monthly__markPriceKlines__{s}__1h__*.zip', s).reindex(idx)
    ix = klines('data__futures__um__monthly__indexPriceKlines__{s}__1h__*.zip', s).reindex(idx)
    for a, b in [('s', sp), ('f', fu)]:
        for c in 'ohlc':
            panels[f'{a}_{c}'][s] = b[{'o':'open','h':'high','l':'low','c':'close'}[c]]
    panels['p_h'][s] = pr['high']; panels['p_l'][s] = pr['low']; panels['p_c'][s] = pr['close']
    panels['m_h'][s] = mk['high']; panels['m_c'][s] = mk['close']; panels['i_c'][s] = ix['close']
    # funding
    files = sorted(glob.glob(os.path.join(D, f'data__futures__um__monthly__fundingRate__{s}__*.zip')))
    fr = pd.concat([read_zip(f) for f in files])
    fr.columns = [c.strip() for c in fr.columns]
    t = pd.to_datetime(fr['calc_time'].astype('int64'), unit='ms', utc=True).dt.round('h')
    ser = pd.Series(fr['last_funding_rate'].astype(float).values, index=t)
    ser = ser[~ser.index.duplicated()]
    # funding event at time T is paid on positions held at T; attach to the hourly bar whose CLOSE is T (open = T-1h)
    fs = pd.Series(0.0, index=idx)
    ev = ser.copy(); ev.index = ev.index - pd.Timedelta(hours=1)
    ev = ev[ev.index.isin(idx)]
    fs.loc[ev.index] = ev.values
    panels['fund'][s] = fs
    print(s, 'missing spot', int(sp['close'].isna().sum()), 'perp', int(fu['close'].isna().sum()), 'prem', int(pr['close'].isna().sum()), 'fund events', len(ev))
os.makedirs(D + '/panel', exist_ok=True)
for k, v in panels.items():
    pd.DataFrame(v).to_parquet(f'{D}/panel/{k}.parquet')
print('done')

# --- extra: index high/low panel (added) ---
if __name__ == '__main__':
    ih, il = {}, {}
    for s in SYMS:
        ix = klines('data__futures__um__monthly__indexPriceKlines__{s}__1h__*.zip', s).reindex(idx)
        ih[s] = ix['high']; il[s] = ix['low']
    pd.DataFrame(ih).to_parquet(f'{D}/panel/i_h.parquet'); pd.DataFrame(il).to_parquet(f'{D}/panel/i_l.parquet')
