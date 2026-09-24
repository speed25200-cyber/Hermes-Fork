import pandas as pd, json
df = pd.read_csv('results_all.csv')
out = []
for (cfg, cost, scope, mode, L), d in df.groupby(['config', 'cost', 'scope', 'mode', 'L']):
    i = d[d.period == 'IS'].iloc[0]; o = d[d.period == 'OOS'].iloc[0]; f = d[d.period == 'FULL'].iloc[0]
    out.append(dict(config=cfg, cost=cost, scope=scope, mode=mode, L=L,
                    cagr_IS=round(i.cagr * 100, 2), cagr_OOS=round(o.cagr * 100, 2), maxdd_IS=round(i.maxdd * 100, 1), maxdd_OOS=round(o.maxdd * 100, 1),
                    sharpe_IS=round(i.sharpe, 2), sharpe_OOS=round(o.sharpe, 2), worstday_IS=round(i.worst_day * 100, 1), worstday_OOS=round(o.worst_day * 100, 1),
                    liq_IS=int(i.nliq), liq_OOS=int(o.nliq), entries_IS=int(i.nentry), rolls_IS=int(i.nroll), entries_OOS=int(o.nentry), rolls_OOS=int(o.nroll),
                    margin_rebalances_OOS=int(o.nrb),
                    **{f'y{y}_fullrun': round(f[f'y{y}'] * 100, 1) for y in range(2022, 2027)}))
s = pd.DataFrame(out)
s.to_csv('results_summary.csv', index=False)
main = s[(s.cost.isin(['base', 'maker_fut'])) & (s.scope == 'BTC+ETH')]
print(main[(main.config.isin(['short_B', 'short_A', 'two_B'])) & (main['mode'] == 'cross')].to_string(index=False))
print(len(s), 'rows saved to results_summary.csv')
