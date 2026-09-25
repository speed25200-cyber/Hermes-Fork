"""Leverage grid of the combined account: L in {1,2,3,4,5,8} x book intrabar model x price source, OKX tier MMR
(coins 2% = median tier-1 of live new listings, BTC 0.4%, book names 2%), plus stresses. -> levgrid.json, levgrid.csv"""
import json
from combo import *
LEVS = [1, 2, 3, 4, 5, 8]
MODELS = ['close', 'S1', 'S3', 'C', 'B', 'A']
out, rows = {}, []
for price in ('hybrid2', 'hybrid'):
    Dt = Data(price)
    for m in MODELS:
        for L in LEVS:
            s = summ(combo_sim(Dt, L, m))
            rows.append(dict(price=price, model=m, L=L, **{k: v for k, v in s.items() if k not in ('years', 'parts')},
                             y2025=s['years'].get(2025), y2026=s['years'].get(2026), part_list=s['parts'].get('listing_intrabar'),
                             part_book=s['parts'].get('book_trough')))
    # stresses on hybrid2 / model C and close
    if price == 'hybrid2':
        ST = {'mmr_coin5_btc05': dict(mmr_coin=0.05, mmr_btc=0.005), 'mmr_all5': dict(mmr_coin=0.05, mmr_btc=0.005, mmr_book=0.05),
              'stopslip5': dict(stop_slip=0.05), 'stopworst': dict(stop_worst=True), 'cost15_lat1': dict(cost_mult=1.5, lat=1)}
        for nm, kw in ST.items():
            for m in ('close', 'C', 'B'):
                for L in LEVS:
                    s = summ(combo_sim(Dt, L, m, **kw))
                    rows.append(dict(price=price + ':' + nm, model=m, L=L, **{k: v for k, v in s.items() if k not in ('years', 'parts')},
                                     y2025=s['years'].get(2025), y2026=s['years'].get(2026), part_list=s['parts'].get('listing_intrabar'),
                                     part_book=s['parts'].get('book_trough')))
df = pd.DataFrame(rows)
df.to_csv('levgrid.csv', index=False)
pd.set_option('display.width', 250)
print(df[['price', 'model', 'L', 'sharpe', 'cagr', 'y2025', 'y2026', 'daily_maxdd', 'ib_maxdd', 'liq', 'liq_time', 'maxdd_time', 'part_list', 'part_book', 'im_use_max']].to_string(index=False))
