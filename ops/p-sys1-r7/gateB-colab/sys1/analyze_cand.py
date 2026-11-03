#!/usr/bin/env python3
"""P+Sys1 分析（離線，不連任何伺服器）。
  1) 在 gate_data run 的 cal 切分上調門檻（和 P+K v2 同一套 tune_v2），寫入 sys1/frozen/<id>.json（寫一次；已存在就拒絕覆蓋）。
  2) 用凍結的門檻在 router_gold run（hold-out）評分，不再調任何東西。
  3) 對照：Parser only、Kev-0.8B 凍結結果（若 hold-out run 有 kev/raw.jsonl）；McNemar 成對比較；對 gates.json 逐條判定。
  python3 sys1/analyze_cand.py --cand laya-mmbert --cal-run runs/sys1-gd --hold-run runs/sys1-gold [--out DIR]
"""
import argparse, hashlib, json, math, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE); sys.path.insert(0, ROOT)
import policy


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cand", required=True); ap.add_argument("--cal-run", required=True); ap.add_argument("--hold-run", required=True)
    ap.add_argument("--out"); ap.add_argument("--min-recall-safety", type=float, default=0.99)
    ap.add_argument("--kev-ref-run", help="Colab 的 gate_data run（含 kev/raw.jsonl，例如 results-gateB-t4-001/gateB-t4-001）：在 gate_data test 上並列 Kev-0.8B 對照（非 hold-out，不影響 S2 判定）")
    a = ap.parse_args()
    code, msg = policy.check(a.cand, "collect"); print("[policy]", msg)
    if code: return code
    c0 = policy.load()[a.cand]; lab = c0["kev_label"]
    import gateb as GB, kevlib as K, gateA as GA, holdout as HO, gaterun as G
    cal_cases = json.load(open(os.path.join(a.cal_run, "cases.json"), encoding="utf-8")); hold_cases = json.load(open(os.path.join(a.hold_run, "cases.json"), encoding="utf-8"))
    kd = lambda run: os.path.join(run, "kev", lab)
    rc, rh = GB.read_jsonl(os.path.join(kd(a.cal_run), "raw.jsonl")), GB.read_jsonl(os.path.join(kd(a.hold_run), "raw.jsonl"))
    pj = os.path.join(kd(a.cal_run), "probe.json"); K.NOUL_TRUE_HIGH = bool(json.load(open(pj)).get("noul_true_high", True)) if os.path.exists(pj) else True
    if not K.NOUL_TRUE_HIGH: print("[WARN] noul 方向與 Kev 文件相反（probe.json），分數已翻轉，請確認伺服器")
    okrow = lambda r: isinstance(r.get("answers"), dict) and "emergency" in r["answers"]
    cal = [c for c in cal_cases if c["split"] == "cal" and okrow(rc.get(c["id"], {}))]
    if not cal: sys.exit("cal 切分沒有可用的回答：先對 gate_data run 執行 collect（--split all）")
    P2c, _ = GB.parse_all(cal_cases); P2h, pmh = GB.parse_all(hold_cases)
    out = a.out or os.path.join(a.hold_run, f"sys1_{lab}"); os.makedirs(out, exist_ok=True)
    fz = os.path.join(HERE, "frozen", f"{a.cand}.json"); os.makedirs(os.path.dirname(fz), exist_ok=True)
    if os.path.exists(fz):
        th = json.load(open(fz))["thresholds"]; print(f"[freeze] 使用既有凍結門檻 {fz}（不重調）")
    else:
        kv = {c["id"]: GA.kev_view(c, rc[c["id"]], P2c[c["id"]], None) for c in cal}
        pe = {c["id"]: K.p_noul(rc[c["id"]]["answers"], "emergency") for c in cal}
        th = GB.tune_v2(cal, kv, pe, P2c, a.min_recall_safety)
        body = {"cand": a.cand, "thresholds": th, "cal_run": a.cal_run, "n_cal": len(cal), "noul_true_high": K.NOUL_TRUE_HIGH}
        body["sha256"] = hashlib.sha256(json.dumps(th, sort_keys=True).encode()).hexdigest()
        json.dump(body, open(fz, "w"), ensure_ascii=False, indent=1); print(f"[freeze] 門檻寫入 {fz}（之後不可覆蓋）")
    ok = [c for c in hold_cases if okrow(rh.get(c["id"], {}))]; failed = len(hold_cases) - len(ok)
    arms = [HO.score("Parser only（無模型）", hold_cases, {c["id"]: GA.parser_only(c, P2h[c["id"]]) for c in hold_cases},
                     {"M7_latency": {"kind": "p50", "p50": G.lat([pmh[c["id"]] for c in hold_cases]).get("p50")}})]
    dec = {}
    for c in ok:
        r = rh[c["id"]]; kvv = GA.kev_view(c, r, P2h[c["id"]], None); pe = K.p_noul(r["answers"], "emergency")
        dec[c["id"]] = GB.decide_v2(c, P2h[c["id"]], kvv, pe, th)
    walls = sorted(rh[c["id"]]["wall_ms"] + rh[c["id"]]["parser_ms"] for c in ok)
    p95 = walls[int(0.95 * (len(walls) - 1))] if walls else None
    arms.append(HO.score(f"P+Sys1・{a.cand}", hold_cases, dec, {"M7_latency": {"kind": "p50", "p50": G.lat(walls).get("p50") if walls else None}, "sys1_failed": failed, "p95": p95, "base": True}))
    # Kev-0.8B 凍結結果（若有）當對照
    kp = os.path.join(a.hold_run, "kev", "raw.jsonl")
    if os.path.exists(kp):
        FT = json.load(open(os.path.join(HO.HD, "frozen_thresholds.json"), encoding="utf-8"))["by_kev"]["0.8b"]["v2"]; kr = GB.read_jsonl(kp)
        kk = [c for c in hold_cases if okrow(kr.get(c["id"], {}))]
        kd2 = {c["id"]: GB.decide_v2(c, P2h[c["id"]], GA.kev_view(c, kr[c["id"]], P2h[c["id"]], None), K.p_noul(kr[c["id"]]["answers"], "emergency"), FT) for c in kk}
        arms.append(HO.score("P+K v2・Kev-0.8B（凍結，對照）", hold_cases, kd2, {"M7_latency": {"kind": "p50", "p50": None}}))
    me = arms[1]
    for x in arms:
        if x is not me: x["paired"] = HO.paired(me, x, hold_cases)
    g = json.load(open(os.path.join(HERE, "gates.json")))["S2_holdout"]; parser = arms[0]; v = []
    chk = lambda name, cond, val: v.append((name, bool(cond), val))
    chk("安全漏判 ≤ %d" % g["safety_fn_max"], me["M1_k"][1] - me["M1_k"][0] <= g["safety_fn_max"], f"漏 {me['M1_k'][1] - me['M1_k'][0]}/{me['M1_k'][1]}（Wilson 下界 {me['M1_ci'][0]:.3f}）")
    chk("安全誤報率 ≤ %.2f" % g["safety_far_max"], (me["M2_safety_far"] or 0) <= g["safety_far_max"], f"{me['M2_k'][0]}/{me['M2_k'][1]}")
    chk("路由準確率 ≥ %.2f" % g["route_acc_abs_min"], me["M3_route_acc"] >= g["route_acc_abs_min"], f"{me['M3_route_acc']:.3f}")
    chk("路由準確率高於 Parser only", me["M3_route_acc"] > parser["M3_route_acc"], f"{me['M3_route_acc']:.3f} vs {parser['M3_route_acc']:.3f}")
    chk("Sys1 無回答/錯誤率 ≤ %.3f" % g["sys1_error_rate_max"], failed / len(hold_cases) <= g["sys1_error_rate_max"], f"{failed}/{len(hold_cases)}")
    chk("p95 延遲(含 Parser) ≤ %d ms" % g["p95_latency_ms_max"], p95 is not None and p95 <= g["p95_latency_ms_max"], f"{p95:.0f} ms" if p95 else "–")
    if os.path.exists(os.path.join(a.hold_run, "sys1_dataset.txt")):
        import holdout_v2 as H2; f = json.load(open(H2.FZ)) if os.path.exists(H2.FZ) else None
        frozen = bool(f) and H2.sha(f["file"]) == f["sha256"]; bad = [] if frozen else ["router_gold_v2.json"]
    else:
        frozen, bad = HO.check_freeze()
    md = HO.render(arms, frozen, bad, {"env": {}}, me["name"])
    ds = "router_gold_v2" if os.path.exists(os.path.join(a.hold_run, "sys1_dataset.txt")) else "router_gold"
    md = md.replace("Gate B hold-out：router_gold", f"P+Sys1 hold-out：{ds}", 1).replace("300 句（每個路由 50 句；boundary 38）", f"{len(hold_cases)} 句", 1).replace("P+K 門檻：`holdout/frozen_thresholds.json`（只用 gate_data cal）", "Sys1 門檻：`sys1/frozen/<id>.json`（只用 gate_data cal）", 1)
    L = [f"\n## S2 判定（{a.cand}；門檻來源 sys1/gates.json，事前登錄）\n", "|條件|結果|數值|", "|--|--|--|"] + [f"|{n}|{'PASS' if p else 'FAIL'}|{x}|" for n, p, x in v]
    allp = all(p for _, p, _ in v); L.append(f"\n**整體：{'PASS（可進 S3 影子流量）' if allp else 'FAIL（不進 S3）'}**。這是合成 hold-out；PASS 只代表可以進下一關，不代表可以替換。\n")
    ref = kev_reference(a, th, cal_cases, rc, GB, GA, K) if a.kev_ref_run else None
    if ref: L += ref["md"]
    open(os.path.join(out, "SYS1_HOLDOUT.md"), "w", encoding="utf-8").write(md + "\n".join(L))
    json.dump({"cand": a.cand, "pass": allp, "checks": v, "arms": [{k: x_ for k, x_ in x.items() if k != "_pred"} for x in arms], "gate_data_test_reference": ref and ref["metrics"]}, open(os.path.join(out, "sys1_metrics.json"), "w"), ensure_ascii=False, indent=1, default=str)
    print("\n".join(L)); print(f"→ {out}/SYS1_HOLDOUT.md"); return 0 if allp else 5


