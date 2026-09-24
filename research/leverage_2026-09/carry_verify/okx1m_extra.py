"""OKX 1m mark-price (SWAP) and index candles around the stress hours, to check the Binance mark/index proxy."""
import ssl, urllib.request, json, time, pandas as pd
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
H = {'User-Agent': 'curl/8.5.0', 'Accept': '*/*'}
W = ['2023-08-17 21:00', '2025-10-10 21:00', '2024-08-05 01:00', '2026-02-07 16:00']
def get(path, inst, t_end_ms, t_start_ms):
    out = []; after = t_end_ms
    while True:
        url = f'https://www.okx.com/api/v5/market/{path}?instId={inst}&bar=1m&limit=100&after={after}'
        for a in range(5):
            try:
                d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=H), context=CTX, timeout=30)); break
            except Exception as e:
                time.sleep(2 + a)
        data = d.get('data', [])
        if not data: break
        out += data
        after = int(data[-1][0])
        time.sleep(0.12)
        if after <= t_start_ms: break
    df = pd.DataFrame([r[:5] for r in out], columns=['t', 'o', 'h', 'l', 'c'])
    df['t'] = pd.to_datetime(df.t.astype('int64'), unit='ms', utc=True)
    return df.drop_duplicates('t').set_index('t').astype(float).sort_index()
rows = []
for w in W:
    c = pd.Timestamp(w, tz='UTC')
    a, b = c - pd.Timedelta(hours=12), c + pd.Timedelta(hours=12)
    for coin in ['BTC', 'ETH']:
        mk = get('history-mark-price-candles', f'{coin}-USDT-SWAP', int(b.value // 1e6), int(a.value // 1e6))
        ix = get('history-index-candles', f'{coin}-USDT', int(b.value // 1e6), int(a.value // 1e6))
        df = pd.DataFrame({'mk_h': mk.h, 'mk_l': mk.l, 'mk_c': mk.c, 'ix_h': ix.h, 'ix_l': ix.l, 'ix_c': ix.c})
        df['coin'] = coin; df['win'] = w
        rows.append(df)
        print(w, coin, len(mk), len(ix), flush=True)
pd.concat(rows).to_parquet('okx_1m_windows_extra.parquet')
