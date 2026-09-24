"""Collect final IS/OOS results into results/summary.csv and results/summary.json."""
import os, json
import numpy as np, pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
R = os.path.join(BASE, 'results')


def table(fname, label):
    df = pd.read_csv(os.path.join(R, fname))
    base = df[df.variant == 'base'] if 'variant' in df else df
    out = []
    for L in sorted(base.L.unique()):
        a = base[(base.L == L) & (base.period == 'IS')].iloc[0]
        b = base[(base.L == L) & (base.period == 'OOS')].iloc[0]
        row = dict(family=label, L_per_venue=L, L_per_leg_over_total_equity=L / 2,
                   cagr_IS=a.cagr, cagr_OOS=b.cagr, maxdd_IS=a.maxdd, maxdd_OOS=b.maxdd,
                   worst_day_IS=a.worst_day, worst_day_OOS=b.worst_day, sharpe_IS=a.sharpe, sharpe_OOS=b.sharpe,
                   liq_IS=a.liq, liq_OOS=b.liq, cuts_IS=a.cuts, cuts_OOS=b.cuts,
                   entries_IS=a.entries, entries_OOS=b.entries, fills_IS=a.fills, fills_OOS=b.fills,
                   transfers_IS=a.transfers, transfers_OOS=b.transfers,
                   funding_IS=a.funding_pct, funding_OOS=b.funding_pct,
                   trading_costs_IS=a.fees_pct + a.slip_pct, trading_costs_OOS=b.fees_pct + b.slip_pct)
        for y in ['2022', '2023', '2024']:
            row['ret_' + y] = a.get('y' + y, np.nan)
        for y in ['2025', '2026']:
            row['ret_' + y] = b.get('y' + y, np.nan)
        cfg = {k: a[k] for k in ['R', 'k_dl', 'delta', 'H', 'th_in', 'th_out', 'K', 'universe', 'balance', 'sp_in',
                                  'sp_out', 'sp_maxhold'] if k in a and pd.notna(a[k])}
        row['config'] = json.dumps({k: (v.item() if hasattr(v, 'item') else v) for k, v in cfg.items()})
        out.append(row)
    return pd.DataFrame(out)


def sens(fname):
    df = pd.read_csv(os.path.join(R, fname))
    piv = df.pivot_table(index=['L', 'variant'], columns='period', values=['cagr', 'maxdd', 'liq', 'cuts'])
    return piv


if __name__ == '__main__':
    parts = [table('final_runs.csv', 'funding_diff_carry')]
    if os.path.exists(os.path.join(R, 'spread_final_runs.csv')):
        parts.append(table('spread_final_runs.csv', 'price_divergence_reversion'))
    S = pd.concat(parts, ignore_index=True)
    S.to_csv(os.path.join(R, 'summary.csv'), index=False)
    S.to_json(os.path.join(R, 'summary.json'), orient='records', indent=1)
    pd.set_option('display.width', 250)
    cols = ['family', 'L_per_venue', 'cagr_IS', 'cagr_OOS', 'maxdd_IS', 'maxdd_OOS', 'worst_day_IS', 'worst_day_OOS',
            'sharpe_IS', 'sharpe_OOS', 'liq_IS', 'liq_OOS', 'cuts_IS', 'cuts_OOS', 'entries_IS', 'entries_OOS',
            'ret_2022', 'ret_2023', 'ret_2024', 'ret_2025', 'ret_2026']
    print(S[cols].round(4).to_string())
    print(S[['family', 'L_per_venue', 'config']].to_string())
    sv = sens('final_runs.csv')
    sv.to_csv(os.path.join(R, 'sensitivities.csv'))
    print(sv.round(4).to_string())
