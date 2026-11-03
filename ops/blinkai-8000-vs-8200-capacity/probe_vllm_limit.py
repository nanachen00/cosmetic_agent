#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from urllib.parse import urlparse

import compare_8000_8200_multiturn as bench

def is_sustainable(s,args):
    return bench.sustainable(
        s,
        args.slo_p95,
        args.success_floor,
        args.conversation_floor,
    )

def run_probe(args,vu,outdir,scenario):
    print(f"\n===== vLLM LIMIT PROBE VU={vu} =====",flush=True)
    bench.wait_gpu_idle(args.gpu)
    time.sleep(args.cooldown_s)
    s=bench.run_level(
        "green",
        args.green_url,
        vu,
        scenario,
        args.model,
        args.max_tokens,
        args.request_timeout,
        args.min_conversations_per_probe,
        outdir/f"vu{vu}",
        args.gpu,
    )
    print(json.dumps({
      "vus":vu,
      "success":s["request_success_rate"],
      "conversation_complete":s["conversation_completion_rate"],
      "p95":s["latency_s"]["p95"],
      "rps":s["request_rps"],
      "completion_tok_s":s["completion_tokens_per_s"],
      "healthy_after":s["endpoint_healthy_after"],
      "sustainable":is_sustainable(s,args),
    },ensure_ascii=False),flush=True)
    bench.wait_gpu_idle(args.gpu)
    return s

