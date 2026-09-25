#!/bin/sh
# Pipeline (order matters). Python: /home/user/.venv/bin/python
set -e
PY=/home/user/.venv/bin/python
cd "$(dirname "$0")"
$PY -c "from common import *; p,_=s3_list('data/futures/um/monthly/klines/'); q,_=s3_list('data/futures/um/daily/klines/'); json.dump({'monthly':[x.split('/')[-2] for x in p],'daily':[x.split('/')[-2] for x in q]},open('data/um_symbols.json','w'))"
$PY dl_daily.py            # all USDT perps, 1d klines (listing dates, delisted included)
$PY dl_hourly.py           # 1h klines + funding, first 5 months of each listing since 2021-09, BTC
$PY okx_meta.py            # OKX instruments + position tiers (today)
$PY okx_calendar.py        # OKX point-in-time listing calendar from trade archives
$PY dl_spot.py             # Binance spot first day per base asset
$PY prep.py                # events.parquet + panel.npz
$PY dl_okx_funding.py      # OKX realized funding for event windows
$PY add_okx_funding.py     # -> panel2.npz
$PY dl_okx_candles.py      # OKX 1H candles for contracts still listed
$PY prep_okx_panel.py      # -> panel_okx.npz
$PY dl_funding_all.py      # Binance funding, all perps (age factor)
$PY grid.py binance; $PY grid.py hybrid
$PY evaluate.py binance; $PY evaluate.py hybrid
$PY executability.py; $PY althedge.py; $PY firsthours.py; $PY agefactor.py; $PY book_corr.py; $PY leverage_total.py; $PY verify_pnl.py
$PY summary.py
