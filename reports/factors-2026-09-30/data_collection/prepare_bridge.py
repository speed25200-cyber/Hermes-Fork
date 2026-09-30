"""Separate tail linking historical panel to September, with explicitly mixed funding venue."""
from pathlib import Path
import hashlib, json
import numpy as np
import pandas as pd
from hermes.data.panel import Panel, resample_panel
from hermes.data.binance_archive import _bar_floor_after
from fetch_september import OUT, SYMBOLS

start=pd.Timestamp('2026-08-31 20:00',tz='UTC')
end=pd.Timestamp('2026-09-01',tz='UTC')
index=pd.date_range(start,end-pd.Timedelta(minutes=15),freq='15min')
old=Path('artifacts/diagnostic_2026')
venue=pd.read_parquet(old/'panel/venue_listed.parquet').iloc[-1]
frames={}
source_hashes={}
for sym in SYMBOLS:
    p=old/'data/parsed/15m'/f'{sym}.parquet'
    fields=['open','high','low','close','volume','quote_volume','trades','taker_buy_quote','premium']
    if p.exists():
        source_hashes[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
        f=pd.read_parquet(p).reindex(index)[fields]
    else: f=pd.DataFrame(np.nan,index=index,columns=fields)
    f['observed_close']=f['close'].notna().astype(float)
    f['funding_rate']=np.nan; f['funding_known']=0.0
    fp=OUT/'funding_raw'/sym/'events.parquet'
    if fp.exists():
        fd=pd.read_parquet(fp)
        source_hashes[str(fp)]=hashlib.sha256(fp.read_bytes()).hexdigest()
        # The acquisition exhausted FET history; its unknown bridge remains unknown.
        events=pd.Series(fd['realized_rate'].to_numpy(),index=_bar_floor_after(pd.DatetimeIndex(fd['time']),'15m')).groupby(level=0).sum(min_count=1)
        f['funding_rate']=events.reindex(index)
        for j in range(1,len(fd)):
            a,b=fd.iloc[j-1],fd.iloc[j]
            if pd.notna(a['realized_rate']) and pd.notna(b['realized_rate']) and b['time']-a['time']<=pd.Timedelta(hours=8):
                closes=f.index+pd.Timedelta(minutes=15)
                f.loc[(closes>a['time'])&(closes<=b['time']),'funding_known']=1.0
    f['funding_okx']=f['funding_rate']
    f['vwap_first']=(f['quote_volume']/f['volume'].where(f['volume']>0)).where(lambda x:(x>=f['low'])&(x<=f['high']))
    # Existing historical venue calendar for the very same Aug31 UTC day.
    f['venue_listed']=float(venue.get(sym,0))
    frames[sym]=f
panel=Panel.from_long(frames,'15m')
panel.save(OUT/'bridge15m')
p30=resample_panel(panel,'30m')
p30.fields['funding_known']=panel['funding_known'].resample('30min').min()
p30.fields['observed_close']=panel['observed_close'].resample('30min').min()
p30.fields['funding_okx']=panel['funding_okx'].resample('30min').sum(min_count=1)
p30.save(OUT/'bridge30m')
m={'purpose':'Explicit 8-bar bridge after historical last bar Aug31 19:30; prices/premium from existing Binance raw parsed monthly archives, realized funding from OKX.',
'start':str(p30.index[0]),'last_bar':str(p30.index[-1]),'shape':p30.shape,'historical_source_unchanged':True,
'funding_known_fraction_on_observed':float(p30['funding_known'].where(p30['observed_close']>0).stack().mean()),'source_sha256':source_hashes,
'output_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (OUT/'bridge30m').glob('*.parquet')},
'unknowns':'Funding unknown stays NaN with funding_known=0. Known non-settlement bars are NaN with funding_known=1; caller may use zero only on that mask.',
'venue_listed':'Carried existing historical calendar Aug31 same UTC date, no carry across dates.'}
(OUT/'bridge_manifest.json').write_text(json.dumps(m,indent=2)+'\n')
print(json.dumps({k:v for k,v in m.items() if 'sha256' not in k},indent=2))
