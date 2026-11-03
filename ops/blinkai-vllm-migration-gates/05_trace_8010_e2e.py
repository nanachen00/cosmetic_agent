#!/usr/bin/env python3
from __future__ import annotations
import argparse, http.cookiejar, json, re, statistics, time, urllib.error, urllib.request, uuid
from pathlib import Path

def build_opener(cookie_jar=None):
    handlers=[]
    if cookie_jar:
        jar=http.cookiejar.MozillaCookieJar(cookie_jar)
        jar.load(ignore_discard=True,ignore_expires=True)
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)

def request_timed(op,url,payload,headers,timeout=180):
    data=json.dumps(payload,ensure_ascii=False).encode()
    req=urllib.request.Request(url,data=data,headers={"Content-Type":"application/json",**headers},method="POST")
    t0=time.perf_counter()
    try:
        r=op.open(req,timeout=timeout)
        t_headers=time.perf_counter()
        first=r.read(1)
        t_first=time.perf_counter()
        rest=r.read()
        t_end=time.perf_counter()
        raw=(first+rest).decode(errors="replace")
        return {
          "status":r.status,
          "headers_s":round(t_headers-t0,4),
          "first_body_byte_s":round(t_first-t0,4),
          "total_s":round(t_end-t0,4),
          "body":raw[:10000],
          "response_headers":dict(r.headers),
        }
    except urllib.error.HTTPError as e:
        t=time.perf_counter()
        return {
          "status":e.code,
          "headers_s":round(t-t0,4),
          "first_body_byte_s":round(t-t0,4),
          "total_s":round(t-t0,4),
          "body":e.read().decode(errors="replace")[:10000],
          "response_headers":dict(e.headers),
        }
    except Exception as e:
        t=time.perf_counter()
        return {"status":None,"headers_s":None,"first_body_byte_s":None,"total_s":round(t-t0,4),"error":f"{type(e).__name__}: {e}"}

def model_payload(prompt):
    return {
      "model":"taide-12b",
      "messages":[{"role":"user","content":prompt}],
      "temperature":0.0,
      "top_p":1.0,
      "max_tokens":96
    }

def med(rows,key):
    vals=[r[key] for r in rows if isinstance(r.get(key),(int,float))]
    return round(statistics.median(vals),4) if vals else None

