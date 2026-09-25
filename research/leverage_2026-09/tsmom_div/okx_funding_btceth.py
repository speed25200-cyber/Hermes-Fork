"""OKX realized funding for BTC/ETH USDT swaps, 2021-12..2026-08, from OKX static monthly files
(static.okx.com/cdn/okex/traderecords/swaprates/monthly/{yyyymm}/{inst}-fundingrates-{yyyy-mm}.zip).
-> okx_funding_btceth.csv (daily sums per holding day, same day convention as the panel) and the daily adjustment
for the 1x carry stream: +0.5 * sum_coin (okx - binance) (short perp receives funding, 50/50 BTC/ETH notional)."""
import io, ssl, time, zipfile, urllib.request
import pandas as pd, numpy as np
from concurrent.futures import ThreadPoolExecutor
SP = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad'
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')


def get(url, tries=5):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'curl/8.0'}), context=CTX,
                                        timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1 + k)
        except Exception:
            time.sleep(1 + k)
    return None


jobs = [(inst, str(pd.Period('2021-12', 'M') + k)) for inst in ['BTC-USDT-SWAP', 'ETH-USDT-SWAP'] for k in range(57)]


def month(job):
    inst, m = job
    b = get(f'https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/{m.replace("-", "")}/{inst}-fundingrates-{m}.zip')
    if b is None:
        return job, None
    z = zipfile.ZipFile(io.BytesIO(b))
    df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    df.columns = ['instId', 'rate', 'funding_time']
    return job, df


with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(month, jobs))
miss = [j for j, d in res if d is None]
print('files', sum(d is not None for _, d in res), 'missing', miss)
o = pd.concat([d for _, d in res if d is not None], ignore_index=True).drop_duplicates(['instId', 'funding_time'])
o['d'] = pd.to_datetime(o.funding_time - 300000, unit='ms', utc=True).dt.normalize()
od = o.groupby(['d', 'instId']).rate.sum().unstack()
f = pd.read_parquet(f'{SP}/tsmom_div/funding_all.parquet')
f = f[f.sym.isin(['BTCUSDT', 'ETHUSDT'])]
f['d'] = pd.to_datetime(f.t - 300000, unit='ms', utc=True).dt.normalize()
bd = f.groupby(['d', 'sym']).rate.sum().unstack()
print('binance coverage', {c: (bd[c].first_valid_index(), bd[c].last_valid_index()) for c in bd})
out = pd.DataFrame({'okx_BTC': od.get('BTC-USDT-SWAP'), 'okx_ETH': od.get('ETH-USDT-SWAP'),
                    'bin_BTC': bd.get('BTCUSDT'), 'bin_ETH': bd.get('ETHUSDT')})
out.to_csv(f'{SP}/tsmom_div/okx_funding_btceth.csv')
print(out.groupby(out.index.year).mean() * 365)
