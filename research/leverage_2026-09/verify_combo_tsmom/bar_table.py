"""Literal pre-registered bar per candidate, from rerun/results.json (researcher pipeline, reproduced) and
verify_results.json (verifier checks). -> verify_bar.csv"""
import json, pandas as pd
R = json.load(open('rerun/results.json')); V = json.load(open('verify_results.json'))
rows = []
t = R['tsmom']; rb = t['robustness']
rows.append(dict(candidate='TSMOM N30_short_z_hl60_b0.5', sharpe_oos=t['oos']['sharpe'], y2025=t['years']['2025'], y2026=t['years']['2026'],
                 item1=False, lomo_min=rb['lomo_min'], loco_min=rb['loco_min'], stress=rb['stress_cost15_lag1'], item2=False,
                 item3='pass (lot-rounded Sharpe 0.47/0.55/0.53 at 1k/3k/10k, but fails 1-2)',
                 item4_researcher=t['supportable_L'], item4_verifier='1 with hourly trough (DD 31.2%), 0 with daily proxy (35.7%); half-Kelly 1.64',
                 passes=False))
for sc in ['inv_vol_3', 'inv_var_3', 'inv_vol_book_tsmom', 'equal_capital_3', 'book_alone']:
    c = R['combos'][sc]; v = V['V3_V4_item1'][sc]
    rows.append(dict(candidate=f'mix {sc} {c["weights"]}', sharpe_oos=c['sharpe_oos'], y2025=c['y2025'], y2026=c['y2026'],
                     item1=f"numeric {'pass' if v['base']['pass_'] else 'fail'}; OKX funding {v['okx_funding']['sharpe_oos']} "
                           f"({'pass' if v['okx_funding']['pass_'] else 'fail'}); book all-taker {v['okx_funding_book_all_taker']['sharpe_oos']}; "
                           "LITERAL FAIL: book OOS used for its own config choice",
                     lomo_min=c['lomo_min'], loco_min=c['loco_min'], stress=c['stress_cost15_lag1'],
                     item2='FAIL (book leave-one-coin-out and latency not testable)',
                     item3='FAIL/unverified (book fills: fixed 60% maker share, not trade-through)' +
                           ('; book sleeve positions below OKX min order at 1k-3k' if sc == 'inv_vol_3' else ''),
                     item4_researcher=c['supportable_L'], item4_verifier=str(c['supportable_L']) +
                     (' (2 if book intrabar loss on 2025-10-10 >= 4%)' if sc == 'inv_vol_book_tsmom' else
                      ' (2 if >= 8%)' if sc == 'equal_capital_3' else ''),
                     passes=False))
pd.DataFrame(rows).to_csv('verify_bar.csv', index=False)
print(pd.DataFrame(rows)[['candidate', 'sharpe_oos', 'y2025', 'y2026', 'item4_researcher', 'item4_verifier']].to_string())
