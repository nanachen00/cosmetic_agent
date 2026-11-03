#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess, time, urllib.error, urllib.request
from pathlib import Path

EXPORT_CODE=r"""
import sys
from transformers import AutoTokenizer
tok=AutoTokenizer.from_pretrained(sys.argv[1],trust_remote_code=False)
if not tok.chat_template: raise SystemExit('NO_CHAT_TEMPLATE')
print(tok.chat_template,end='')
"""

def post(url,payload,timeout=180):
    req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),headers={"Content-Type":"application/json"},method="POST")
    t0=time.perf_counter()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return r.status,json.loads(r.read().decode()),time.perf_counter()-t0
    except urllib.error.HTTPError as e:
        return e.code,{"error":e.read().decode(errors="replace")},time.perf_counter()-t0
    except Exception as e:
        return None,{"error":f"{type(e).__name__}: {e}"},time.perf_counter()-t0

def extract(body):
    return {
      "prompt_tokens":(body.get("usage") or {}).get("prompt_tokens"),
      "completion_tokens":(body.get("usage") or {}).get("completion_tokens"),
      "content":(((body.get("choices") or [{}])[0].get("message") or {}).get("content")),
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--blue-url",default="http://127.0.0.1:8000")
    ap.add_argument("--green-url",default="http://127.0.0.1:8200")
    ap.add_argument("--model-path",default="/var/lib/jenkins/tomod/taide-12b")
    ap.add_argument("--blue-python",default="/opt/conda/envs/rocm_env/bin/python")
    ap.add_argument("--output-dir",required=True)
    a=ap.parse_args()
    out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)

    cp=subprocess.run([a.blue_python,"-c",EXPORT_CODE,a.model_path],capture_output=True,text=True,timeout=60)
    if cp.returncode:
        raise SystemExit(f"template export failed: {cp.stderr}")
    template=cp.stdout
    tpl=out/"blue_chat_template.jinja"; tpl.write_text(template,encoding="utf-8")

    messages=[
      {"role":"system","content":"你是 BLINKAI 美妝資料整理測試助理。若要求 JSON，只輸出合法 JSON；保留使用者提供的繁體中文偏好詞。"},
      {"role":"user","content":"請記住 DRIFT-CODE，只回答「收到」。"},
      {"role":"assistant","content":"收到"},
      {"role":"user","content":"使用者想找「清爽、無香料、保濕」的產品。只輸出 JSON：{\"preferences\":[...]}。"},
    ]
    base={"model":"taide-12b","messages":messages,"temperature":0.0,"top_p":1.0,"max_tokens":256}
    tests=[
      ("blue_default",a.blue_url,dict(base)),
      ("green_default",a.green_url,dict(base)),
      ("green_blue_template",a.green_url,{**base,"chat_template":template,"add_generation_prompt":True}),
    ]
    results={}
    for name,url,payload in tests:
        st,b,lat=post(url.rstrip("/")+"/v1/chat/completions",payload)
        results[name]={"status":st,"latency_s":round(lat,4),**extract(b),"raw":b if st!=200 else None}

    gen_cfg=Path(a.model_path)/"generation_config.json"
    generation_config=json.loads(gen_cfg.read_text()) if gen_cfg.is_file() else None

    bp=results["blue_default"]["prompt_tokens"]; gp=results["green_default"]["prompt_tokens"]; op=results["green_blue_template"]["prompt_tokens"]
    summary={
      "results":results,
      "generation_config":generation_config,
      "template_override_prompt_token_match":bp is not None and bp==op,
      "default_prompt_token_match":bp is not None and bp==gp,
      "verdict":(
        "REQUEST_LEVEL_TEMPLATE_OVERRIDE_MATCHES_BLUE" if bp is not None and bp==op and bp!=gp
        else "DEFAULT_ALREADY_PROMPT_TOKEN_MATCH" if bp is not None and bp==gp
        else "PROMPT_TOKEN_DRIFT_REMAINS"
      ),
      "production_mutation_performed":False,
      "candidate_chat_template_file":str(tpl),
      "note":"Request-level chat_template override only; no service restart or routing change."
    }
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
