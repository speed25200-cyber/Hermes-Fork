"""Crash-window diagnostics: basis spikes in the data + margin ratio / liquidations of the carry at 10-20x."""
import json, numpy as np, pandas as pd
import carry_sim as cs
OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry'
W = {
    'LUNA May-2022': ('2022-05-07', '2022-05-15'),
    '3AC/Celsius Jun-2022': ('2022-06-10', '2022-06-20'),
    'FTX Nov-2022': ('2022-11-06', '2022-11-15'),
    'Squeeze 14-Jan-2023': ('2023-01-13', '2023-01-16'),
    'USDC/SVB Mar-2023': ('2023-03-09', '2023-03-15'),
    'Yen unwind Aug-2024': ('2024-08-02', '2024-08-08'),
    'Feb-2025 tariff crash': ('2025-02-02', '2025-02-04'),
    'Mar-2025 pump/crash': ('2025-03-02', '2025-03-04'),
    'Apr-2025 tariffs': ('2025-04-06', '2025-04-09'),
    '10-Oct-2025 cascade': ('2025-10-10', '2025-10-12'),
    'Feb-2026 crash': ('2026-02-04', '2026-02-07'),
    'Jun-2026 selloff': ('2026-06-01', '2026-06-06'),
}
P = cs.load()
sel = json.load(open(f'{OUT}/results.json'))
bands = {int(k): v for k, v in sel['selected_always'].items()}
rot = {int(k): v for k, v in sel['selected_rotate'].items()}
mp_c = P['v_f'] / P['v_s'] - 1
series = {}
for L in [5, 10, 15, 20]:
    series[('always', L)] = cs.sim(['BTCUSDT', 'ETHUSDT'], L, band=bands[L], record=True)['series']
    series[('rotate', L)] = cs.sim(cs.ALL, L, mode='rotate', record=True, **rot[L])['series']
rows = []
for name, (a, b) in W.items():
    sl = slice(pd.Timestamp(a, tz='UTC'), pd.Timestamp(b + ' 23:00', tz='UTC'))
    d = dict(window=name, start=a, end=b)
    for c in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT', 'XRPUSDT']:
        k = c[:-4]
        px = P['v_s'][c][sl]
        d[f'{k}_move_%'] = round((px.iloc[-1] / px.iloc[0] - 1) * 100, 1)
        d[f'{k}_max_intrabar_markprem_jump_%'] = round(P['jump_mark'][c][sl].max() * 100, 3)
        d[f'{k}_markprem_close_min_max_%'] = f"{mp_c[c][sl].min()*100:.2f}/{mp_c[c][sl].max()*100:.2f}"
        d[f'{k}_max_raw_premidx_jump_%'] = round(P['jump_raw'][c][sl].max() * 100, 2)
    for (v, L), s in series.items():
        ss = s[sl]
        d[f'{v}_L{L}_min_margin_ratio'] = round(float(ss['mr_worst'].min()), 2) if ss['mr_worst'].notna().any() else None
        e0 = s['E'][:sl.start].iloc[-1] if len(s['E'][:sl.start]) else 1.0
        d[f'{v}_L{L}_window_ret_%'] = round((ss['E'].iloc[-1] / e0 - 1) * 100, 1) if e0 > 0 else None
        d[f'{v}_L{L}_worst_intrabar_%'] = round((ss['Emin'].min() / e0 - 1) * 100, 1) if e0 > 0 else None
    rows.append(d)
df = pd.DataFrame(rows)
df.to_csv(f'{OUT}/crash_windows.csv', index=False)
pd.set_option('display.width', 250)
print(df[['window'] + [c for c in df.columns if c.startswith(('BTC_', 'ETH_'))]].to_string())
print(df[['window'] + [c for c in df.columns if c.startswith(('SOL_', 'DOGE_'))]].to_string())
print(df[['window'] + [c for c in df.columns if c.startswith('always')]].to_string())
print(df[['window'] + [c for c in df.columns if c.startswith('rotate')]].to_string())
