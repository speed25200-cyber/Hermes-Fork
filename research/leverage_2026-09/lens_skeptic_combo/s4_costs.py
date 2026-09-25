"""(5) Cost / capacity realism.
A. Book costs x1.5 from its own daily cost columns (fees+spread+impact), plus 1-bar latency approximated by the report's
   lag-1 Sharpe ratio (1.269/1.363) applied as an exposure-proportional drift cut; combined with the listing sleeve at
   costs x1.5 and (i) researcher lat=1 (stops become next-open exits: flattering), (ii) fair latency (entry/exit delayed
   1 bar, stops still intrabar), (iii) stop slippage 5% and worst-case stop fill at the bar high.
B. Small-account execution of the BOOK sleeve on OKX: min order and lot step (minSz/lotSz * ctVal * price) of the book's
   latest 30-name universe vs its typical position size and per-bar trade size at 1k/3k/10k, scale L*w_book.
-> s4_costs.json"""
import json, os, sys, re, numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'newlisting'))
import sim
from s0_repro import load, sharpe, years, lomo_min
from scipy import optimize

# ---- patched simulator: fair latency + stop slippage (copy of sim.simulate with 2 edits)
src = open(os.path.join(HERE, '..', 'newlisting', 'sim.py')).read()
fn = src[src.index('def simulate('):src.index('def sharpe(')]
fn = fn.replace("min_frac=0.0, record=False):", "min_frac=0.0, record=False, stop_slip=0.0, stop_worst=False, fair_lat=False):")
old = "                    if lat == 0:\n                        fill = max(o_, p['stop_px']) if p['q'] < 0 else min(o_, p['stop_px'])"
assert old in fn
new = ("                    if lat == 0 or fair_lat:\n                        fill = max(o_, p['stop_px']) if p['q'] < 0 else min(o_, p['stop_px'])\n"
       "                        fill = min(h_, fill * (1 + stop_slip)) if p['q'] < 0 else max(l_, fill * (1 - stop_slip))\n"
       "                        if stop_worst: fill = h_ if p['q'] < 0 else l_")
fn = fn.replace(old, new)
ns = dict(sim.__dict__); exec(fn, ns); simulate2 = ns['simulate']
cfg = dict(d0=72, d1=168, side=-1, beta=1.0, stop=0.5, uni='newtok', K=5)
Dt = sim.Data('hybrid')
OOS = slice('2025-01-01', '2026-08-31')
base = simulate2(Dt, cfg, '2025-01-01', '2026-09-01')['ret']
ref = pd.read_csv(os.path.join(HERE, '..', 'newlisting', 'results', 'oos_daily_selected_hybrid.csv'), index_col=0, parse_dates=True)['ret']
assert float((base - ref).abs().max()) < 1e-12, 'patched sim does not reproduce'
NLV = {'base': base,
       'c15_lat0': simulate2(Dt, cfg, '2025-01-01', '2026-09-01', cost_mult=1.5)['ret'],
       'c15_lat1_researcher': simulate2(Dt, cfg, '2025-01-01', '2026-09-01', cost_mult=1.5, lat=1)['ret'],
       'c15_lat1_fair': simulate2(Dt, cfg, '2025-01-01', '2026-09-01', cost_mult=1.5, lat=1, fair_lat=True)['ret'],
       'c15_lat1_fair_stopslip5': simulate2(Dt, cfg, '2025-01-01', '2026-09-01', cost_mult=1.5, lat=1, fair_lat=True, stop_slip=0.05)['ret'],
       'c15_lat1_fair_stopworst': simulate2(Dt, cfg, '2025-01-01', '2026-09-01', cost_mult=1.5, lat=1, fair_lat=True, stop_worst=True)['ret'],
       'stopworst_only': simulate2(Dt, cfg, '2025-01-01', '2026-09-01', stop_worst=True)['ret']}
j = load().dropna(subset=['book', 'nl'])['2023-08-01':'2026-08-31']
b = pd.read_csv('/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv', index_col=0, parse_dates=True)
b.index = b.index.tz_localize(None).normalize()
cost = (b.fees + b.spread + b.impact).reindex(j.index)
book15 = j.book - 0.5 * cost
# latency: exposure-proportional drift cut so the full-period naive Sharpe falls by the report's lag-1 ratio
ratio = 1.2689479439402793 / 1.3633322749880703
tgt = sharpe(j.book) * ratio
k_lat = optimize.brentq(lambda k: sharpe(j.book - k * j.book_gross) - tgt, -0.01, 0.01)
book15_lat = book15 - k_lat * j.book_gross
S1 = json.load(open(os.path.join(HERE, 's1_haircut.json'))); KB = S1['scenarios_k']
W = 0.6030102397652416
out = {'book': dict(sharpe_oos=sharpe(j.book[OOS]), c15_oos=sharpe(book15[OOS]), c15_lat_oos=sharpe(book15_lat[OOS]),
                    c15_full=sharpe(book15), cost_per_year_oos=float(cost[OOS].sum() / (len(cost[OOS]) / 365)), k_lat=k_lat)}