def appended_log(path,start):
    p=Path(path)
    if not p.is_file():
        return None
    with p.open("rb") as f:
        f.seek(start)
        return f.read().decode(errors="replace")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--output-dir",required=True)
    ap.add_argument("--repetitions",type=int,default=3)
    ap.add_argument("--prompt",default="請用繁體中文一句話說明玻尿酸在保養品中的主要用途。")
    ap.add_argument("--model-8200",default="http://127.0.0.1:8200/v1/chat/completions")
    ap.add_argument("--router-8001",default="http://127.0.0.1:8001/v1/chat/completions")
    ap.add_argument("--litellm-8003",default="http://127.0.0.1:8003/v1/chat/completions")
    ap.add_argument("--app-url",default="http://127.0.0.1:8010/api/chat/message")
    ap.add_argument("--stream-url",default="http://127.0.0.1:8010/api/chat/message/stream")
    ap.add_argument("--body-file")
    ap.add_argument("--user-id")
    ap.add_argument("--conversation-id")
    ap.add_argument("--flow")
    ap.add_argument("--cookie-jar")
    ap.add_argument("--header",action="append",default=[],help="Extra header as 'Name: value'; repeatable.")
    ap.add_argument("--public-url")
    ap.add_argument("--app-log")
    ap.add_argument("--timeout",type=float,default=300)
    ap.add_argument("--include-stream",action="store_true")
    a=ap.parse_args()

    out=Path(a.output_dir)
    out.mkdir(parents=True,exist_ok=True)

    headers={}
    for h in a.header:
        if ":" not in h:
            raise SystemExit(f"bad --header: {h}")
        k,v=h.split(":",1)
        headers[k.strip()]=v.strip()

    if a.body_file:
        app_body=json.loads(Path(a.body_file).read_text(encoding="utf-8"))
    else:
        if not a.user_id or not a.conversation_id:
            raise SystemExit("Step 5 needs --body-file OR both --user-id and --conversation-id.")
        app_body={"message":a.prompt,"user_id":a.user_id,"conversation_id":a.conversation_id}
        if a.flow:
            app_body["flow"]=a.flow

    op=build_opener(a.cookie_jar)
    all_results={}

    for label,url in (
        ("model_8200",a.model_8200),
        ("router_8001",a.router_8001),
        ("litellm_8003",a.litellm_8003),
    ):
        rows=[]
        for _ in range(a.repetitions):
            rows.append(request_timed(op,url,model_payload(a.prompt),headers,a.timeout))
        all_results[label]=rows

    log_start=None
    if a.app_log and Path(a.app_log).is_file():
        log_start=Path(a.app_log).stat().st_size

    app_rows=[]
    for _ in range(a.repetitions):
        h={**headers,"Idempotency-Key":str(uuid.uuid4())}
        body=dict(app_body)
        body["message"]=a.prompt
        app_rows.append(request_timed(op,a.app_url,body,h,a.timeout))
    all_results["app_8010"]=app_rows

    if a.include_stream:
        stream_rows=[]
        for _ in range(a.repetitions):
            h={**headers,"Idempotency-Key":str(uuid.uuid4())}
            body=dict(app_body)
            body["message"]=a.prompt
            stream_rows.append(request_timed(op,a.stream_url,body,h,a.timeout))
        all_results["app_8010_stream"]=stream_rows

    if a.public_url:
        pub=[]
        for _ in range(a.repetitions):
            h={**headers,"Idempotency-Key":str(uuid.uuid4())}
            body=dict(app_body)
            body["message"]=a.prompt
            pub.append(request_timed(op,a.public_url,body,h,a.timeout))
        all_results["public"]=pub

    summaries={}
    for label,rows in all_results.items():
        summaries[label]={
          "statuses":[r.get("status") for r in rows],
          "median_headers_s":med(rows,"headers_s"),
          "median_first_body_byte_s":med(rows,"first_body_byte_s"),
          "median_total_s":med(rows,"total_s"),
          "rows":rows,
        }

    def diff(a,b):
        x=summaries[a]["median_total_s"]
        y=summaries[b]["median_total_s"]
        return round(x-y,4) if x is not None and y is not None else None

    decomposition={
      "router_minus_8200_s":diff("router_8001","model_8200"),
      "litellm_minus_router_s":diff("litellm_8003","router_8001"),
      "app8010_minus_litellm_s":diff("app_8010","litellm_8003"),
      "note":"External differential measurements only; exact internal orchestration attribution requires matching backend logs/tracing."
    }

    if log_start is not None:
        time.sleep(0.2)
        log_text=appended_log(a.app_log,log_start)
        if log_text is not None:
            (out/"app_log_segment.txt").write_text(log_text,encoding="utf-8")
            keywords=re.compile(r"(intent|orchestr|agent|embedding|retriev|knowledge|product|safety|compliance|taide|litellm|response|complete)",re.I)
            hits=[line for line in log_text.splitlines() if keywords.search(line)]
            (out/"app_log_stage_hits.txt").write_text("\n".join(hits)+"\n",encoding="utf-8")

    report={
      "prompt":a.prompt,
      "summaries":summaries,
      "decomposition":decomposition,
      "app_log":a.app_log
    }
    (out/"summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")

    print(json.dumps({
      "summaries":{k:{kk:vv for kk,vv in v.items() if kk!="rows"} for k,v in summaries.items()},
      "decomposition":decomposition
    },ensure_ascii=False,indent=2))

    bad=[(k,r.get("status")) for k,rows in all_results.items() for r in rows if r.get("status") != 200]
    if bad:
        print("[FAIL] non-200 responses:",bad)
        return 2
    return 0

if __name__=="__main__":
    raise SystemExit(main())