def unique_by_vu(items):
    out={}
    for x in items:
        if not x.get("skipped"):
            out[int(x["vus"])]=x
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--green-url",default="http://127.0.0.1:8200")
    ap.add_argument("--model",default="taide-12b")
    ap.add_argument("--scenario",default=str(Path(__file__).with_name("multiturn_scenario.json")))
    ap.add_argument("--seed-summary",help="green_summary.json from the A/B comparison; avoids rerunning already-tested ladder levels.")
    ap.add_argument("--max-vu",type=int,default=2048)
    ap.add_argument("--request-timeout",type=float,default=30.0)
    ap.add_argument("--max-tokens",type=int,default=64)
    ap.add_argument("--min-conversations-per-probe",type=int,default=1)
    ap.add_argument("--refine-steps",type=int,default=8)
    ap.add_argument("--cooldown-s",type=float,default=2.0)
    ap.add_argument("--gpu",type=int,default=0)
    ap.add_argument("--slo-p95",type=float,default=15.0)
    ap.add_argument("--success-floor",type=float,default=.99)
    ap.add_argument("--conversation-floor",type=float,default=.95)
    ap.add_argument("--output-dir",required=True)
    a=ap.parse_args()

    if a.max_vu<1 or a.max_vu>bench.HARD_MAX_VU:
        raise SystemExit(f"--max-vu must be 1..{bench.HARD_MAX_VU}")
    if not bench.endpoint_healthy(a.green_url):
        raise SystemExit(":8200 is not healthy")

    scenario=json.loads(Path(a.scenario).read_text(encoding="utf-8"))
    out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)

    inventory=bench.listener_inventory(urlparse(a.green_url).port or 8200)
    tested={}
    if a.seed_summary:
        seed=json.loads(Path(a.seed_summary).read_text(encoding="utf-8"))
        tested.update(unique_by_vu(seed))

    # Ensure we have an exponential ladder through max_vu if the seed did not.
    ladder=[1,2,4,8,16,32,64,128,256,512,1024,2048]
    ladder=[v for v in ladder if v<=a.max_vu]
    for vu in ladder:
        if vu in tested:
            continue

        # If an earlier test left the endpoint unhealthy, do not escalate.
        if not bench.endpoint_healthy(a.green_url):
            break

        s=run_probe(a,vu,out,scenario)
        tested[vu]=s

        # A severe failure is enough to establish an upper bound.
        if (
            not s.get("endpoint_healthy_after",True)
            or s.get("request_success_rate",0)<.50
            or s.get("conversation_completion_rate",0)<.25
        ):
            break

    sustainable_vus=sorted(v for v,s in tested.items() if is_sustainable(s,a))
    unsustainable_vus=sorted(v for v,s in tested.items() if not is_sustainable(s,a))

    low=max(sustainable_vus,default=0)
    higher_bad=[v for v in unsustainable_vus if v>low]
    high=min(higher_bad,default=None)

    # If the whole tested ladder is sustainable through the requested ceiling,
    # the correct answer is a lower bound, not a fabricated exact limit.
    ceiling_reached=(low>=a.max_vu and is_sustainable(tested.get(a.max_vu,{}),a))

    refinement=[]
    if not ceiling_reached and high is not None:
        for _ in range(a.refine_steps):
            if high-low<=1:
                break
            mid=(low+high)//2
            if mid in tested:
                # Find an untested midpoint-like integer.
                candidates=[x for x in range(mid+1,high) if x not in tested]
                if not candidates:
                    candidates=[x for x in range(low+1,mid) if x not in tested]
                if not candidates:
                    break
                mid=candidates[len(candidates)//2]

            if not bench.endpoint_healthy(a.green_url):
                break

            s=run_probe(a,mid,out/"refinement",scenario)
            tested[mid]=s
            ok=is_sustainable(s,a)
            refinement.append({"vus":mid,"sustainable":ok})

            if ok:
                low=mid
            else:
                high=mid

    ordered=[tested[v] for v in sorted(tested)]
    sustainable_vus=sorted(v for v,s in tested.items() if is_sustainable(s,a))
    low=max(sustainable_vus,default=0)
    unsustainable_above=sorted(v for v,s in tested.items() if v>low and not is_sustainable(s,a))
    high=min(unsustainable_above,default=None)

    peak=max(
        (s.get("completion_tokens_per_s") or 0, v)
        for v,s in tested.items()
    ) if tested else (0,None)

    max_tested=max(tested,default=None)
    limit_text=(
        f">={a.max_vu}" if (
            max_tested==a.max_vu
            and is_sustainable(tested[a.max_vu],a)
        )
        else str(low)
    )

    gpu_util=inventory.get("gpu_memory_utilization")
    max_num_seqs=inventory.get("max_num_seqs")
    conservative_caps=bool(
        (isinstance(gpu_util,(int,float)) and gpu_util<=.30)
        or (isinstance(max_num_seqs,int) and max_num_seqs<=8)
    )

    report={
      "scope":"CURRENT_RUNNING_VLLM_SERVER_CONFIGURATION",
      "green_url":a.green_url,
      "server_inventory":inventory,
      "slo":{
        "p95_s":a.slo_p95,
        "request_success_floor":a.success_floor,
        "conversation_completion_floor":a.conversation_floor,
      },
      "tested_vus":sorted(tested),
      "max_tested_vu":max_tested,
      "max_sustainable_vu":low if low else None,
      "first_unsustainable_vu_above_boundary":high,
      "capacity_limit_statement":limit_text,
      "ceiling_reached_without_failure":limit_text.startswith(">="),
      "peak_completion_tokens_per_s":round(peak[0],4),
      "peak_throughput_vu":peak[1],
      "refinement":refinement,
      "conservative_startup_caps_detected":conservative_caps,
      "absolute_hardware_limit_established":False,
      "interpretation":(
        "The measured boundary is the capacity of the CURRENT :8200 server configuration. "
        "If the configured max-num-seqs and GPU-memory-utilization are conservative, "
        "this is not the absolute MI300X/vLLM hardware ceiling. "
        "If 2048 remains sustainable, the result is a lower bound (>=2048), not an exact limit."
      ),
    }

    (out/"all_tested_results.json").write_text(
        json.dumps(ordered,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"vllm_limit.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")

    md=[
      "# vLLM :8200 Current-Configuration Capacity Limit",
      "",
      f"- Capacity statement: **{limit_text} VU**",
      f"- Max sustainable measured VU: **{report['max_sustainable_vu']}**",
      f"- First unsustainable VU above boundary: **{high}**",
      f"- Peak completion throughput: **{report['peak_completion_tokens_per_s']} tok/s @ VU {report['peak_throughput_vu']}**",
      f"- Current max-num-seqs: **{max_num_seqs}**",
      f"- Current gpu-memory-utilization: **{gpu_util}**",
      f"- Conservative startup caps detected: **{conservative_caps}**",
      "",
      "This is the limit of the currently running vLLM configuration under the configured SLO.",
      "It is not labeled as the absolute MI300X hardware limit because this script deliberately does not restart/tune :8200.",
    ]
    (out/"LIMIT_REPORT.md").write_text("\n".join(md)+"\n",encoding="utf-8")

    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
