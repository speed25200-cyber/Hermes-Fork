import pandas as pd, numpy as np
e = pd.read_csv('okx_ev_evidence2.csv')
lab = {'MET-USDT-SWAP': 'PRE-MARKET confirmed (preMktSwTime 2025-10-23 14:30; OKX ann. 2025-10-23; spot +318h; CoinGecko first 2025-10-23)',
       'RE-USDT-SWAP': 'PRE-MARKET confirmed (preMktSwTime 2026-06-18 14:20; OKX ann. 2026-06-18; spot +27.5h; CoinGecko first 2026-06-18)',
       'TESTPM002-USDT-SWAP': 'OKX test pre-market contract (3 bars, never traded)',
       'GRAM-USDT-SWAP': 'NOT A LAUNCH: Toncoin renamed (TON-USDT-SWAP last trade 2026-06-16 08:00 @1.695; GRAM first 06-17 10:30 @1.670; Gate "GRAM (prev. Toncoin)")',
       'UP-USDT-SWAP': 'spot later on CEX (Gate +68h) but CoinGecko prices from 2026-03-14 -> token existed; not pre-market',
       'HYPE-USDT-SWAP': 'CEX spot later on OKX/Bybit but Gate spot -1441h; not pre-market',
       'MOODENG-USDT-SWAP': 'OKX spot +1037h but Gate -138h; not pre-market',
       'CAT-USDT-SWAP': 'OKX spot +1176h; Bybit CATUSDT -8088h (possibly other CAT); DEX token; not a pre-market contract (uncertain)',
       'AI16Z-USDT-SWAP': 'Bybit spot +407h; no other CEX; DEX-traded token (knowledge, not verified here)',
       'BUZZ-USDT-SWAP': 'no CEX spot found; DEX token (knowledge, not verified here)',
       'LAUNCHCOIN-USDT-SWAP': 'no CEX spot found; DEX token (knowledge, not verified here)',
       'PROS-USDT-SWAP': 'OKX spot +356h, CoinGecko Pharos first 2026-04-28 (= t0 day) -> not pre-market; newtok=False only via ticker collision with Binance PROS (Prosper, 2022)',
       'DATA-USDT-SWAP': 'Gate "DATA Network" -47.5h; newtok=False via ticker collision with Binance DATA (Streamr, 2018)'}
default = pd.Series(np.where(e.gap_h <= 0, 'spot existed at/before perp start', 'check'), index=e.index)
e['verdict'] = e.sym.map(lab).fillna(default)
e[['sym', 't0_exact', 'cls', 'newtok', 'gate_name', 'g_okx_spot', 'g_bybit_10th', 'g_gate_start', 'g_bn_spot_day', 'g_okx_preMktSw', 'gap_h', 'verdict']].to_csv('okx_premarket_classification.csv', index=False)
print(e.verdict.value_counts().to_string())
b = pd.read_csv('bn_ev_evidence2.csv'); a = pd.read_csv('bn_pm_anchor.csv')
b['premarket'] = b.i.isin(a.i)
b = b.merge(a[['i', 'tge', 'tge_h']], on='i', how='left')
b[['i', 'sym', 't0', 'gate_name', 'g_bn_spot', 'g_okx_spot', 'g_bybit_10th', 'g_gate_start', 'g_okx_preMktSw', 'gap_h', 'premarket', 'tge', 'tge_h']].to_csv('bn_premarket_classification.csv', index=False)
print('binance traded pre-market events', b.premarket.sum(), 'of', len(b))
