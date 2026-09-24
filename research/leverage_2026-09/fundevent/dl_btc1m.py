"""BTCUSDT perp 1m klines 2021-12 .. 2026-08 (hedge leg) -> data/btc_1m.parquet"""
import os
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from common import D, get_archive, parse_klines

months = [f'{y}-{m:02d}' for y in range(2021, 2027) for m in range(1, 13) if (2021, 12) <= (y, m) <= (2026, 8)]


def one(mo):
    return parse_klines(get_archive(f'data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{mo}.zip'))


with ThreadPoolExecutor(8) as ex:
    parts = list(ex.map(one, months))
df = pd.concat([p for p in parts if p is not None]).drop_duplicates('t').sort_values('t').reset_index(drop=True)
df.to_parquet(os.path.join(D, 'btc_1m.parquet'))
print(len(df), pd.to_datetime(df.t.iloc[0], unit='ms'), pd.to_datetime(df.t.iloc[-1], unit='ms'),
      'gaps>1m:', int((df.t.diff() > 60000).sum()))
