import sys, json
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
from sim import *
P = np.load(os.path.join(D, 'panel2.npz')); Q = np.load(os.path.join(D, 'panel_okx.npz'))
ev = pd.read_parquet(os.path.join(D, 'events.parquet'))
inst = {x['instId']: x for x in json.load(open(os.path.join(D, 'okx_instruments.json')))}
tr = pd.read_csv('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting/results/trades_selected_hybrid.csv', parse_dates=['entry_time','exit_time'])
print(tr.columns.tolist()); print(len(tr), (tr.entry_time>='2025-01-01').sum())
rows=[]
for _, t in tr.iterrows():
    i = int(t.i); g0 = (ev.t0.values[i]-G0)//HMS
    a = int(t.entry_t - g0); b = int(min(t.exit_t - g0, 3599))
    bo, qo = P['c'][i, a:b+1], Q['c'][i, a:b+1]
    m = np.isfinite(bo) & np.isfinite(qo)
    ld = np.log(qo[m]/bo[m]) if m.any() else np.array([np.nan])
    nok = np.isfinite(qo).sum(); nbn = np.isfinite(bo).sum()
    r = ev.iloc[i]
    lt = inst.get(r.inst, {}).get('listTime')
    rows.append(dict(sym=t.sym, inst=r.inst, entry=t.entry_time, oos=t.entry_time>=pd.Timestamp('2025-01-01'), ret=t.ret, reason=t.reason,
        n_hours=b-a+1, n_okx=int(nok), n_bn=int(nbn), med_logdiff=float(np.nanmedian(ld)), max_abs_logdiff=float(np.nanmax(np.abs(ld))),
        okx_first=r.okx_first, listTime=pd.to_datetime(int(lt), unit='ms') if lt else None, t0=pd.to_datetime(r.t0, unit='ms')))
X = pd.DataFrame(rows)
X['mixed'] = (X.n_okx>0) & (X.n_okx < X.n_hours)
X['entry_before_listTime'] = X.listTime.notna() & (X.entry < X.listTime)
pd.set_option('display.width', 250)
print('trades with OKX candles:', (X.n_okx>0).sum(), 'mixed source within trade:', X.mixed.sum())
print('max |median log(okx/bn)| across trades:', X.med_logdiff.abs().max())
print(X.sort_values('max_abs_logdiff', ascending=False).head(8)[['sym','entry','n_hours','n_okx','med_logdiff','max_abs_logdiff','ret']].to_string())
print('entry before current OKX listTime:', X.entry_before_listTime.sum())
print(X[X.entry_before_listTime][['sym','inst','entry','t0','okx_first','listTime','ret','oos']].to_string())
print(X[X.mixed][['sym','entry','n_hours','n_okx','n_bn','ret','oos']].to_string())
X.to_csv('trade_source_check.csv', index=False)
