import zipfile, io, glob, numpy as np, pandas as pd
D='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/directional/data'
P='/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/directional'
def rz(p):
    with zipfile.ZipFile(p) as z: raw=z.read(z.namelist()[0]).decode()
    hdr = 0 if not raw[0].isdigit() else None
    df=pd.read_csv(io.StringIO(raw), header=hdr)
    return df
for sym in ['BTCUSDT','ETHUSDT']:
    k=pd.concat([rz(p).iloc[:, :6].set_axis(['t','o','h','l','c','v'],axis=1) for p in sorted(glob.glob(f'{D}/*_klines_{sym}_5m_*.zip'))])
    k=k.drop_duplicates('t').set_index('t').sort_index()
    pq=pd.read_parquet(f'{P}/{sym}_5m.parquet').set_index('t')
    j=pq.join(k, rsuffix='_raw', how='left')
    for c in 'ohlc':
        print(sym, c, 'max abs diff parquet vs raw', np.nanmax(np.abs(j[c]-j[c+'_raw'])), 'nan raw', j[c+'_raw'].isna().sum())
    # time units check (ms vs us)
    print(sym, 'first/last t', pd.to_datetime(pq.index[0],unit='ms'), pd.to_datetime(pq.index[-1],unit='ms'), 'n', len(pq), 'step uniq', np.unique(np.diff(pq.index.values)))
    # funding calc_time
    f=pd.concat([rz(p) for p in sorted(glob.glob(f'{D}/*_fundingRate_{sym}_*.zip'))])
    f.columns=['calc_time','interval','rate'][:f.shape[1]]
    ct=f.calc_time.values
    off=(ct % 3600000)
    print(sym,'funding events',len(f),'offset-from-hour ms: min',off.min(),'max',off.max(), 'intervals', np.unique(f.interval))
    hrs=pd.to_datetime(ct,unit='ms').hour
    print(sym,'funding hours', np.unique(hrs, return_counts=True))
    # mark vs last big divergence
    dv = (pq.mh/pq.h-1).abs().max(), (pq.ml/pq.l-1).abs().max()
    print(sym, 'max mark/last high/low divergence', dv)
    # largest 5m ranges in OOS
    ts=pd.to_datetime(pq.index,unit='ms')
    r=(pq.h/pq.o-1); r2=(pq.l/pq.o-1)
    oos=ts>='2025-01-01'
    print(sym,'largest 5m up (open->high) OOS:'); print(pd.DataFrame({'ts':ts[oos],'up':r[oos].values,'dn':r2[oos].values,'mup':(pq.mh/pq.mo-1)[oos].values}).sort_values('up').tail(3).to_string())
    print(sym,'largest 5m down OOS:'); print(pd.DataFrame({'ts':ts[oos],'up':r[oos].values,'dn':r2[oos].values,'mdn':(pq.ml/pq.mo-1)[oos].values}).sort_values('dn').head(3).to_string())
