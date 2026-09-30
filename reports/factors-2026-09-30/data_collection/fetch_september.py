"""Public-data acquisition only; no strategy selection, account access, or orders.

Separate September data. Binance OHLC/flow/premium, exact realized OKX funding.
Funding stays NaN at non-events; a separate known mask establishes coverage.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import httpx
import numpy as np
import pandas as pd

from hermes.data.binance_archive import KLINE_COLUMNS, _bar_floor_after, _read_csv_from_zip, _to_utc_ms
from hermes.data.panel import Panel, resample_panel
from hermes.execution.okx.instruments import okx_inst_id

ROOT = Path('artifacts/profitability_2026')
OUT = ROOT / 'september'
OUT.mkdir(parents=True, exist_ok=True)
SYMBOLS = json.loads(Path('artifacts/diagnostic_2026/declaration.json').read_text())['symbols']
START = pd.Timestamp('2026-09-01', tz='UTC')
END = pd.Timestamp('2026-09-30', tz='UTC')  # exclusive price interval, inclusive final settlement
START_MS, END_MS = int(START.timestamp()*1000), int(END.timestamp()*1000)
S3 = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'
OKX = 'https://www.okx.com/api/v5/public/funding-rate-history'
LOCK = threading.Lock()
ABORT = threading.Event()
CLIENT = httpx.Client(timeout=30, follow_redirects=True, limits=httpx.Limits(max_connections=20, max_keepalive_connections=16))


def digest(blob): return hashlib.sha256(blob).hexdigest()


def record(row):
    with LOCK:
        with (OUT / 'requests.jsonl').open('a') as f:
            f.write(json.dumps(row, sort_keys=True)+'\n')


def get(url, params, path):
    if ABORT.is_set(): raise RuntimeError('collection stopped after transport outage')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists(): return path.read_bytes()
    for attempt in range(6):
        now = datetime.now(UTC).isoformat()
        try:
            attempt_url=url
            if '/data/futures/' in url and url.startswith(S3) and attempt%2:
                attempt_url=url.replace(S3,'https://data.binance.vision',1)
            r = CLIENT.get(attempt_url, params=params)
            record({'retrieved_at':now, 'url':str(r.url), 'status':r.status_code, 'bytes':len(r.content), 'sha256':digest(r.content), 'file':str(path), 'attempt':attempt})
            if r.status_code in (403,451): raise RuntimeError(f'access rejected: {r.status_code} {url}')
            if r.status_code==503 and 'remote address:envoy://cloudflare_https_tunnel/' in r.text:
                if attempt==5:
                    ABORT.set()
                    raise RuntimeError('persistent proxy transport outage; stop all downloads')
                time.sleep(min(2**attempt,8))
                continue
            if r.status_code==429 or r.status_code>=500:
                time.sleep(1+attempt)
                continue
            r.raise_for_status()
            path.write_bytes(r.content)
            return r.content
        except httpx.HTTPError as exc:
            if attempt==5: raise
            time.sleep(1+attempt)
    raise RuntimeError(f'fetch exhausted {url}')


def listing(job):
    sym, kind = job
    prefix = f'data/futures/um/daily/{kind}/{sym}/15m/{sym}-15m-2026-09-'
    raw = get(S3, {'prefix':prefix}, OUT/'listings'/kind/f'{sym}.xml')
    tree = ElementTree.fromstring(raw)
    ns = '{http://s3.amazonaws.com/doc/2006-03-01/}'
    if tree.findtext(f'{ns}IsTruncated')=='true': raise ValueError(f'unexpected truncated listing {prefix}')
    keys = [e.text for e in tree.iter(f'{ns}Key') if e.text and e.text.endswith('.zip')]
    keys = [k for k in keys if int(k.rsplit('-',1)[-1][:2])<=29]
    return sym, kind, sorted(keys)


def archive(job):
    sym, kind, key = job
    raw = get(f'{S3}/{key}', {}, OUT/'raw'/key)
    df = _read_csv_from_zip(raw, KLINE_COLUMNS)
    df.index = _to_utc_ms(df['open_time'])
    if not ((df.index>=START)&(df.index<END)).all(): raise ValueError(f'archive outside interval {key}')
    return sym, kind, df.apply(pd.to_numeric, errors='coerce')


def funding(sym):
    inst = okx_inst_id(sym)
    initial=OUT/'funding_raw'/sym/f'{END_MS+1}.json'
    rows, pages, cursor, error = [], [], END_MS+1 if initial.exists() else None, None
    for n in range(12):
        params={'instId':inst,'limit':100}
        if cursor is not None: params['after']=str(cursor)
        stamp=str(cursor) if cursor is not None else 'latest_2026-09-30'
        raw = get(OKX, params, OUT/'funding_raw'/sym/f'{stamp}.json')
        data = json.loads(raw)
        pages.append(digest(raw))
        if data.get('code')!='0':
            error = f"OKX code={data.get('code')} {data.get('msg')}"
            break
        new = data.get('data',[])
        if not new: break
        times=[int(x['fundingTime']) for x in new]
        if cursor is not None and max(times)>=cursor: raise ValueError(f'nonexclusive pagination {sym}')
        if len(set(times))!=len(times): raise ValueError(f'duplicated funding page {sym}')
        rows.extend(new)
        oldest=min(times)
        if oldest<=START_MS: break
        if cursor is not None and oldest>=cursor: raise ValueError(f'nonprogress pagination {sym}')
        cursor=oldest
        time.sleep(0.12)
    df=pd.DataFrame(rows)
    if len(df):
        df['time']=pd.to_datetime(pd.to_numeric(df['fundingTime']),unit='ms',utc=True)
        if df['time'].duplicated().any(): raise ValueError(f'duplicate funding {sym}')
        df['realized_rate']=pd.to_numeric(df['realizedRate'],errors='coerce')
        # Empty realizedRate is unknown; never use an estimate/fundingRate as fallback.
        df=df.sort_values('time')
        df.to_parquet(OUT/'funding_raw'/sym/'events.parquet', index=False)
    coverage={'symbol':sym, 'instId':inst,'source':OKX,'error':error,'events':len(df),'pages':len(pages),'page_sha256':pages,
        'complete_interval':False,'first':None,'last':None,'max_event_gap_hours':None,'missing_realized_rates':None}
    if len(df):
        coverage.update({'first':str(df['time'].iloc[0]), 'last':str(df['time'].iloc[-1]),
            'max_event_gap_hours':float(df['time'].diff().dt.total_seconds().max()/3600),
            'missing_realized_rates':int(df['realized_rate'].isna().sum()),
            'complete_interval': bool(df['time'].iloc[0]<=START and df['time'].iloc[-1]>=END and not df['realized_rate'].isna().any() and df['time'].diff().max()<=pd.Timedelta(hours=8))})
    return sym,df,coverage


def main():
    declaration={'created_at':datetime.now(UTC).isoformat(),'price_start':str(START),'price_end_exclusive':str(END),'last_settlement_inclusive':str(END),'symbols':SYMBOLS,
        'scope':'Public acquisition only. Fixed historical candidate list; all present September daily archives fetched including contracts delisted during September.',
        'prices_premium_source':'Binance public daily archives','funding_source':'OKX public realizedRate history; Binance REST 451 and September monthly funding archive unavailable',
        'mixing':'September isolated. DO NOT silently append to Binance-funded history or claim exact OKX price execution.',
        'funding_missing':'NaN at non-events; funding_known mask identifies covered bars. Entire unknown periods remain NaN. No absent rate replaced with zero.',
        'no_orders':True}
    (OUT/'declaration.json').write_text(json.dumps(declaration,indent=2)+'\n')
    # Check exact boundary mapping before using settled rates.
    mapped = _bar_floor_after(pd.DatetimeIndex([START, END]),'15m')
    assert list(mapped)==[START-pd.Timedelta(minutes=15),END-pd.Timedelta(minutes=15)]
    listings=[]
    with ThreadPoolExecutor(12) as pool:
        futures=[pool.submit(listing,(sym,kind)) for sym in SYMBOLS for kind in ('klines','premiumIndexKlines')]
        for f in as_completed(futures): listings.append(f.result())
    jobs=[(sym,kind,key) for sym,kind,keys in listings for key in keys]
    (OUT/'archive_listing.json').write_text(json.dumps([{'symbol':s,'kind':k,'keys':p} for s,k,p in sorted(listings)],indent=2)+'\n')
    print(json.dumps({'phase':'listed','archives':len(jobs),'symbols_with_bars':sum(bool(p) for s,k,p in listings if k=='klines')}),flush=True)
    funds,coverage={},[]
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(funding,s) for s in SYMBOLS]
        for n,f in enumerate(as_completed(futures),1):
            sym,df,cov=f.result(); funds[sym]=df; coverage.append(cov)
            if n%25==0: print(json.dumps({'phase':'funding','done':n,'total':len(SYMBOLS)}),flush=True)
    (OUT/'funding_coverage.json').write_text(json.dumps(sorted(coverage,key=lambda x:x['symbol']),indent=2)+'\n')
    chunks={sym:{} for sym in SYMBOLS}
    with ThreadPoolExecutor(8) as pool:
        futures=[pool.submit(archive,j) for j in jobs]
        for n,f in enumerate(as_completed(futures),1):
            sym,kind,df=f.result(); chunks[sym].setdefault(kind,[]).append(df)
            if n%500==0: print(json.dumps({'phase':'archives','done':n,'total':len(jobs)}),flush=True)
    full_index=pd.date_range(START,END-pd.Timedelta(minutes=15),freq='15min')
    frames={}
    coverage_map={x['symbol']:x for x in coverage}
    for sym in SYMBOLS:
        parts=chunks[sym].get('klines',[])
        if parts:
            raw=pd.concat(parts).sort_index()
            if raw.index.duplicated().any(): raise ValueError(f'duplicate klines {sym}')
            f=raw.rename(columns={'count':'trades','taker_buy_quote_volume':'taker_buy_quote'})[['open','high','low','close','volume','quote_volume','trades','taker_buy_quote']].reindex(full_index)
        else:
            f=pd.DataFrame(np.nan,index=full_index,columns=['open','high','low','close','volume','quote_volume','trades','taker_buy_quote'])
        premiums=chunks[sym].get('premiumIndexKlines',[])
        f['premium']=pd.concat(premiums).sort_index()['close'].reindex(full_index) if premiums else np.nan
        f['observed_close']=f['close'].notna().astype(float)
        f['funding_rate']=np.nan
        f['funding_known']=0.0
        fd=funds[sym]
        if len(fd):
            # Coverage is bracketed by known actual settlements, then clipped to the month.
            valid = fd['realized_rate'].notna()
            for j in range(1,len(fd)):
                # A >8h hole cannot be declared complete. Rates can move to 1/2/4h; only actual events used.
                a,b=fd.iloc[j-1],fd.iloc[j]
                if valid.iloc[j-1] and valid.iloc[j] and b['time']-a['time']<=pd.Timedelta(hours=8):
                    closes=f.index+pd.Timedelta(minutes=15)
                    f.loc[(closes>a['time'])&(closes<=b['time']),'funding_known']=1.0
            events=pd.Series(fd['realized_rate'].to_numpy(),index=_bar_floor_after(pd.DatetimeIndex(fd['time']),'15m')).groupby(level=0).sum(min_count=1)
            f['funding_rate']=events.reindex(full_index)
        f['funding_okx']=f['funding_rate']
        f['vwap_first']=(f['quote_volume']/f['volume'].where(f['volume']>0)).where(lambda x:(x>=f['low'])&(x<=f['high']))
        frames[sym]=f
        (OUT/'parsed/15m').mkdir(parents=True,exist_ok=True)
        f.to_parquet(OUT/'parsed/15m'/f'{sym}.parquet')
    panel=Panel.from_long(frames,'15m')
    panel.save(OUT/'panel15m')
    panel30=resample_panel(panel,'30m')
    # Unknown half-bars stay unknown after coarsening; both OHLC source bars required for observed.
    panel30.fields['funding_known']=panel['funding_known'].resample('30min').min()
    panel30.fields['observed_close']=panel['observed_close'].resample('30min').min()
    panel30.fields['funding_okx']=panel['funding_okx'].resample('30min').sum(min_count=1)
    panel30.save(OUT/'panel30m')
    hashes={str(p.relative_to(OUT)):digest(p.read_bytes()) for p in sorted(OUT.rglob('*.parquet'))}
    summary={'finished_at':datetime.now(UTC).isoformat(),'archives':len(jobs),'shape_30m':panel30.shape,'start':str(panel30.index[0]),'last_bar':str(panel30.index[-1]),
        'symbols_with_prices':int(panel30['close'].notna().any().sum()),'symbols_funding_complete':sum(x['complete_interval'] for x in coverage),
        'funding_known_fraction_on_observed_bars':float(panel30['funding_known'].where(panel30['observed_close']>0).stack().mean()),
        'unknown_funding_observed_bars':int(((panel30['funding_known']<1)&(panel30['observed_close']>0)).to_numpy().sum()),
        'sha256':hashes,'script_sha256':digest(Path(__file__).read_bytes()),'limitations':['Mixed venue bars and funding. No actual fill costs. Keep September venue change explicit.','Funding is only proven between adjacent realized events <=8h apart. Funding_known=0 must not be priced as zero.','No causally computed venue_listed field; caller must use historical calendar, never current listing status alone.']}
    (OUT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    (OUT/'incomplete_status.json').write_text(json.dumps({'status':'resolved_acquisition_complete','summary':'summary.json','finished_at':summary['finished_at']},indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='sha256'},indent=2),flush=True)

if __name__=='__main__': main()
