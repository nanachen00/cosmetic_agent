#!/usr/bin/env python3
from __future__ import annotations
import re
import argparse, json, statistics, time, urllib.error, urllib.request
from pathlib import Path

def post(url,payload,timeout):
    req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),headers={"Content-Type":"application/json"},method="POST")
    t=time.perf_counter()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return r.status,json.loads(r.read().decode()),time.perf_counter()-t
    except urllib.error.HTTPError as e:
        return e.code,{"error":e.read().decode(errors="replace")},time.perf_counter()-t
    except Exception as e:
        return None,{"error":f"{type(e).__name__}: {e}"},time.perf_counter()-t

def first_json_strict(text):
    try:
        return json.loads(text.strip())
    except Exception:
        return None

def first_json_semantic(text):
    s=text.strip()
    m=re.fullmatch(r"```(?:json)?\s*(.*?)\s*```",s,flags=re.S|re.I)
    if m:
        s=m.group(1).strip()
    try:
        return json.loads(s)
    except Exception:
        pass

    for start,ch in enumerate(s):
        if ch not in "[{":
            continue
        stack=[ch]; in_string=False; escape=False
        for i in range(start+1,len(s)):
            c=s[i]
            if in_string:
                if escape: escape=False
                elif c=="\\": escape=True
                elif c=='"': in_string=False
                continue
            if c=='"':
                in_string=True
            elif c in "[{":
                stack.append(c)
            elif c in "]}":
                if not stack:
                    break
                opener=stack[-1]
                if (opener=="[" and c!="]") or (opener=="{" and c!="}"):
                    break
                stack.pop()
                if not stack:
                    frag=s[start:i+1]
                    try:
                        return json.loads(frag)
                    except Exception:
                        break
    return None

def contains_semantic_value(items,value):
    if isinstance(items,str):
        return items.strip().casefold()==str(value).strip().casefold()
    if isinstance(items,list):
        return any(contains_semantic_value(x,value) for x in items)
    if isinstance(items,dict):
        return any(contains_semantic_value(x,value) for x in items.values())
    return False

def validate(turn,content,nonce):
    if turn==1:
        ok="收到" in content
        return ok,ok
    if turn==2:
        strict_obj=first_json_strict(content)
        semantic_obj=first_json_semantic(content)
        strict=isinstance(strict_obj,dict) and isinstance(strict_obj.get("preferences"),list)
        semantic=isinstance(semantic_obj,dict) and isinstance(semantic_obj.get("preferences"),list)
        return strict,semantic
    if turn==3:
        strict_obj=first_json_strict(content)
        semantic_obj=first_json_semantic(content)
        strict=(
            isinstance(strict_obj,dict)
            and isinstance(strict_obj.get("preferences"),list)
            and "不黏膩" in strict_obj["preferences"]
            and set(strict_obj)=={"preferences"}
        )
        semantic=(
            isinstance(semantic_obj,dict)
            and isinstance(semantic_obj.get("preferences"),list)
            and contains_semantic_value(semantic_obj["preferences"],"不黏膩")
        )
        return strict,semantic
    if turn==4:
        return content.strip()==nonce, nonce in content
    return False,False

def run_endpoint(name,url,conversations,max_tokens,timeout,template=None):
    rows=[]
    system="你是 BLINKAI 美妝資料整理測試助理。若要求 JSON，只輸出合法 JSON；保留使用者提供的繁體中文偏好詞。"
    epoch=int(time.time())
    for c in range(1,conversations+1):
        nonce=f"AB-{name}-C{c:03d}-{epoch}"
        turns=[
          f"請記住 {nonce}，只回答「收到」。",
          "使用者想找「清爽、無香料、保濕」的產品。只輸出 JSON：{\"preferences\":[...]}。",
          "在同一 JSON 結構加入「不黏膩」，不要增加其他 key。",
          "最後只輸出測試代碼。",
        ]
        messages=[{"role":"system","content":system}]
        for ti,user in enumerate(turns,1):
            messages.append({"role":"user","content":user})
            payload={"model":"taide-12b","messages":messages,"temperature":0.0,"top_p":1.0,"max_tokens":max_tokens}
            if template is not None:
                payload["chat_template"]=template
                payload["add_generation_prompt"]=True
            st,b,lat=post(url.rstrip("/")+"/v1/chat/completions",payload,timeout)
            row={"conversation":c,"turn":ti,"status":st,"latency_s":round(lat,4),"content":None,"prompt_tokens":None,"completion_tokens":None,"strict":False,"semantic":False}
            if st==200:
                content=b["choices"][0]["message"]["content"]
                usage=b.get("usage") or {}
                strict,semantic=validate(ti,content,nonce)
                row.update(content=content,prompt_tokens=usage.get("prompt_tokens"),completion_tokens=usage.get("completion_tokens"),strict=strict,semantic=semantic)
                messages.append({"role":"assistant","content":content})
            else:
                row["error"]=b
            rows.append(row)
    good=[r for r in rows if r["status"]==200]
    summary={
      "requests":len(rows),"http_200":len(good),"http_success_rate":len(good)/len(rows),
      "strict_pass_rate":sum(r["strict"] for r in good)/len(good) if good else 0,
      "semantic_pass_rate":sum(r["semantic"] for r in good)/len(good) if good else 0,
      "latency_mean_s":round(statistics.mean(r["latency_s"] for r in good),4) if good else None,
      "prompt_tokens_by_turn":{str(t):sorted(set(r["prompt_tokens"] for r in good if r["turn"]==t)) for t in range(1,5)},
      "per_turn":{str(t):{
         "strict":sum(r["strict"] for r in good if r["turn"]==t),
         "semantic":sum(r["semantic"] for r in good if r["turn"]==t),
         "count":sum(1 for r in good if r["turn"]==t)
      } for t in range(1,5)}
    }
    return rows,summary

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--blue-url",default="http://127.0.0.1:8000")
    ap.add_argument("--green-url",default="http://127.0.0.1:8200")
    ap.add_argument("--green-chat-template-file")
    ap.add_argument("--conversations",type=int,default=10)
    ap.add_argument("--max-tokens",type=int,default=256)
    ap.add_argument("--timeout",type=float,default=300)
    ap.add_argument("--output-dir",required=True)
    a=ap.parse_args()
    out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)
    template=Path(a.green_chat_template_file).read_text(encoding="utf-8") if a.green_chat_template_file else None
    brows,bsummary=run_endpoint("blue",a.blue_url,a.conversations,a.max_tokens,a.timeout,None)
    grows,gsummary=run_endpoint("green",a.green_url,a.conversations,a.max_tokens,a.timeout,template)
    summary={
      "blue":bsummary,"green":gsummary,
      "green_template_override_used":bool(template),
      "behavioral_parity":{
        "http_equal":bsummary["http_success_rate"]==gsummary["http_success_rate"],
        "green_semantic_not_worse":gsummary["semantic_pass_rate"]>=bsummary["semantic_pass_rate"],
        "green_strict_not_worse":gsummary["strict_pass_rate"]>=bsummary["strict_pass_rate"],
        "prompt_token_parity_by_turn":bsummary["prompt_tokens_by_turn"]==gsummary["prompt_tokens_by_turn"],
      }
    }
    (out/"blue_rows.json").write_text(json.dumps(brows,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"green_rows.json").write_text(json.dumps(grows,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
