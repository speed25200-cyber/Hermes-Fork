"""(d) Squeeze tail. Event level (every new-token listing OKX lists at entry, short from +d0 to +168h, hourly OKX-first
prices): max adverse excursion (MAE = max high / entry open - 1), worst trades, and what a stop at s costs vs saves
versus holding to day 7 (ignoring the BTC hedge; stop fill at the stop level = optimistic). -> tail.json"""
import json, numpy as np, pandas as pd
X = pd.read_parquet('events_d7.parquet')
out = {}
for d0 in (24, 72):
    for per, m in (('IS', X.t < '2025-01-01'), ('OOS', X.t >= '2025-01-01'), ('ALL', X.t.notna())):
        Y = X[(X.d0 == d0) & m].copy()
        q = Y.mae.quantile([.5, .75, .9, .95, .99]).to_dict()
        d = dict(n=len(Y), mae_q={str(k): float(v) for k, v in q.items()}, mae_max=float(Y.mae.max()),
                 p_mae_gt={str(s): float((Y.mae > s).mean()) for s in (0.1, 0.25, 0.5, 1.0, 2.0)},
                 end_loss_q={'p5_short_raw': float(Y.short_raw.quantile(0.05)), 'min_short_raw': float(Y.short_raw.min())})
        st = {}
        for s in (0.25, 0.5, 1.0):
            hit = Y.mae >= s
            nostop = Y.short_raw
            withstop = np.where(hit, -s, Y.short_raw)
            saved = Y.short_raw[hit] < -s   # would have ended worse than the stop
            st[str(s)] = dict(n_hit=int(hit.sum()), frac_hit=float(hit.mean()), n_saved=int(saved.sum()), n_cost=int((~saved).sum()),
                              mean_ret_nostop=float(nostop.mean()), mean_ret_stop=float(withstop.mean()),
                              stop_effect_per_trade=float(withstop.mean() - nostop.mean()),
                              sum_saved=float((-s - Y.short_raw[hit][saved]).sum()), sum_cost=float((Y.short_raw[hit][~saved] + s).sum()),
                              sd_nostop=float(nostop.std()), sd_stop=float(np.std(withstop, ddof=1)))
        d['stops'] = st
        out[f'd0={d0}_{per}'] = d
        print(f"\nd0={d0} {per} n={len(Y)} MAE quantiles {({k: round(v, 3) for k, v in d['mae_q'].items()})} max {d['mae_max']:.2f}")
        print('   P(MAE>x)', {k: round(v, 3) for k, v in d['p_mae_gt'].items()}, 'worst end short raw', round(d['end_loss_q']['min_short_raw'], 3), 'p5', round(d['end_loss_q']['p5_short_raw'], 3))
        for s, v in st.items():
            print(f"   stop {s}: hit {v['n_hit']} ({v['frac_hit']:.1%}), saved {v['n_saved']} cost {v['n_cost']}, mean/trade nostop {v['mean_ret_nostop']:+.4f} stop {v['mean_ret_stop']:+.4f} "
                  f"(effect {v['stop_effect_per_trade']:+.4f}); sd {v['sd_nostop']:.3f}->{v['sd_stop']:.3f}; sum saved {v['sum_saved']:.3f} sum cost {v['sum_cost']:.3f}")
for d0 in (72, 24):
    Y = X[X.d0 == d0].sort_values('mae', ascending=False)
    print(f'\nworst 12 by MAE d0={d0}:'); print(Y.head(12)[['sym', 't', 'mae', 'short_raw', 'short_btc', 'alt30']].round(3).to_string(index=False))
    out[f'worst_mae_d0={d0}'] = Y.head(12)[['sym', 't', 'mae', 'short_raw', 'short_btc']].astype({'t': str}).to_dict('records')
    Z = X[X.d0 == d0].sort_values('short_raw')
    print(f'worst 8 by final short return (no stop) d0={d0}:'); print(Z.head(8)[['sym', 't', 'mae', 'short_raw']].round(3).to_string(index=False))
    out[f'worst_end_d0={d0}'] = Z.head(8)[['sym', 't', 'mae', 'short_raw']].astype({'t': str}).to_dict('records')
json.dump(out, open('tail.json', 'w'), indent=1, default=str)
