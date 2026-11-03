#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,time,urllib.error,urllib.request
from pathlib import Path

def get_json(url,timeout):
    started=time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request(url,method='GET'),timeout=timeout) as r: return int(r.status),json.loads(r.read().decode()),time.perf_counter()-started
    except Exception as e: return None,{'error':f'{type(e).__name__}: {e}'},time.perf_counter()-started

def post_json(url,payload,timeout):
    req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST'); started=time.perf_counter()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r: return int(r.status),r.read().decode(errors='replace'),time.perf_counter()-started
    except urllib.error.HTTPError as e: return int(e.code),e.read().decode(errors='replace'),time.perf_counter()-started
    except Exception as e: return None,f'{type(e).__name__}: {e}',time.perf_counter()-started

def validate(case,content):
    rule=case.get('validation',{'type':'nonempty'}); typ=rule.get('type')
    if typ=='exact_normalized':
        ok=' '.join(content.split()).upper()==str(rule['value']).upper(); return ok,None if ok else f"expected {rule['value']!r}"
    if typ=='nonempty':
        ok=bool(content.strip()); return ok,None if ok else 'empty content'
    if typ=='json_contains_ingredient':
        try: parsed=json.loads(content)
        except Exception as e: return False,f'invalid JSON: {e}'
        vals=parsed.get('ingredients'); ok=isinstance(vals,list) and rule['value'] in vals; return ok,None if ok else f'missing ingredient; got {vals!r}'
    return False,f'unknown validation type {typ!r}'

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--url',required=True); ap.add_argument('--model',default='taide-12b'); ap.add_argument('--cases',default=str(Path(__file__).with_name('taide_generate_cases.json'))); ap.add_argument('--warmups',type=int,default=3); ap.add_argument('--timeout',type=float,default=120); ap.add_argument('--output'); a=ap.parse_args()
    base=a.url.rstrip('/'); models=base+'/v1/models'; chat=base+'/v1/chat/completions'
    st,body,lat=get_json(models,min(a.timeout,15)); print('===== /v1/models ====='); print(json.dumps({'status':st,'latency_s':round(lat,4),'body':body},ensure_ascii=False,indent=2))
    if st!=200: return 2
    ids=[x.get('id') for x in body.get('data',[]) if isinstance(x,dict)] if isinstance(body,dict) else []
    if ids and a.model not in ids: print(f'[ERROR] expected model {a.model!r}, got {ids!r}'); return 2
    warm={'model':a.model,'messages':[{'role':'user','content':'只回答 OK'}],'temperature':0.0,'top_p':1.0,'max_tokens':8}; wl=[]
    print(f'\n===== WARM-UP x{a.warmups} =====')
    for i in range(a.warmups):
        s,b,e=post_json(chat,warm,a.timeout); wl.append(e); print(f'warmup {i+1}: status={s} latency_s={e:.4f}')
        if s!=200: print(b[:1000]); return 3
    cases=json.loads(Path(a.cases).read_text(encoding='utf-8')); results=[]; failed=False
    for case in cases:
        payload={'model':a.model,'messages':case['messages'],'temperature':case.get('temperature',0.0),'top_p':1.0,'max_tokens':case.get('max_new_tokens',64)}
        s,b,e=post_json(chat,payload,a.timeout); row={'name':case['name'],'status':s,'latency_s':round(e,4),'ok':False,'content':None,'usage':None,'validation_error':None}
        if s==200:
            try:
                p=json.loads(b); c=p['choices'][0]['message']['content']; row['content']=c; row['usage']=p.get('usage'); ok,err=validate(case,c); row['ok']=ok; row['validation_error']=err
            except Exception as ex: row['validation_error']=f'{type(ex).__name__}: {ex}'
        else: row['validation_error']=b[:2000]
        failed=failed or not row['ok']; results.append(row); print(f"\n===== {case['name']} ====="); print(json.dumps(row,ensure_ascii=False,indent=2))
    report={'base_url':base,'model':a.model,'models_endpoint':{'status':st,'model_ids':ids},'warmup':{'count':a.warmups,'latencies_s':[round(x,4) for x in wl]},'results':results}
    if a.output: Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)); print(f'\n[OK] report={a.output}')
    return 1 if failed else 0
if __name__=='__main__': raise SystemExit(main())
