import time, sys
sys.argv = ['run.py', 'main']
import run
import pandas as pd
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 30)
for fam, ci in [('grid', 0), ('grid', 7), ('orb', 0), ('wick', 0)]:
    t = time.time(); rows = run.job((fam, ci)); print(fam, ci, f'{time.time()-t:.1f}s')
    df = pd.DataFrame(rows)
    print(df[['cfg', 'L', 'period', 'cagr', 'maxdd', 'worst_day', 'sharpe', 'trades', 'liq', 'stops']].to_string())
