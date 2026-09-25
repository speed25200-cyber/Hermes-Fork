"""Exact launch time (first trade) and hourly last-price OHLC for the OKX-listing events, plus OKX BTC-USDT-SWAP 1H.
Live contracts: REST history-candles 1H; first trade from the first archive day. Delisted contracts: 1H OHLC rebuilt
from the daily trade archive (UTC+8 days) over [t0, t0 + 9 days].
-> data/ev_h1.parquet (instId, t, o, h, l, c, src), data/ev_t0.csv (instId, t0, first_day8), data/btc_h1.parquet"""
import sys
sys.path.insert(0, "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xlist")
from common import *
from concurrent.futures import ThreadPoolExecutor

ea = pd.read_csv(os.path.join(D, 'okx_events_a.csv'))
ea = ea[ea.cls != 'bn_covered'] if '--all' not in sys.argv else ea
HOUR = 3600000


def first_trade(inst, approx):
    """First archive day (UTC+8) around an approximate first time, and the first trade's timestamp."""
    d = day8(approx)
    for k in range(-4, 3):
        day = d + pd.Timedelta(days=k)
        b = get(trades_url(inst, day))
        if b is not None:
            tr = read_trades(b)
            if len(tr):
                return day, int(tr.t.min()), tr
    return None, None, None


def ohlc(tr):
    tr = tr.sort_values('t')
    h = (tr.t // HOUR) * HOUR
    g = tr.groupby(h).px
    return pd.DataFrame({'t': g.first().index.astype('int64'), 'o': g.first().values, 'h': g.max().values,
                         'l': g.min().values, 'c': g.last().values})


def rest_h1(inst, a, b):
    rows, after = [], b + HOUR
    for _ in range(60):
        d = okx_get(f'/api/v5/market/history-candles?instId={inst}&bar=1H&limit=100&after={after}')
        if not d:
            break
        rows += d
        after = int(d[-1][0])
        if after <= a:
            break
    if not rows:
        return None
    df = pd.DataFrame([[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in rows],
                      columns=['t', 'o', 'h', 'l', 'c'])
    return df[(df.t >= a) & (df.t <= b)].drop_duplicates('t').sort_values('t')


def one(r):
    inst = r.instId
    day, t0, tr = first_trade(inst, r.okx_first)
    if t0 is None:
        return inst, None, None
    end = t0 + 9 * 24 * HOUR
    live = str(r.live) == 'True'
    df = rest_h1(inst, (t0 // HOUR) * HOUR, end) if live else None
    src = 'rest'
    if df is None or len(df) < 24:
        parts = [ohlc(tr)]
        for k in range(1, 11):
            b = get(trades_url(inst, day + pd.Timedelta(days=k)))
            if b is not None:
                parts.append(ohlc(read_trades(b)))
        df = pd.concat(parts).groupby('t').agg(o=('o', 'first'), h=('h', 'max'), l=('l', 'min'), c=('c', 'last')).reset_index()
        df = df[df.t <= end]
        src = 'trades'
    df['instId'], df['src'] = inst, src
    return inst, dict(instId=inst, t0=t0, first_day8=str(day.date())), df


with ThreadPoolExecutor(6) as ex:
    res = list(ex.map(one, [r for _, r in ea.iterrows()]))
t0s = pd.DataFrame([m for _, m, _ in res if m])
h1 = pd.concat([df for _, _, df in res if df is not None], ignore_index=True)
miss = [i for i, m, _ in res if m is None]
suffix = '_all' if '--all' in sys.argv else ''
t0s.to_csv(os.path.join(D, f'ev_t0{suffix}.csv'), index=False)
h1.to_parquet(os.path.join(D, f'ev_h1{suffix}.parquet'))
print('events', len(ea), 'with t0', len(t0s), 'missing', miss, 'bars', len(h1), h1.groupby('src').instId.nunique().to_dict(), flush=True)
if not os.path.exists(os.path.join(D, 'btc_h1.parquet')):
    a, b = pd.Timestamp('2021-12-25').value // 10 ** 6, pd.Timestamp('2026-09-25 04:00').value // 10 ** 6
    chunks = []
    for s in range(a, b, 90 * 24 * HOUR):
        chunks.append(rest_h1('BTC-USDT-SWAP', s, min(s + 90 * 24 * HOUR - HOUR, b)))
    btc = pd.concat(chunks).drop_duplicates('t').sort_values('t')
    btc.to_parquet(os.path.join(D, 'btc_h1.parquet'))
    print('btc bars', len(btc), pd.to_datetime(btc.t.min(), unit='ms'), pd.to_datetime(btc.t.max(), unit='ms'))
