"""Replace the Binance mark/index path with OKX's own 1m mark (BTC/ETH-USDT-SWAP) and index (BTC/ETH-USDT) in 16 stress windows (+-12h),
then re-run the always-on BTC+ETH carry at 8-20x (Binance 1m elsewhere)."""
import sys
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry_verify')
import carry_sim_v as cv, pandas as pd, numpy as np
P = cv.load(); idx = P['s_c'].index
M1 = cv.load_m1()
o = pd.read_parquet('okx_1m_windows.parquet')
okx = {}
cmp_rows = []
for w, g in o.groupby('win'):
    b = g[g.coin == 'BTC'].drop(columns=['coin', 'win']); e = g[g.coin == 'ETH'].drop(columns=['coin', 'win'])
    b = b[~b.index.duplicated()]; e = e[~e.index.duplicated()]
    for t_h in pd.date_range(b.index.min().ceil('h') + pd.Timedelta(hours=1), b.index.max().floor('h') - pd.Timedelta(hours=1), freq='h'):
        mins = pd.date_range(t_h, periods=60, freq='min')
        prev = t_h - pd.Timedelta(minutes=1)
        try:
            bb = b.loc[mins]; ee = e.loc[mins]; bp = b.loc[prev]; ep = e.loc[prev]
        except KeyError:
            continue
        if bb.isna().any().any() or ee.isna().any().any():
            continue
        t = idx.get_loc(t_h)
        d = {k: np.stack([bb[k.replace('mk_', 'mk_').replace('ix_', 'ix_')].values, ee[k].values], axis=1) for k in ['mk_c', 'mk_h', 'ix_c', 'ix_h']}
        d['mk_prev'] = np.array([bp.mk_c, ep.mk_c]); d['ix_prev'] = np.array([bp.ix_c, ep.ix_c])
        okx[t] = d
        # premium comparison (max over minute closes), OKX vs Binance, relative to previous hour close premium
        for j, c in enumerate(['BTC', 'ETH']):
            po = (d['mk_c'][:, j] / d['ix_c'][:, j] - 1).max() - (d['mk_prev'][j] / d['ix_prev'][j] - 1)
            pb_prev = P['m_c'][c + 'USDT'].iloc[t - 1] / P['i_c'][c + 'USDT'].iloc[t - 1] - 1
            pb = (M1['mk_c'][t][:, j] / M1['ix_c'][t][:, j] - 1).max() - pb_prev
            cmp_rows.append(dict(win=w, hour=t_h, coin=c, okx_jump_bp=po * 1e4, bn_jump_bp=pb * 1e4, bn_proxy_bp=P['jump_mark'][c + 'USDT'].iloc[t] * 1e4))
cmp = pd.DataFrame(cmp_rows)
cmp.to_csv('okx_vs_binance_premium_jumps.csv', index=False)
print('hours with OKX 1m data:', len(okx))
print(cmp.sort_values('okx_jump_bp', ascending=False).head(15).round(1).to_string())
print(cmp.sort_values('bn_jump_bp', ascending=False).head(10).round(1).to_string())
print('corr okx vs bn 1m jump', cmp[['okx_jump_bp', 'bn_jump_bp', 'bn_proxy_bp']].corr().round(2).to_string())
bands = {8: 0.2, 10: 0.2, 15: 0.2, 20: 0.05}
rows = []
for L in [8, 10, 15, 20]:
    for per, (a, b_) in {'IS': ('2022-01-01', '2024-12-31 23:00'), 'OOS': ('2025-01-01', '2026-08-31 23:00')}.items():
        for src in ['binance1m', 'okx_in_windows']:
            r = cv.sim(['BTCUSDT', 'ETHUSDT'], L, band=bands[L], start=a, end=b_, intrabar='1m', okx=(okx if src != 'binance1m' else None), record=True)
            s = r['series']
            win_mr = s['mr_worst'].iloc[[i for i in range(len(s)) if (idx.get_loc(s.index[i]) in okx)]]
            rows.append(dict(L=L, per=per, src=src, cagr=round(r['cagr'], 4), maxdd=round(r['maxdd'], 4), liq=r['liquidations'], liq_dates=r['liq_dates'],
                             min_mr_in_windows=round(float(win_mr.min()), 2), at=str(win_mr.idxmin())[:13]))
df = pd.DataFrame(rows); df.to_csv('okx_window_check.csv', index=False); print(df.to_string())
