"""Fully funded BTC+ETH cash-and-carry at 1x (long spot = short perp = equity; no borrowing), re-run with the
round-1 simulator (read-only import from ../carry, same settings as its 'i_always_BTCETH' L=1 row: band 0.2, OKX
borrow series, mark stress, taker fees). Output: daily return and daily intrabar trough (from hourly Emin) for
  base            : BTC+ETH, OKX VIP0 taker fees + 2 bp slippage per leg
  cost15          : same with fees and slippage x1.5
  btc_only/eth_only: leave-one-coin-out
Also exports the OKX USDT hourly borrow rate aggregated to a daily cost rate (for sleeves levered above 1x)."""
import sys
import numpy as np, pandas as pd
SP = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad'
sys.path.insert(0, f'{SP}/carry')
import carry_sim as cs


def run(coins):
    r = cs.sim(coins, 1, mode='always', band=0.2, start='2022-01-01', end='2026-08-31 23:00', record=True)
    s = r['series']
    print(coins, {k: v for k, v in r.items() if k in ('cagr', 'maxdd', 'sharpe', 'liquidations', 'fees', 'interest')})
    Ed = s['E'].resample('D').last()
    prev = Ed.shift(1).fillna(1.0)
    ret = Ed / prev - 1
    ib = s['Emin'].resample('D').min() / prev - 1
    return ret, np.minimum(ib, np.minimum(ret, 0))


out = {}
out['ret'], out['ib'] = run(['BTCUSDT', 'ETHUSDT'])
out['ret_btc'], out['ib_btc'] = run(['BTCUSDT'])
out['ret_eth'], out['ib_eth'] = run(['ETHUSDT'])
cs.FEE_SPOT *= 1.5; cs.FEE_PERP *= 1.5
cs.SLIP = {k: v * 1.5 for k, v in cs.SLIP.items()}
out['ret_cost15'], out['ib_cost15'] = run(['BTCUSDT', 'ETHUSDT'])
out = pd.DataFrame(out)
b = pd.read_csv(f'{SP}/carry/data/okx_usdt_lending_rate_hourly.csv', parse_dates=['ts']).set_index('ts')['rate']
b = b[~b.index.duplicated()]
out['borrow_daily'] = (b.resample('D').mean().reindex(out.index).ffill().bfill() / 365.0)
out.to_csv(f'{SP}/tsmom_div/carry_daily.csv')
x = out.ret
print('carry 1x: IS sharpe', x['2022':'2024'].mean() / x['2022':'2024'].std() * np.sqrt(365),
      'OOS sharpe', x['2025':].mean() / x['2025':].std() * np.sqrt(365), 'OOS ann', x['2025':].mean() * 365)