def kev_reference(a, th, cal_cases, rc, GB, GA, K):
    """gate_data test 切分上：Parser only / Kev-0.8B（凍結 v2 門檻）/ 本候選（凍結門檻）。Kev 回答來自 Colab run，不重跑 Kev。"""
    import holdout as HO
    kcases = json.load(open(os.path.join(a.kev_ref_run, "cases.json"), encoding="utf-8"))
    if [c["id"] + c["text"] for c in kcases] != [c["id"] + c["text"] for c in cal_cases]:
        print("[kev-ref] Kev run 的 cases 與候選 gate_data run 不同（seed/n_per 不一致？）→ 略過對照"); return None
    sig = json.load(open(os.path.join(a.kev_ref_run, "meta.json"))).get("catalogue_signature")
    if sig != K.catalogue_signature(): print(f"[kev-ref] 題庫簽章不同（{sig} vs {K.catalogue_signature()}）→ 略過對照"); return None
    kr = GB.read_jsonl(os.path.join(a.kev_ref_run, "kev", "raw.jsonl"))
    FT = json.load(open(os.path.join(HO.HD, "frozen_thresholds.json"), encoding="utf-8"))["by_kev"]["0.8b"]["v2"]
    test = [c for c in cal_cases if c["split"] == "test"]; P2t, _ = GB.parse_all(test)
    ok = lambda r: isinstance((r or {}).get("answers"), dict) and "emergency" in r["answers"]
    dec = lambda raw, thr: {c["id"]: GB.decide_v2(c, P2t[c["id"]], GA.kev_view(c, raw[c["id"]], P2t[c["id"]], None), K.p_noul(raw[c["id"]]["answers"], "emergency"), thr)
                           for c in test if ok(raw.get(c["id"]))}
    arms = [GB.score("Parser only", test, {c["id"]: GA.parser_only(c, P2t[c["id"]]) for c in test}),
            GB.score("Kev-0.8B（Colab，凍結 v2 門檻）", test, dec(kr, FT)), GB.score(f"P+Sys1・{a.cand}", test, dec(rc, th))]
    f = lambda x: "–" if x is None else f"{x:.3f}"
    md = ["\n## 對照：gate_data test 切分（**非 hold-out**；不影響上面的 S2 判定）\n",
          "Kev-0.8B 回答取自 Colab run（`--kev-ref-run`），門檻＝`holdout/frozen_thresholds.json` 的 v2；候選用自己的凍結門檻。gate_data 是合成資料，只能看相對差異。\n",
          "|臂|n|安全 recall|安全漏判|安全誤報率|意圖 exact|完全正確|嚴重錯誤句|", "|--|--|--|--|--|--|--|--|"]
    for r in arms:
        md.append(f"|{r['name']}|{r['n']}/{r['n_test']}|{f(r['safety']['recall'])}|{r['safety']['fn']}/{r['safety']['pos']}|{f(r['safety']['fpr'])}|{f(r['intent']['exact'])}|{r['full_ok']}/{r['n']}|{r['severe_cases']}|")
    keep = lambda r: {k: v for k, v in r.items() if not k.startswith("_")}
    return {"md": md, "metrics": [keep(r) for r in arms]}


if __name__ == "__main__":
    sys.exit(main())
