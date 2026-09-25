"""Combined book + listing-ensemble drawdown with the listing sleeve's INTRABAR worst case (hourly, all members' worst
at once) and the book at daily closes (its intrabar path is not available here -> a lower bound on the true intrabar
DD). Listing sleeve at short-leg gross 1 (total 2) x w_list x L, as in combo.py. Also with the listing sleeve's OOS
mean haircut to Sharpe 1.0. -> combo_intrabar.json"""
import sys, json, itertools
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/verify_listings')
from sim_v import *
from multiprocessing import Pool
from ens_robust import SETS
_D = None
def run(c):
    global _D
    if _D is None:
        _D = Data('hybrid2')
    r = simulate(_D, c, OOS_START, OOS_END, L=2.0 / (1 + c['beta']), record=True)
    return r['eq'], r['eq_worst'].fillna(r['eq'])
B = pd.read_csv('/home/user/Hermes/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756/equity_daily.csv', index_col=0)
B.index = pd.to_datetime(B.index).tz_convert(None).normalize()
rb = B['return'].loc['2025-01-01':'2026-08-31']
W = json.load(open('combo.json'))
if __name__ == '__main__':
    out = {}
    with Pool(4) as p:
        for nm, key in (('IS top-10', 'IS top-10 ens (hybrid2)'), ('plateau BTC-hedged (16)', 'plateau BTC-hedged 16 ens (hybrid2)')):
            res = p.map(run, SETS[nm])
            E = pd.concat([a for a, _ in res], axis=1).mean(axis=1)
            Wh = pd.concat([b for _, b in res], axis=1).mean(axis=1)
            Ed = E.resample('D').last()
            prev = Ed.shift(1).fillna(1.0)
            rl = Ed / prev - 1                                   # daily close-to-close
            rl_w = (Wh.resample('D').min() / prev - 1).clip(upper=0)   # worst intraday vs previous close
            wb, wl = W[key]['w_book'], W[key]['w_list']
            idx = rl.index.intersection(rb.index)
            o = {}
            for hc in (None, 1.0):
                r_l, r_lw = rl.reindex(idx), rl_w.reindex(idx)
                if hc is not None:
                    shift = r_l.mean() - hc * r_l.std() / np.sqrt(365)
                    r_l = r_l - shift; r_lw = r_lw - shift
                for L in (2, 3, 4, 5):
                    c = L * (wb * rb.reindex(idx) + wl * r_l)
                    cw = L * (wb * np.minimum(rb.reindex(idx), 0) + wl * r_lw)
                    e = (1 + c).cumprod(); ep = e.shift(1).fillna(1.0)
                    worst = ep * (1 + np.minimum(cw, c))
                    pk = e.cummax().shift(1).fillna(1.0)
                    dd = float((1 - worst / pk).max()); ddc = float((1 - e / e.cummax()).max())
                    o[f'L{L}' + ('' if hc is None else f'_haircut{hc}')] = dict(dd_intrabar_lb=dd, dd_close=ddc, cagr=float(e.iloc[-1] ** (365 / len(c)) - 1),
                                                                             y2026=float(np.prod(1 + c[c.index.year == 2026]) - 1))
            out[nm] = o
            print(nm, {k: (round(v['dd_intrabar_lb'], 3), round(v['dd_close'], 3), round(v['cagr'], 3), round(v['y2026'], 3)) for k, v in o.items()}, flush=True)
    json.dump(out, open('combo_intrabar.json', 'w'), indent=1)
