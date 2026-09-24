"""Summaries of the three grids (IS selection only) -> results_summary.csv, results.json, printed tables."""
import os, json
import numpy as np, pandas as pd
from common import D

HERE = os.path.dirname(os.path.abspath(__file__))
LEVS = [1, 3, 5, 10, 15, 20]


def load():
    cap = pd.read_parquet(os.path.join(D, 'grid_capture.parquet'))
    dri = pd.read_parquet(os.path.join(D, 'grid_drift.parquet'))
    slo = pd.read_parquet(os.path.join(D, 'grid_slow.parquet'))
    slo['years'] = [json.dumps({**json.loads(a), **json.loads(b)}) for a, b in zip(slo.years_is, slo.years_oos)]
    pc = os.path.join(D, 'grid_capture_pred.parquet'); pd_ = os.path.join(D, 'grid_drift_pred.parquet')
    capp = pd.read_parquet(pc) if os.path.exists(pc) else cap.iloc[:0]
    drip = pd.read_parquet(pd_) if os.path.exists(pd_) else dri.iloc[:0]
    ps = os.path.join(D, 'grid_capture_spot.parquet')
    caps = pd.read_parquet(ps) if os.path.exists(ps) else cap.iloc[:0]
    fams = {'capture(i)[all tradable: btc/none/spot hedge, lag+pred]': pd.concat([cap[cap.sig == 'lag'], capp, caps[caps.sig != 'oracle']], ignore_index=True),
            'capture(i)[spot hedge only, lag+pred]': caps[caps.sig != 'oracle'].copy(),
            'capture_spot_ORACLE(upper bound)': caps[caps.sig == 'oracle'].copy(),
            'capture(i)[lag+pred signals]': pd.concat([cap[cap.sig == 'lag'], capp], ignore_index=True),
            'capture(i)[pred signal only]': capp.copy(),
            'capture_ORACLE(upper bound)': cap[cap.sig == 'oracle'].copy(),
            'drift(ii)[lag+pred signals]': pd.concat([dri, drip], ignore_index=True),
            'slow(iii)': slo.copy()}
    p = os.path.join(D, 'grid_slow_mark.parquet')
    if os.path.exists(p):
        sm = pd.read_parquet(p)
        sm['years'] = [json.dumps({**json.loads(a), **json.loads(b)}) for a, b in zip(sm.years_is, sm.years_oos)]
        fams['slow(iii)_markliq'] = sm
    return fams


def fix_years(js):
    """per-year returns: once an account is ruined (<= -99.99%) within the IS (2022-24) or the OOS (2025-26)
    run, the later years of that run are reported as None (the stored per-year products ignored the ruin)."""
    y = {int(k): v for k, v in json.loads(js).items()}
    out = {}
    for period in ([2022, 2023, 2024], [2025, 2026]):
        dead = False
        for k in period:
            if k not in y:
                continue
            out[k] = None if dead else y[k]
            if y[k] is not None and y[k] <= -0.9999:
                dead = True
    return json.dumps(out)


def cfg_cols(df):
    return [c for c in ['a', 'b', 'dir', 'sig', 'hedge', 'exec', 'universe', 'thr', 'K', 'thr_in', 'thr_out_f', 'side'] if c in df.columns]


def describe(r, cc):
    return ' '.join(f'{c}={r[c]}' for c in cc)


def main():
    fams = load()
    dist, sel = [], []
    for name, df in fams.items():
        cc = cfg_cols(df)
        for L in LEVS:
            x = df[df.L == L]
            dist.append({'family': name, 'L': L, 'configs': len(x), 'is_profitable': int((x.cagr_is > 0).sum()),
                         'oos_profitable': int((x.cagr_oos > 0).sum()), 'both_profitable': int(((x.cagr_is > 0) & (x.cagr_oos > 0)).sum()),
                         'oos_ruined(final<1%)': int((x.final_oos < 0.01).sum()),
                         'median_cagr_is': float(x.cagr_is.median()), 'median_cagr_oos': float(x.cagr_oos.median()),
                         'best_cagr_oos_in_grid': float(x.cagr_oos.max())})
            if 'ORACLE' in name:
                continue
            for rule in ['A:max_IS_CAGR', 'B:max_IS_Sharpe(trades_IS>=100)']:
                xx = x if rule.startswith('A') else x[x.trades_is >= 100]
                if len(xx) == 0:
                    continue
                r = xx.loc[xx.cagr_is.idxmax()] if rule.startswith('A') else xx.loc[xx.sharpe_is.idxmax()]
                sel.append({'family': name, 'rule': rule, 'L': L, 'config': describe(r, cc),
                            'cagr_is': r.cagr_is, 'cagr_oos': r.cagr_oos, 'maxdd_is': r.maxdd_is, 'maxdd_oos': r.maxdd_oos,
                            'worst_day_is': r.worst_day_is, 'worst_day_oos': r.worst_day_oos, 'sharpe_is': r.sharpe_is,
                            'sharpe_oos': r.sharpe_oos, 'liq_is': int(r.liq_is), 'liq_oos': int(r.liq_oos),
                            'trades_is': int(r.trades_is), 'trades_oos': int(r.trades_oos), 'per_year': fix_years(r.years)})
        # fixed config chosen at L=1 by rule B, run at every leverage
        x1 = df[(df.L == 1) & (df.trades_is >= 100)]
        if len(x1) and 'ORACLE' not in name:
            r1 = x1.loc[x1.sharpe_is.idxmax()]
            m = np.ones(len(df), bool)
            for c in cc:
                m &= (df[c] == r1[c]).values
            for _, r in df[m].sort_values('L').iterrows():
                sel.append({'family': name, 'rule': 'C:rule-B config chosen at 1x, all L', 'L': int(r.L), 'config': describe(r, cc),
                            'cagr_is': r.cagr_is, 'cagr_oos': r.cagr_oos, 'maxdd_is': r.maxdd_is, 'maxdd_oos': r.maxdd_oos,
                            'worst_day_is': r.worst_day_is, 'worst_day_oos': r.worst_day_oos, 'sharpe_is': r.sharpe_is,
                            'sharpe_oos': r.sharpe_oos, 'liq_is': int(r.liq_is), 'liq_oos': int(r.liq_oos),
                            'trades_is': int(r.trades_is), 'trades_oos': int(r.trades_oos), 'per_year': fix_years(r.years)})
    dist = pd.DataFrame(dist); sel = pd.DataFrame(sel)
    dist.to_csv(os.path.join(HERE, 'grid_distribution.csv'), index=False)
    sel.to_csv(os.path.join(HERE, 'results_summary.csv'), index=False)
    json.dump({'distribution': dist.to_dict('records'), 'selected': sel.to_dict('records')},
              open(os.path.join(HERE, 'results.json'), 'w'), indent=1, default=float)
    pd.set_option('display.width', 250); pd.set_option('display.max_columns', 30); pd.set_option('display.max_colwidth', 120)
    print(dist.round(3).to_string())
    print(sel.drop(columns=['per_year']).round(3).to_string())


if __name__ == '__main__':
    main()
