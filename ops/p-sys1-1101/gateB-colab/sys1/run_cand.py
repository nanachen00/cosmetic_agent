#!/usr/bin/env python3
"""P+Sys1 收集包裝器：先過 policy（基座來源），再用 gateb.py 的 kev-collect 對任何「說 Kev 協定」的 System1 伺服器收集。
不修改任何凍結檔案：只在執行期把 KevClient.ask 的 model 欄位換成 registry 的 model_field（例如 Laya 的 multilingual）。

  python3 sys1/run_cand.py collect --cand laya-mmbert --run runs/sys1-gd  --kev-url http://127.0.0.1:8231 [--vus 1] [--timeout 180]
  python3 sys1/run_cand.py collect --cand laya-mmbert --run runs/sys1-gold --dataset router_gold --kev-url http://127.0.0.1:8231
  python3 sys1/run_cand.py probe   --cand laya-mmbert --kev-url http://127.0.0.1:8231      # 送 1 句完整題組，檢查格式/延遲/機率合法性，不寫檔
  --dataset router_gold_v2 = 已凍結的新 hold-out（sys1/holdout_v2.py）
結果寫到 <run>/kev/<kev_label>/raw.jsonl + probe.json（與 Kev 同格式，可直接用 sys1/analyze_cand.py 分析）。
gateb 只允許連 127.0.0.1:8230-8239（kevlib 硬性限制）。
"""
import argparse, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE); sys.path.insert(0, ROOT)
import policy


def patch_model(name):
    import kevlib as K
    orig = K.KevClient.ask
    def ask(self, text, questions, model=name): return orig(self, text, questions, model=model)
    K.KevClient.ask = ask


V2 = os.path.join(HERE, "holdout_v2", "router_gold_v2.json")


def use_v2(run):
    """--dataset router_gold_v2：沿用 gateb 的 router_gold 流程，但把題目換成已凍結的 v2（執行期替換 HO.cases，不改檔案）。"""
    import holdout as HO, holdout_v2 as H2
    if not H2.os.path.exists(H2.FZ): sys.exit("[v2] hold-out v2 尚未凍結：python3 sys1/holdout_v2.py freeze ...")
    f = json.load(open(H2.FZ))
    if H2.sha(f["file"]) != f["sha256"]: sys.exit("[v2] NOT FROZEN ✗：router_gold_v2.json 被改過，停止")
    mk = os.path.join(run, "sys1_dataset.txt")
    if os.path.exists(mk) and open(mk).read().strip() != "v2": sys.exit(f"[v2] {run} 之前是別的資料集，請換新的 --run")
    os.makedirs(run, exist_ok=True); open(mk, "w").write("v2")
    def cases():
        out = []
        for c in json.load(open(f["file"], encoding="utf-8"))["cases"]:
            out.append({"id": "h2_" + c["id"], "text": c["query"], "split": "holdout", "group": c["route"] + ("_boundary" if c["boundary"] else ""), "route": c["route"],
                        "source": c["source"], "boundary": bool(c["boundary"]), "safety": c["route"] == "safety", "emergency": False,
                        "intents": HO.GOLD_INTENTS[c["route"]], "budget": None, "avoid": [], "concern": None, "other_numbers": False})
        return out
    HO.cases = cases


def probe(c, url):
    import gateb as GB, kevlib as K
    K.check_base(url); patch_model(c["model_field"])
    cl = K.KevClient(url, timeout=60); m = cl.models()
    print("models:", m["status"], str(m["json"])[:300])
    text = "孕婦可以用水楊酸嗎，預算八百"
    P2c, _ = GB.parse_all([{"id": "x", "text": text}]); q = GB.pk_bundle(P2c["x"])
    t0 = time.perf_counter(); r = cl.ask(text, q); ms = (time.perf_counter() - t0) * 1000
    a = r["answers"] or {}; bad = []
    print(f"status={r['status']} wall={ms:.0f}ms 題數={len(q)} 回答數={len(a)} server_ms={r['kev_ms']}")
    for name, qq in q.items():
        v = a.get(name)
        if v is None: bad.append(f"{name}: 沒有回答"); continue
        if qq["type"] == "noul":
            try:
                p = float(v["noul"]); assert 0 <= p <= 1
            except Exception: bad.append(f"{name}: noul 不是 0-1 機率 ({v})")
        else:
            pr = v.get("probabilities")
            if not isinstance(pr, dict) or not pr: bad.append(f"{name}: choice 沒有 probabilities")
    print("契約問題：" + ("無" if not bad else ""))
    for b in bad[:12]: print("  -", b)
    if len(bad) > 12: print(f"  … 另外 {len(bad) - 12} 項")
    if r["status"] != 200: print(r["text"][:300])
    return 0 if (r["status"] == 200 and not bad) else 4


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["collect", "probe"]); ap.add_argument("--cand", required=True)
    ap.add_argument("--run"); ap.add_argument("--dataset", default="gate_data", choices=["gate_data", "router_gold", "router_gold_v2"]); ap.add_argument("--kev-url", default="http://127.0.0.1:8231")
    ap.add_argument("--vus", default="1"); ap.add_argument("--timeout", default="180"); ap.add_argument("--split", default="test")
    a = ap.parse_args()
    code, msg = policy.check(a.cand, "collect"); print("[policy]", msg)
    if code: return code
    c = policy.load()[a.cand]
    if a.cmd == "probe": return probe(c, a.kev_url)
    if not a.run: sys.exit("--run 必填")
    import gateb as GB, kevlib as K
    K.check_base(a.kev_url); patch_model(c["model_field"])
    ds = a.dataset
    if ds == "router_gold_v2": use_v2(a.run); ds = "router_gold"
    elif ds == "router_gold" and os.path.exists(os.path.join(a.run, "sys1_dataset.txt")): sys.exit("[run] 這個 run 是 v2 資料集，請換新的 --run")
    sys.argv = ["gateb.py", "kev-collect", "--run", a.run, "--dataset", ds, "--kev-url", a.kev_url, "--kev-label", c["kev_label"],
                "--vus", a.vus, "--timeout", a.timeout, "--split", a.split, "--no-analyze"]
    return GB.main()


if __name__ == "__main__":
    sys.exit(main())
