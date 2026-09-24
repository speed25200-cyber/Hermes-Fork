import re, ssl, urllib.request, json, concurrent.futures as cf
ctx = ssl.create_default_context(cafile='/root/.ccr/ca-bundle.crt')
def ls(prefix):
    keys=[]; marker=''
    while True:
        url=f"https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix={prefix}&delimiter=/"+(f"&marker={marker}" if marker else '')
        s=urllib.request.urlopen(url, context=ctx, timeout=60).read().decode()
        k=re.findall(r'<Key>(.*?)</Key>',s); keys+=k
        if '<IsTruncated>true' in s and k: marker=k[-1]
        else: break
    return [x for x in keys if x.endswith('.zip')]
syms=[f"{a}USDT_{d}" for a in ['BTC','ETH'] for d in ['211231','220325','220624','220930','221230','230331','230630','230929','231229','240329','240628','240927','241227','250328','250627','250926','251226','260327','260626','260925','261225']]
out={}
def job(s):
    r={}
    for kind in ['klines','markPriceKlines']:
        r[kind]=[k.split('-1m-')[1][:7] for k in ls(f"data/futures/um/monthly/{kind}/{s}/1m/")]
    r['daily_klines']=[k.split('-1m-')[1][:10] for k in ls(f"data/futures/um/daily/klines/{s}/1m/")]
    return s,r
with cf.ThreadPoolExecutor(8) as ex:
    for s,r in ex.map(job,syms): out[s]=r
json.dump(out,open('listing.json','w'),indent=0)
for s in syms:
    r=out[s]; d=r['daily_klines']
    print(s,'kl',r['klines'][:1],r['klines'][-1:],len(r['klines']),'| mk',r['markPriceKlines'][:1],r['markPriceKlines'][-1:],len(r['markPriceKlines']),'| daily',d[:1],d[-1:],len(d))
