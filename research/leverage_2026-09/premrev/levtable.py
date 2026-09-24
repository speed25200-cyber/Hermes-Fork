"""Leverage tables for trade lists re-priced with tick data (Binance aggTrades, OKX prints) and for the
OKX-native scan. K=1 (one open pair at a time across BTC/ETH), notional per leg = L x equity,
equity x (1 + L*net) per trade; worst intrabar equity = 1 + L*worst (worst per notional incl. entry fees);
idealised portfolio margin (m_pm = 1.4% of notional, maintenance) liquidation: equity after =
max(0, worst equity - (2% penalty + 0.4% MMR + 0.05% fee) x notional). USDT borrow interest ignored (holds are
minutes to hours) is charged: for the Binance-timestamp variants it is taken from the 1m engine
(L*net - ret_L of the same trade: USDT borrow at the OKX hourly rate, min 1 hour); for the OKX-native scan it is
(L-1) x OKX USDT rate x ceil(hours)/8760.
Writes out/levtables.csv and prints them."""
import numpy as np, pandas as pd

OUT = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/premrev/out'
LEVS = [1, 3, 5, 10, 15, 20]
M_PM, PEN = 0.014, 0.02 + 0.004 + 0.0005
IS_A, IS_B, OOS_B = pd.Timestamp('2022-01-01', tz='UTC'), pd.Timestamp('2025-01-01', tz='UTC'), pd.Timestamp('2026-09-01', tz='UTC')
USDT_RATE = '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/carry/data/okx_usdt_lending_rate_hourly.csv'
YRS = {'IS': 3.0, 'OOS': (OOS_B - IS_B).days / 365.25}


def k1(df):
    df = df.sort_values(['t_entry', 'coin'], kind='stable').reset_index(drop=True)   # ties: BTC first (as the grid)
    keep, last = [], None
    for i, r in df.iterrows():
        if last is not None and r.t_entry <= last:
            continue
        keep.append(i); last = r.t_exit
    return df.loc[keep]


def run(df, variant, worst_col_for_L=None, adj_col=None):
    rows = []
    df = k1(df)
    for seg, a, b in [('IS', IS_A, IS_B), ('OOS', IS_B, OOS_B)]:
        d = df[(df.t_entry >= a) & (df.t_entry < b)]
        days = pd.date_range(a, b - pd.Timedelta(days=1), freq='D')
        for L in LEVS:
            E, peak, mdd, liq = 1.0, 1.0, 0.0, 0
            daily = pd.Series(np.nan, index=days)
            for _, r in d.iterrows():
                w = r[worst_col_for_L.format(L=L)] if worst_col_for_L else L * r.worst
                weq = 1 + w
                if weq < M_PM * L:
                    liq += 1
                    mult = max(0.0, weq - PEN * L)
                else:
                    mult = 1 + L * r.net - (r[adj_col.format(L=L)] if adj_col else 0.0)
                mdd = min(mdd, E * max(weq, 0) / peak - 1)
                E = E * max(mult, 0.0)
                mdd = min(mdd, E / peak - 1); peak = max(peak, E)
                daily[r.t_exit.floor('D')] = E
            daily = daily.ffill().fillna(1.0)
            ret = daily.pct_change().fillna(daily.iloc[0] - 1)
            yrs = {y: float(daily[daily.index.year == y].iloc[-1] / (daily[daily.index.year < y].iloc[-1] if (daily.index.year < y).any() else 1.0) - 1)
                   for y in sorted(set(daily.index.year))}
            rows.append(dict(variant=variant, seg=seg, L=L, trades=len(d), cagr=E ** (1 / YRS[seg]) - 1 if E > 0 else -1.0,
                             maxdd=mdd, liqs=liq, worst_day=float(ret.min()),
                             sharpe=float(ret.mean() / ret.std() * np.sqrt(365)) if ret.std() > 0 else 0.0,
                             mean_net_bp=float(d.net.mean() * 1e4) if len(d) else np.nan,
                             years=';'.join(f'{y}:{v:+.4f}' for y, v in yrs.items())))
    return rows


def main():
    rows = []
    tr = pd.read_csv(f'{OUT}/trades_sel_btceth_k80.csv', parse_dates=['t'])
    tr = tr[(tr['exec'] == 'taker') & (tr['lat'] == 0)].copy()
    tr['t_entry'] = tr.t + pd.Timedelta(minutes=1)
    tr['t_exit'] = tr.t_entry + pd.to_timedelta(tr.hold_min, unit='min')
    for L in LEVS:
        tr[f'int{L}'] = L * tr.net_bp / 1e4 - tr[f'pm_ret{L}']        # borrow interest (>= 0) from the engine
    base = tr[['coin', 't', 't_entry', 't_exit'] + [f'pm_worst{L}' for L in LEVS] + [f'int{L}' for L in LEVS]].copy()
    # (A) 1m-bar model (Binance), for reference (same numbers as the grid's pm rows)
    a = base.copy(); a['net'] = tr.net_bp.values / 1e4
    rows += run(a, 'binance_1m_model_taker', 'pm_worst{L}', 'int{L}')
    # (B) Binance ticks, (C) OKX prints at the same timestamps
    bt = pd.read_csv(f'{OUT}/tick_check_sel.csv', parse_dates=['t'])
    ok = pd.read_csv(f'{OUT}/okx_tick_check_sel.csv', parse_dates=['t'])
    for lat in [0.5, 2.0, 10.0]:
        b = base.merge(bt[['coin', 't', f'net_{lat}']], on=['coin', 't'])
        b['net'] = b[f'net_{lat}'] / 1e4
        rows += run(b, f'binance_ticks_lat{lat}s', 'pm_worst{L}', 'int{L}')
        o = base.merge(ok[['coin', 't', f'okx_net_{lat}']], on=['coin', 't'])
        o['net'] = o[f'okx_net_{lat}'].fillna(0) / 1e4
        rows += run(o, f'okx_same_timestamps_lat{lat}s', 'pm_worst{L}', 'int{L}')
    # (D) OKX-native scan: k chosen on IS (2022-2024) by mean net per trade at 0.5 s, then OOS
    sc = pd.read_csv(f'{OUT}/okx_scan_trades.csv', parse_dates=['t', 't_exit'])
    sc = sc.dropna(subset=['net_bp'])
    sc['t_entry'] = sc.t; sc['net'] = sc.net_bp / 1e4
    sc['worst'] = (sc.worst_bp.fillna(0).clip(upper=0) - sc.fee_in_bp) / 1e4
    r = pd.read_csv(USDT_RATE, parse_dates=['ts']).drop_duplicates('ts').set_index('ts')['rate'].sort_index()
    rate = r.reindex(sc.t.dt.floor('h'), method='ffill').values
    hours = np.ceil(sc.hold_s / 3600).clip(lower=1)
    for L in LEVS:
        sc[f'int{L}'] = max(L - 1, 0) * rate * hours / 8760
    for lat in sorted(sc.lat.unique()):
        for k in sorted(sc.k_bp.unique()):
            d = sc[(sc.lat == lat) & (sc.k_bp == k)]
            rows += run(d, f'okx_native_k{int(k)}_lat{lat}s', None, 'int{L}')
    out = pd.DataFrame(rows)
    out.to_csv(f'{OUT}/levtables.csv', index=False, float_format='%.5g')
    pd.set_option('display.width', 250); pd.set_option('display.max_rows', 500)
    piv = out.pivot_table(index=['variant', 'L'], columns='seg', values=['cagr', 'maxdd', 'liqs', 'trades', 'mean_net_bp'])
    print(piv.round(3).to_string())


if __name__ == '__main__':
    main()
