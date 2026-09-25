#!/bin/sh
# Reproduce the diversified-TSMOM / combination study (order matters). Python: /home/user/.venv/bin/python
set -e
PY=/home/user/.venv/bin/python
cd "$(dirname "$0")"
$PY prep.py               # daily panel (Binance UM daily OHLCV, OKX point-in-time calendar, OKX specs)
$PY fund_fix.py           # complete Binance funding for all universe symbol-months, rewrite panel F
$PY grid.py               # 72-config pre-registered TSMOM grid, IS selection -> tsmom_selected.json
$PY carry_stream.py       # BTC+ETH 1x carry via round-1 simulator (+ LOCO, cost x1.5)
$PY okx_funding_btceth.py # OKX BTC/ETH funding (static monthly files) for the carry sensitivity
$PY evaluate.py           # success bar: TSMOM alone, combinations, leverage, sensitivities -> results.json
$PY check_1010.py         # worst-day intrabar check with OKX hourly mark prices
$PY summarize.py          # results_summary.csv
