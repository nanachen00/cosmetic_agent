#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re, subprocess, time, urllib.error, urllib.request
from pathlib import Path

def post_json(url, payload, timeout=120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type":"application/json"},
        method="POST",
    )
    t0=time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw=r.read().decode(errors="replace")
            return r.status, json.loads(raw), time.perf_counter()-t0
    except urllib.error.HTTPError as e:
        return e.code, {"error":e.read().decode(errors="replace")}, time.perf_counter()-t0
    except Exception as e:
        return None, {"error":f"{type(e).__name__}: {e}"}, time.perf_counter()-t0

def get_json(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except Exception as e:
        return None, {"error":f"{type(e).__name__}: {e}"}

RENDER_CODE = r"""
import hashlib, json, sys
from pathlib import Path
from transformers import AutoTokenizer
model=sys.argv[1]
messages=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
tok=AutoTokenizer.from_pretrained(model, trust_remote_code=False)
template=getattr(tok,'chat_template',None)
if template is None:
    raise SystemExit('NO_CHAT_TEMPLATE')
rendered=tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
ids=tok.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)

# Transformers versions differ here:
# - some return a plain list / tensor
# - newer versions may return BatchEncoding
# Normalize all of them to a plain list[int] before JSON serialization.
if hasattr(ids, "data") and isinstance(getattr(ids, "data", None), dict):
    ids = ids.data.get("input_ids", ids)
elif isinstance(ids, dict):
    ids = ids.get("input_ids", ids)
elif hasattr(ids, "input_ids"):
    ids = ids.input_ids

if hasattr(ids, "tolist"):
    ids = ids.tolist()

if isinstance(ids, tuple):
    ids = list(ids)

if ids and isinstance(ids[0], (list, tuple)):
    ids = list(ids[0])

if not isinstance(ids, list) or not all(isinstance(x, int) for x in ids):
    raise TypeError(f"Unable to normalize token IDs to list[int]; got {type(ids).__name__}: {repr(ids)[:500]}")

obj={
  'python':sys.executable,
  'transformers':__import__('transformers').__version__,
  'tokenizer_class':type(tok).__name__,
  'chat_template_sha256':hashlib.sha256(template.encode()).hexdigest(),
  'chat_template':template,
  'rendered':rendered,
  'rendered_sha256':hashlib.sha256(rendered.encode()).hexdigest(),
  'token_ids':ids,
  'token_count':len(ids),
  'bos_token':tok.bos_token,'bos_token_id':tok.bos_token_id,
  'eos_token':tok.eos_token,'eos_token_id':tok.eos_token_id,
  'pad_token':tok.pad_token,'pad_token_id':tok.pad_token_id,
  'special_tokens_map':tok.special_tokens_map,
}
print(json.dumps(obj,ensure_ascii=False))
"""

def render_with(py, model, messages_path):
    cp=subprocess.run([py,"-c",RENDER_CODE,model,str(messages_path)],capture_output=True,text=True,timeout=120)
    if cp.returncode:
        raise RuntimeError(f"{py} render failed rc={cp.returncode}: {cp.stderr[-4000:]}")
    return json.loads(cp.stdout)

def listener_info(port):
    cp=subprocess.run(["ss","-ltnp"],capture_output=True,text=True)
    lines=[x for x in cp.stdout.splitlines() if re.search(rf":{port}\b",x)]
    info={"lines":lines}
    m=re.search(r'pid=(\d+)',"\n".join(lines))
    if m:
        pid=int(m.group(1)); info["pid"]=pid
        try: info["cmdline"]=Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0",b" ").decode()
        except Exception: pass
        if info.get("cmdline"):
            for token in info["cmdline"].split():
                if token.endswith(".py") and Path(token).is_file():
                    info["source"]=token
                    try:
                        src=Path(token).read_text(errors="replace")
                        hits=[ln for ln in src.splitlines() if any(k in ln for k in ("apply_chat_template","add_generation_prompt","chat_template","bos_token","eos_token"))]
                        info["source_template_hits"]=hits[:100]
                    except Exception: pass
                    break
    return info

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--blue-url",default="http://127.0.0.1:8000")
    ap.add_argument("--green-url",default="http://127.0.0.1:8200")
    ap.add_argument("--model-path",default="/var/lib/jenkins/tomod/taide-12b")
    ap.add_argument("--blue-python",default="/opt/conda/envs/rocm_env/bin/python")
    ap.add_argument("--green-python",default="/opt/conda/envs/vllm-rocm-v092/bin/python")
    ap.add_argument("--output-dir",required=True)
    a=ap.parse_args()
    out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)

    for name,url in (("blue",a.blue_url),("green",a.green_url)):
        st,body=get_json(url.rstrip("/")+"/v1/models")
        if st != 200:
            raise SystemExit(f"{name} /v1/models not ready: status={st} body={body}")

    messages=[
      {"role":"system","content":"你是 BLINKAI 美妝資料整理測試助理。若要求 JSON，只輸出合法 JSON；保留使用者提供的繁體中文偏好詞。"},
      {"role":"user","content":"請記住 PARITY-CODE，只回答「收到」。"},
      {"role":"assistant","content":"收到"},
      {"role":"user","content":"使用者想找「清爽、無香料、保濕」的產品。只輸出 JSON：{\"preferences\":[...]}。"},
    ]
    mp=out/"messages.json"
    mp.write_text(json.dumps(messages,ensure_ascii=False,indent=2),encoding="utf-8")

    blue_render=render_with(a.blue_python,a.model_path,mp)
    green_render=render_with(a.green_python,a.model_path,mp)
    (out/"blue_tokenizer_render.json").write_text(json.dumps(blue_render,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"green_tokenizer_render.json").write_text(json.dumps(green_render,ensure_ascii=False,indent=2),encoding="utf-8")

    b_ids=blue_render["token_ids"]; g_ids=green_render["token_ids"]
    first_diff=None
    for i,(b,g) in enumerate(zip(b_ids,g_ids)):
        if b != g:
            first_diff={"index":i,"blue":b,"green":g}; break
    if first_diff is None and len(b_ids)!=len(g_ids):
        i=min(len(b_ids),len(g_ids))
        first_diff={"index":i,"blue_tail":b_ids[i:],"green_tail":g_ids[i:]}

    payload={"model":"taide-12b","messages":messages,"temperature":0.0,"top_p":1.0,"max_tokens":64}
    bst,bbody,blat=post_json(a.blue_url.rstrip("/")+"/v1/chat/completions",payload)
    gst,gbody,glat=post_json(a.green_url.rstrip("/")+"/v1/chat/completions",payload)
    if bst != 200 or gst != 200:
        raise SystemExit(f"chat probe failed blue={bst} green={gst}")

    summary={
      "blue_url":a.blue_url,"green_url":a.green_url,
      "tokenizer_template_equal":blue_render["chat_template"]==green_render["chat_template"],
      "rendered_text_equal":blue_render["rendered"]==green_render["rendered"],
      "token_ids_equal":b_ids==g_ids,
      "blue_token_count":len(b_ids),"green_token_count":len(g_ids),
      "first_token_diff":first_diff,
      "blue_api_prompt_tokens":(bbody.get("usage") or {}).get("prompt_tokens"),
      "green_api_prompt_tokens":(gbody.get("usage") or {}).get("prompt_tokens"),
      "blue_api_latency_s":round(blat,4),"green_api_latency_s":round(glat,4),
      "blue_content":(((bbody.get("choices") or [{}])[0].get("message") or {}).get("content")),
      "green_content":(((gbody.get("choices") or [{}])[0].get("message") or {}).get("content")),
      "blue_listener":listener_info(8000),
      "green_listener":listener_info(8200),
    }
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