out['listing'] = {k: dict(sharpe=sharpe(v[OOS]), y2025=years(v[OOS])[2025], y2026=years(v[OOS])[2026]) for k, v in NLV.items()}
rows = []
for bn in ('as_is', 'haircut50_0.68', 'dsr_excess'):
    bk = book15_lat - KB[bn] * j.book_gross
    for ln, v in NLV.items():
        c = (W * bk + (1 - W) * v.reindex(j.index).fillna(0))[OOS]
        rows.append(dict(book=bn + '+c15+lat', listing=ln, comb_sharpe=sharpe(c), y2025=years(c)[2025], y2026=years(c)[2026], lomo=lomo_min(c)[0]))
out['combined_stress'] = rows
pd.set_option('display.width', 250)
print(json.dumps(out['book'], indent=1)); print(pd.DataFrame(out['listing']).T.round(3)); print(pd.DataFrame(rows).round(3).to_string(index=False))

# ---- B. OKX lot sizes for the book's universe
sw = json.load(open(os.path.join(HERE, '..', 'okx_swaps.json')))
inst = [x for v in sw.values() if isinstance(v, list) for x in v if isinstance(x, dict) and x.get('instId', '').endswith('-USDT-SWAP')]
I = {x['instId'].replace('-USDT-SWAP', '') : x for x in inst}
U = pd.read_parquet(os.path.join(HERE, '..', 'verify_combo_listings_ib', 'universe_daily.parquet'))
names = U.iloc[-1][U.iloc[-1]].index.tolist()
k = pd.read_parquet(os.path.join(HERE, '..', 'newlisting', 'data', 'daily_klines.parquet'))
last = k.sort_values('t').groupby('sym').c.last()
gross_b = float(b.gross['2025'].mean()); npos = float(b.n_positions['2025'].mean()); turn = float(b.turnover['2025'].sum())
lot = []
for s in names:
    base_ = s[:-4]; mult = 1
    m = re.match(r'^(1000+)(.*)$', base_)
    key = m.group(2) if m else base_
    if m: mult = int(m.group(1))
    x = I.get(key)
    px = last.get(s)
    if x is None or px is None or not np.isfinite(px):
        lot.append(dict(sym=s, okx=x is not None, price=px)); continue
    px_unit = px / mult
    ctv = float(x['ctVal']) * float(x.get('ctMult', 1))
    lot.append(dict(sym=s, okx=True, price=px_unit, ctVal=ctv, lotSz=float(x['lotSz']), minSz=float(x['minSz']),
                    min_notional=float(x['minSz']) * ctv * px_unit, lot_step=float(x['lotSz']) * ctv * px_unit))
L = pd.DataFrame(lot)
print(L.round(4).to_string(index=False))
res = {}
for cap in (1000, 3000, 10000):
    for Lc in (3, 4, 5):
        sc = Lc * W
        pos = cap * sc * gross_b / npos                      # mean position notional (2025 book, active year)
        bar_trade = cap * sc * turn / 365 / 48 / npos         # mean trade per name per 30m bar
        ok = L.dropna(subset=['min_notional'])
        res[f'{cap}_L{Lc}'] = dict(mean_pos=pos, mean_trade_per_name_per_bar=bar_trade,
                                   frac_names_min_order_gt_quarter_pos=float((ok.min_notional > pos / 4).mean()),
                                   frac_names_min_order_gt_bar_trade=float((ok.min_notional > bar_trade).mean()),
                                   median_lot_rounding_pct_of_mean_pos=float((ok.lot_step / 2 / pos).median() * 100),
                                   max_lot_rounding_pct_of_mean_pos=float((ok.lot_step / 2 / pos).max() * 100))
out['book_lots'] = dict(book_gross_2025=gross_b, npos_2025=npos, turnover_2025=turn, names=len(names), okx_listed=int(L.okx.sum()),
                        per_account=res, table=L.to_dict(orient='records'))
print(json.dumps(res, indent=1))
json.dump(out, open(os.path.join(HERE, 's4_costs.json'), 'w'), indent=1, default=float)
