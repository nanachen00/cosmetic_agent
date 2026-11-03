#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, re, subprocess, time
from pathlib import Path

def listener(port):
    cp=subprocess.run(["ss","-H","-ltn",f"sport = :{port}"],capture_output=True,text=True)
    return bool(cp.stdout.strip())

def load(path):
    p=Path(path)
    return json.loads(p.read_text()) if p.is_file() else None

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--artifact-root",required=True)
    a=ap.parse_args()
    root=Path(a.artifact_root)

    running=[]
    cp=subprocess.run(["ps","-eo","pid,args"],capture_output=True,text=True)
    for line in cp.stdout.splitlines():
        if re.search(r"(benchmark_openai_chat\.py|benchmark_multiturn_chat\.py|03_structured_ab_recheck\.py)",line) and str(os.getpid()) not in line:
            running.append(line.strip())
    if running:
        print("[BLOCKED] benchmark client process(es) still active:")
        print("\n".join(running))
        return 2

    if not listener(8000) or not listener(8200):
        print("[BLOCKED] :8000 and :8200 must remain listening.")
        return 2

    s1=load(root/"01_parity"/"summary.json")
    s2=load(root/"02_drift"/"summary.json")
    s3=load(root/"03_ab"/"summary.json")
    if not all((s1,s2,s3)):
        print("[BLOCKED] missing Step 1-3 summary artifacts")
        return 2

    parity=s3.get("behavioral_parity",{})
    marker={
      "status":"DIRECT_MODEL_MICROBENCHMARK_COMPLETE",
      "created_epoch":time.time(),
      "blue_8000_up":True,"green_8200_up":True,
      "benchmark_clients_running":False,
      "step1_token_ids_equal":s1.get("token_ids_equal"),
      "step1_api_prompt_token_match":s1.get("blue_api_prompt_tokens")==s1.get("green_api_prompt_tokens"),
      "step2_verdict":s2.get("verdict"),
      "step3_behavioral_parity":parity,
      "production_cutover_allowed":bool(
          parity.get("http_equal") and
          parity.get("green_semantic_not_worse") and
          parity.get("prompt_token_parity_by_turn")
      ),
      "routing_changed":False,
      "servers_stopped":False,
      "note":"No benchmark client is left running. :8000/:8200 remain up and :8001 routing is unchanged."
    }
    p=root/"04_direct_model_gate.json"
    p.write_text(json.dumps(marker,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(marker,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
