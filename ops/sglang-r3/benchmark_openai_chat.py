#!/usr/bin/env python3
from __future__ import annotations
import argparse, concurrent.futures, json, math, statistics, time, urllib.error, urllib.request
from pathlib import Path

def post(url, payload, timeout):
    req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),headers={"Content-Type":"application/json"},method="POST")
    t0=time.perf_counter()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return r.status,r.read().decode(errors="replace"),time.perf_counter()-t0
    except urllib.error.HTTPError as e:
        return e.code,e.read().decode(errors="replace"),time.perf_counter()-t0
    except Exception as e:
        return None,f"{type(e).__name__}: {e}",time.perf_counter()-t0

def pct(xs,p):
    xs=sorted(xs)
    if not xs:return None
    if len(xs)==1:return xs[0]
    k=(len(xs)-1)*p
    lo,hi=math.floor(k),math.ceil(k)
    return xs[lo] if lo==hi else xs[lo]+(xs[hi]-xs[lo])*(k-lo)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--url",required=True)
    ap.add_argument("--model",default="taide-12b")
    ap.add_argument("--vus",type=int,default=1)
    ap.add_argument("--requests",type=int,default=8)
    ap.add_argument("--warmups",type=int,default=3)
    ap.add_argument("--timeout",type=float,default=120)
    ap.add_argument("--output")
    a=ap.parse_args()
    endpoint=a.url.rstrip("/")+"/v1/chat/completions"
    payload={"model":a.model,"messages":[{"role":"user","content":"請用繁體中文一句話說明玻尿酸的保濕作用。"}],"temperature":0.0,"top_p":1.0,"max_tokens":64}
    for i in range(a.warmups):
        s,b,l=post(endpoint,payload,a.timeout)
        print(f"warmup {i+1}: status={s} latency_s={l:.4f}")
        if s!=200: raise SystemExit(f"warmup failed: {b[:1000]}")
    def one(_):
        s,b,l=post(endpoint,payload,a.timeout)
        ct=0
        if s==200:
            try: ct=int((json.loads(b).get("usage") or {}).get("completion_tokens") or 0)
            except Exception: pass
        return {"status":s,"latency_s":l,"completion_tokens":ct}
    t0=time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.vus) as ex:
        rows=list(ex.map(one,range(a.requests)))
    wall=time.perf_counter()-t0
    ok=[x for x in rows if x["status"]==200]
    l=[x["latency_s"] for x in ok]
    sc={}
    for x in rows:sc[str(x["status"])]=sc.get(str(x["status"]),0)+1
    toks=sum(x["completion_tokens"] for x in ok)
    out={
      "url":a.url,"vus":a.vus,"requests":a.requests,"wall_s":round(wall,4),"status_counts":sc,
      "success_rate":round(len(ok)/len(rows),4) if rows else 0,
      "success_rps":round(len(ok)/wall,4) if wall else None,
      "completion_tokens_per_s":round(toks/wall,4) if wall else None,
      "latency_s":{"p50":round(pct(l,.5),4) if l else None,"p95":round(pct(l,.95),4) if l else None,"mean":round(statistics.mean(l),4) if l else None,"max":round(max(l),4) if l else None}
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    if a.output:Path(a.output).write_text(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if len(ok)==len(rows) else 1

if __name__=="__main__":
    raise SystemExit(main())
