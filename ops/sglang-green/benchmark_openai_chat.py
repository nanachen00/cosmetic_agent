#!/usr/bin/env python3
from __future__ import annotations
import argparse,concurrent.futures,json,math,statistics,time,urllib.error,urllib.request
from pathlib import Path

def post(url,payload,timeout):
    req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST'); s=time.perf_counter()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: return int(r.status),r.read().decode(errors='replace'),time.perf_counter()-s
    except urllib.error.HTTPError as e: return int(e.code),e.read().decode(errors='replace'),time.perf_counter()-s
    except Exception as e: return None,f'{type(e).__name__}: {e}',time.perf_counter()-s

def pct(v,p):
    if not v:return None
    x=sorted(v); pos=(len(x)-1)*p; lo=math.floor(pos); hi=math.ceil(pos); return x[lo] if lo==hi else x[lo]+(x[hi]-x[lo])*(pos-lo)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--url',required=True); ap.add_argument('--model',default='taide-12b'); ap.add_argument('--vus',type=int,default=1); ap.add_argument('--requests',type=int,default=8); ap.add_argument('--warmups',type=int,default=3); ap.add_argument('--timeout',type=float,default=120); ap.add_argument('--output'); a=ap.parse_args(); endpoint=a.url.rstrip('/')+'/v1/chat/completions'; payload={'model':a.model,'messages':[{'role':'user','content':'請用繁體中文一句話說明玻尿酸的保濕作用。'}],'temperature':0.0,'top_p':1.0,'max_tokens':64}
    for i in range(a.warmups):
        st,b,lat=post(endpoint,payload,a.timeout); print(f'warmup {i+1}: {lat:.4f}s');
        if st!=200: raise SystemExit(f'[ERROR] warmup status={st}: {b[:500]}')
    def one(_):
        st,b,lat=post(endpoint,payload,a.timeout); ct=0
        if st==200:
            try: ct=int((json.loads(b).get('usage') or {}).get('completion_tokens') or 0)
            except Exception: pass
        return {'status':st,'latency_s':lat,'completion_tokens':ct}
    t=time.perf_counter();
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.vus) as ex: rows=list(ex.map(one,range(a.requests)))
    wall=time.perf_counter()-t; ok=[r for r in rows if r['status']==200]; l=[r['latency_s'] for r in ok]; tokens=sum(r['completion_tokens'] for r in ok); sc={}
    for r in rows: sc[str(r['status'])]=sc.get(str(r['status']),0)+1
    rep={'url':a.url,'vus':a.vus,'requests':a.requests,'wall_s':round(wall,4),'status_counts':sc,'success_rate':round(len(ok)/len(rows),4),'success_rps':round(len(ok)/wall,4) if wall else None,'completion_tokens_per_s':round(tokens/wall,4) if wall else None,'latency_s':{'p50':round(pct(l,.5),4) if l else None,'p95':round(pct(l,.95),4) if l else None,'max':round(max(l),4) if l else None,'mean':round(statistics.mean(l),4) if l else None}}
    print(json.dumps(rep,ensure_ascii=False,indent=2));
    if a.output: Path(a.output).write_text(json.dumps(rep,ensure_ascii=False,indent=2))
    return 0 if len(ok)==len(rows) else 1
if __name__=='__main__': raise SystemExit(main())
