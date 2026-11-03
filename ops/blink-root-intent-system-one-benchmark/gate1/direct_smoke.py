from __future__ import annotations
import argparse, hashlib, json, math, statistics, time
from pathlib import Path
import httpx

ROUTES = {
    "product": "找商品、推薦、價格、預算、購買條件或商品篩選",
    "knowledge": "成分、保養原理、膚況知識、比較、一般知識問答",
    "skin_tracking": "紀錄每日膚況、追蹤、照片/觀察、連續紀錄或報告",
    "conversation": "一般聊天、致謝、無明確專業任務",
    "safety": "過敏、刺激、禁忌、懷孕、風險或需優先安全處理",
    "clarify": "資訊不足，無法安全決定上述路由",
}

CASES = [
    ("zh-product", "請推薦適合混合肌、無香料、1000 元內的清爽保濕乳液。", "product"),
    ("zh-knowledge", "玻尿酸與神經醯胺在日常保濕上有什麼差異？", "knowledge"),
    ("zh-autoroot", "我想了解玻尿酸和神經醯胺的差別。", "knowledge"),
    ("zh-tracking", "幫我記錄今天鼻翼比較油、兩頰有點乾。", "skin_tracking"),
    ("zh-safety", "我用了水楊酸後臉很刺痛又紅，還能繼續用嗎？", "safety"),
    ("zh-conversation", "謝謝你，我先考慮一下。", "conversation"),
    ("en-control", "Recommend a fragrance-free moisturizer under TWD 1000 for combination skin.", "product"),
]

QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": "Classify the user's primary task. Choose exactly one route. Use clarify only when the task cannot be safely determined.",
        "criteria": ROUTES,
    },
    "needs_clarification": {
        "type": "noul",
        "instructions": "Is a clarifying question required before the primary task can be safely routed?",
        "criteria": {"true": "Clarification is required", "false": "The task can be routed now"},
    },
    "safety_sensitive": {
        "type": "noul",
        "instructions": "Does the message involve allergy, irritation, contraindication, pregnancy, or another safety-sensitive issue?",
        "criteria": {"true": "Safety-sensitive", "false": "Not obviously safety-sensitive"},
    },
}

def pct(xs, q):
    xs=sorted(xs)
    if not xs: return None
    if len(xs)==1: return xs[0]
    h=(len(xs)-1)*q; lo=int(math.floor(h)); hi=int(math.ceil(h))
    return xs[lo]+(h-lo)*(xs[hi]-xs[lo])

def validate_answer(data):
    assert isinstance(data, dict)
    assert "answers" in data and isinstance(data["answers"], dict)
    route=data["answers"]["route"]
    choice=route.get("choice")
    probs=route.get("probabilities")
    assert choice in ROUTES, route
    assert isinstance(probs, dict), route
    assert set(probs).issubset(set(ROUTES)), route
    vals=[float(v) for v in probs.values()]
    assert vals and all(math.isfinite(v) and 0 <= v <= 1 for v in vals), probs
    assert abs(sum(vals)-1.0) <= 0.03, probs
    conf=float(route.get("confidence", max(vals)))
    assert math.isfinite(conf) and 0 <= conf <= 1
    for key in ("needs_clarification","safety_sensitive"):
        x=data["answers"][key]
        assert "noul" in x, x
        p=float(x["noul"])
        assert math.isfinite(p) and 0 <= p <= 1, x
    if "latency_ms" in data:
        assert float(data["latency_ms"]) >= 0
    return choice, conf, probs

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--base-url",required=True)
    ap.add_argument("--out",required=True)
    args=ap.parse_args()
    base=args.base_url.rstrip("/")
    out=Path(args.out)
    records=[]
    new_state_lat=[]
    repeated_state_lat=[]
    new_model_lat=[]
    repeated_model_lat=[]

    with httpx.Client(timeout=90) as c:
        # Model endpoint contract.
        models=c.get(base+"/v1/models")
        models.raise_for_status()
        model_payload=models.json()

        for case_id,text,gold in CASES:
            for repetition in (1,2):
                body={"state":text,"model":"kev-latest","questions":QUESTIONS}
                t=time.perf_counter()
                r=c.post(base+"/v1/systemone",json=body)
                wall_ms=(time.perf_counter()-t)*1000
                rec={
                    "case":case_id,
                    "repetition":repetition,
                    "text_sha256":hashlib.sha256(text.encode()).hexdigest(),
                    "gold_route_observation_only":gold,
                    "http_status":r.status_code,
                    "client_wall_ms":wall_ms,
                }
                try:
                    payload=r.json()
                    rec["response"]=payload
                    if r.status_code==200:
                        choice,conf,probs=validate_answer(payload)
                        rec.update(contract_pass=True,pred_route=choice,confidence=conf,
                                   route_probabilities=probs,
                                   semantic_match_observation=(choice==gold),
                                   kev_latency_ms=payload.get("latency_ms"))
                    else:
                        rec.update(contract_pass=False,error="non_200")
                except Exception as e:
                    rec.update(contract_pass=False,error=f"{type(e).__name__}:{e}",
                               body_prefix=r.text[:500])
                records.append(rec)
                if rec.get("contract_pass"):
                    target = new_state_lat if repetition==1 else repeated_state_lat
                    target.append(wall_ms)
                    if rec.get("kev_latency_ms") is not None:
                        (new_model_lat if repetition==1 else repeated_model_lat).append(float(rec["kev_latency_ms"]))

    contract_pass=all(r.get("contract_pass") for r in records)
    zh=[r for r in records if r["case"].startswith("zh-") and r["repetition"]==1 and r.get("contract_pass")]
    semantic=sum(1 for r in zh if r.get("semantic_match_observation"))/len(zh) if zh else None
    summary={
        "models_response":model_payload,
        "request_count":len(records),
        "contract_pass":contract_pass,
        "zh_semantic_match_observation_only":semantic,
        "latency_ms":{
            "new_state_client":{"p50":pct(new_state_lat,.5),"p95":pct(new_state_lat,.95),"max":max(new_state_lat) if new_state_lat else None},
            "repeated_state_client":{"p50":pct(repeated_state_lat,.5),"p95":pct(repeated_state_lat,.95),"max":max(repeated_state_lat) if repeated_state_lat else None},
            "new_state_model":{"p50":pct(new_model_lat,.5),"p95":pct(new_model_lat,.95),"max":max(new_model_lat) if new_model_lat else None},
            "repeated_state_model":{"p50":pct(repeated_model_lat,.5),"p95":pct(repeated_model_lat,.95),"max":max(repeated_model_lat) if repeated_model_lat else None},
        },
        "scope":"API/shape/finite-probability smoke. Seven cases are not an accuracy benchmark.",
    }
    out.write_text(json.dumps({"summary":summary,"records":records},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    out.with_name("latency-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    raise SystemExit(0 if contract_pass else 3)

if __name__=="__main__":
    main()
