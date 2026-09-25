"""Bybit perp and spot listing calendars from public.bybit.com directory listings (delisted symbols included)."""
import re, ssl, json, urllib.request, time, sys
from concurrent.futures import ThreadPoolExecutor
CTX = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def get(u, tries=6):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'curl/8'}), context=CTX, timeout=60) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 404: return ''
            time.sleep(1 + k)
        except Exception:
            time.sleep(1 + k)
    return None
out = {}
for kind in ('trading', 'spot'):
    top = get(f'https://public.bybit.com/{kind}/')
    syms = re.findall(r'href="([A-Z0-9]+)/"', top)
    def dates(s):
        x = get(f'https://public.bybit.com/{kind}/{s}/')
        if x is None: return s, None
        ds = sorted(set(re.findall(r'(\d{4}-\d{2}-\d{2})', x)))
        return s, (ds[0], ds[-1], len(ds)) if ds else None
    with ThreadPoolExecutor(32) as ex:
        res = dict(ex.map(dates, syms))
    out[kind] = res
    print(kind, len(syms), sum(v is None for v in res.values()), flush=True)
json.dump(out, open('bybit_calendar.json', 'w'))
