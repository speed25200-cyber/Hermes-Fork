"""Check OKX REST 1H candle highs against hourly OHLC rebuilt from OKX trade archives for AT and RAVE (stop disagreements)."""
import sys, io, zipfile, ssl, urllib.request
sys.path.insert(0, '/home/user/Hermes/src')
import pandas as pd, numpy as np
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
URL = "https://static.okx.com/cdn/okex/traderecords/trades/daily/{ymd}/{inst}-trades-{day}.zip"
okh = pd.read_parquet('../newlisting/data/okx_h1.parquet')
for inst, sym, entry in (('AT-USDT-SWAP', 'ATUSDT', '2025-11-01 11:00'), ('RAVE-USDT-SWAP', 'RAVEUSDT', '2025-12-17 15:00')):
    e = pd.Timestamp(entry); x = e + pd.Timedelta(hours=96)
    parts = []
    for d in pd.date_range((e + pd.Timedelta(hours=8)).normalize(), (x + pd.Timedelta(hours=8)).normalize()):
        u = URL.format(ymd=d.strftime('%Y%m%d'), inst=inst, day=d.strftime('%Y-%m-%d'))
        b = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'curl/8.0'}), context=CTX, timeout=180).read()
        z = zipfile.ZipFile(io.BytesIO(b)); df = pd.read_csv(z.open(z.namelist()[0]), usecols=['trade_id', 'price', 'size', 'created_time'])
        parts.append(df)
    df = pd.concat(parts).sort_values(['created_time', 'trade_id'])
    df['t'] = (df.created_time // 3600000) * 3600000
    g = df.groupby('t').agg(o=('price', 'first'), h=('price', 'max'), l=('price', 'min'), c=('price', 'last'))
    r = okh[okh.sym == sym].set_index('t')[['o', 'h', 'l', 'c']].sort_index()
    ent_ms = int(e.value // 10**6)
    po = r.loc[ent_ms, 'o']; stop = po * 1.5
    w = r.loc[ent_ms:ent_ms + 96 * 3600000]
    hits = w[w.h >= stop]
    print(inst, 'REST entry open', po, 'stop', stop, 'REST hours hitting stop', len(hits))
    for t in hits.index[:3]:
        tt = pd.Timestamp(t, unit='ms')
        sub = df[(df.t == t)]
        n_above = (sub.price >= stop).sum(); q_above = sub.loc[sub.price >= stop, 'size'].sum()
        print('  ', tt, 'REST h', hits.loc[t, 'h'], '| archive h', g.loc[t, 'h'] if t in g.index else None, '| trades >= stop:', int(n_above), 'contracts', q_above)
    print('  archive max over window', g.loc[ent_ms:ent_ms + 96 * 3600000, 'h'].max(), 'REST max', w.h.max())
