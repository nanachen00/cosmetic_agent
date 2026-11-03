#!/usr/bin/env python3
"""holdout — router_gold as a frozen hold-out for Gate B (routing + safety only). Rules: holdout/SCORING.md (pre-registered).
Cases are converted to the gate_data case format so the same Kev / Gemma collection code runs unchanged; analysis is separate
(analyze_holdout) and uses the FROZEN P+K thresholds (holdout/frozen_thresholds.json) — nothing is tuned on this data."""
from __future__ import annotations
import hashlib, json, math, os

HERE = os.path.dirname(os.path.abspath(__file__))
HD = os.path.join(HERE, "holdout")
ROUTES = ["product", "knowledge", "safety", "skin_tracking", "conversation", "clarify"]
GOLD_INTENTS = {"product": ["find_product"], "knowledge": ["knowledge"], "safety": ["knowledge"], "skin_tracking": ["record"],
                "conversation": ["chitchat"], "clarify": ["unclear"]}
# pre-registered arms (SCORING.md §3); prod arms are collected only for sentences the production replay sends to the LLM
GEMMA_ARMS = {"q4_0": ["min_json", "min_fs_prompt", "prod_json"], "q8_0": ["min_json", "prod_json"], "12b-q4_0": ["min_fs_prompt", "prod_json"]}
FROZEN_FILES = ["gateb.py", "holdout.py", "gemma_arms.py", "prod_replay.py", "llmclient.py", "extra_safety.py", "holdout/router_gold.json",
                "holdout/frozen_thresholds.json", "holdout/SCORING.md", "vendor_fk/kevlib.py", "vendor_fk/gateA.py", "vendor_fk/parser_v2.py",
                "vendor_fk/payload/kev_slot_parser.py", "vendor_fk/gate_data.py", "fetch_gguf.py",
                "prompts/min_system.txt", "prompts/min_fewshot.txt", "prompts/ext_system.txt", "prompts/legacy_system.txt",
                "vendor_prod/tools/agent/intent_parser.py", "vendor_prod/tools/agent/product_turn_resolver.py",
                "vendor_prod/tools/agent/dialogue_router.py", "vendor_prod/schemas/intent.py", "vendor_prod/agents/root/orchestrator.py"]


def sha(p):
    return hashlib.sha256(open(os.path.join(HERE, p), "rb").read()).hexdigest()


def write_freeze():
    json.dump({"_doc": "SHA-256 of every file that can change a hold-out decision or score. Written once, before the hold-out run.",
               "files": {p: sha(p) for p in FROZEN_FILES}}, open(os.path.join(HD, "FREEZE.json"), "w"), indent=1)


def check_freeze():
    p = os.path.join(HD, "FREEZE.json")
    if not os.path.exists(p): return False, ["FREEZE.json missing"]
    f = json.load(open(p))["files"]; bad = [k for k, v in f.items() if not os.path.exists(os.path.join(HERE, k)) or sha(k) != v]
    return not bad, bad


def cases():
    g = json.load(open(os.path.join(HD, "router_gold.json"), encoding="utf-8"))["cases"]
    out = []
    for c in g:
        out.append({"id": "h_" + c["id"], "text": c["query"], "split": "holdout", "group": c["route"] + ("_boundary" if c["boundary"] else ""),
                    "route": c["route"], "source": c["source"], "boundary": bool(c["boundary"]), "safety": c["route"] == "safety",
                    "emergency": False, "intents": GOLD_INTENTS[c["route"]], "budget": None, "avoid": [], "concern": None, "other_numbers": False})
    return out


def route_of(d):
    """SCORING.md §4 — fixed priority."""
    if not d: return "clarify"
    ints = set(d.get("intents") or [])
    if d.get("safety"): return "safety"
    if "record" in ints: return "skin_tracking"
    if ints & {"find_product", "price_comparison"}: return "product"
    if "knowledge" in ints: return "knowledge"
    if "chitchat" in ints: return "conversation"
    return "clarify"


def wilson(k, n, z=1.96):
    if not n: return (None, None)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def mcnemar(b, c):
    n = b + c
    if n == 0: return 1.0
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / 2 ** n)


def score(name, C, dec, extra=None):
    T = [c for c in C if c["id"] in dec]; pred = {c["id"]: route_of(dec[c["id"]]) for c in T}
    pos = [c for c in T if c["route"] == "safety"]; neg = [c for c in T if c["route"] != "safety"]; bnd = [c for c in T if c["boundary"]]
    tp = sum(pred[c["id"]] == "safety" for c in pos); fp = sum(pred[c["id"]] == "safety" for c in neg)
    ok = sum(pred[c["id"]] == c["route"] for c in T)
    per = {}
    for r in ROUTES:
        g = [c for c in T if c["route"] == r]; p_ = [c for c in T if pred[c["id"]] == r]
        rec = sum(pred[c["id"]] == r for c in g) / len(g) if g else None
        pre = sum(c["route"] == r for c in p_) / len(p_) if p_ else None
        f1 = 2 * rec * pre / (rec + pre) if rec and pre else 0.0
        per[r] = {"n": len(g), "recall": rec, "precision": pre, "f1": f1}
    conf = {g: {p: sum(1 for c in T if c["route"] == g and pred[c["id"]] == p) for p in ROUTES} for g in ROUTES}
    sub = lambda f: (lambda S: {"n": len(S), "acc": sum(pred[c["id"]] == c["route"] for c in S) / len(S) if S else None})([c for c in T if f(c)])
    r = {"name": name, "n": len(T), "M1_safety_recall": tp / len(pos) if pos else None, "M1_ci": wilson(tp, len(pos)), "M1_k": [tp, len(pos)],
         "M2_safety_far": fp / len(neg) if neg else None, "M2_ci": wilson(fp, len(neg)), "M2_k": [fp, len(neg)],
         "M2_boundary_far": (sum(pred[c["id"]] == "safety" for c in bnd if c["route"] != "safety"), sum(1 for c in bnd if c["route"] != "safety")),
         "M3_route_acc": ok / len(T) if T else None, "M3_ci": wilson(ok, len(T)), "M3_macro_f1": sum(v["f1"] for v in per.values()) / len(ROUTES),
         "M4_per_route": per, "M5_boundary": sub(lambda c: c["boundary"]), "M5_seed": sub(lambda c: c["source"] == "seed"),
         "M5_synth": sub(lambda c: c["source"] == "synth"), "confusion": conf, "M7_latency": None,
         "_pred": pred, "_safety_missed": [c["text"] for c in pos if pred[c["id"]] != "safety"],
         "_false_alarms": [c["text"] for c in neg if pred[c["id"]] == "safety"]}
    if extra: r.update(extra)
    return r


def paired(base, a, C):
    ids = [c["id"] for c in C if c["id"] in base["_pred"] and c["id"] in a["_pred"]]; g = {c["id"]: c for c in C}
    bR = sum(1 for i in ids if base["_pred"][i] == g[i]["route"] and a["_pred"][i] != g[i]["route"])
    aR = sum(1 for i in ids if base["_pred"][i] != g[i]["route"] and a["_pred"][i] == g[i]["route"])
    sb = lambda r, i: (r["_pred"][i] == "safety") == (g[i]["route"] == "safety")
    bS = sum(1 for i in ids if sb(base, i) and not sb(a, i)); aS = sum(1 for i in ids if not sb(base, i) and sb(a, i))
    return {"n": len(ids), "route_base_only": bR, "route_arm_only": aR, "p_route": mcnemar(bR, aR), "safety_base_only": bS, "safety_arm_only": aS, "p_safety": mcnemar(bS, aS)}


def _f(x, d=2): return "–" if x is None else f"{x:.{d}f}"
def _ci(t): return "" if not t or t[0] is None else f" [{t[0]:.2f}–{t[1]:.2f}]"


def render(arms, frozen, bad, meta, base_name):
    L = [f"# Gate B hold-out：router_gold（{'FROZEN ✓' if frozen else 'NOT FROZEN ✗ — 不算 hold-out 結果'}）\n"]
    if not frozen: L.append("與 FREEZE.json 不符的檔案：" + "、".join(f"`{b}`" for b in bad) + "\n")
    env = meta.get("env", {})
    L.append(f"300 句（每個路由 50 句；boundary 38）· GPU {env.get('gpu')} · 評分規則：`holdout/SCORING.md`（事前登錄）· P+K 門檻：`holdout/frozen_thresholds.json`（只用 gate_data cal）\n")
    L.append("> router_gold 也是合成資料（276/300 句），作者依 orchestrator 規則寫標籤；只評路由和安全，不評預算、避開成分、多意圖。安全定義包含「回報使用後的反應」，比 Kev 安全題寬。\n")
    L.append("## 主要指標\n\n|臂|n|M1 安全 recall [95% CI]|M2 安全誤報 [95% CI]|boundary 誤報|M3 路由準確率 [CI]|M3 macro-F1|M7 延遲（T4）|")
    L.append("|--|--|--|--|--|--|--|--|")
    for a in arms:
        lat = a.get("M7_latency") or {}
        v = lat.get("mean") if lat.get("kind") == "mean" else lat.get("p50")
        lt = "–" if v is None else (f"平均 {v:.0f} ms" if lat.get("kind") == "mean" else f"p50 {v:.0f} ms")
        L.append(f"|{a['name']}|{a['n']}|{a['M1_k'][0]}/{a['M1_k'][1]} = {_f(a['M1_safety_recall'])}{_ci(a['M1_ci'])}|{a['M2_k'][0]}/{a['M2_k'][1]} = {_f(a['M2_safety_far'])}{_ci(a['M2_ci'])}|"
                 f"{a['M2_boundary_far'][0]}/{a['M2_boundary_far'][1]}|{_f(a['M3_route_acc'])}{_ci(a['M3_ci'])}|{_f(a['M3_macro_f1'])}|{lt}|")
    L.append("\n## M4 各路由 recall／precision\n\n|臂|" + "|".join(ROUTES) + "|"); L.append("|--|" + "--|" * len(ROUTES))
    for a in arms: L.append(f"|{a['name']}|" + "|".join(f"{_f(a['M4_per_route'][r]['recall'])}／{_f(a['M4_per_route'][r]['precision'])}" for r in ROUTES) + "|")
    L.append("\n## M5 子集\n\n|臂|boundary 準確率|seed 準確率|synth 準確率|"); L.append("|--|--|--|--|")
    for a in arms: L.append(f"|{a['name']}|{_f(a['M5_boundary']['acc'])} (n={a['M5_boundary']['n']})|{_f(a['M5_seed']['acc'])} (n={a['M5_seed']['n']})|{_f(a['M5_synth']['acc'])} (n={a['M5_synth']['n']})|")
    L.append(f"\n## M6 與 {base_name} 成對比較（McNemar 精確檢定）\n\n|臂|n|路由：只有基準對|路由：只有此臂對|p|安全：只有基準對|安全：只有此臂對|p|")
    L.append("|--|--|--|--|--|--|--|--|")
    for a in arms:
        x = a.get("paired")
        if x: L.append(f"|{a['name']}|{x['n']}|{x['route_base_only']}|{x['route_arm_only']}|{_f(x['p_route'], 3)}|{x['safety_base_only']}|{x['safety_arm_only']}|{_f(x['p_safety'], 3)}|")
    L.append("\n## 混淆矩陣（列＝gold，欄＝預測）\n")
    for a in arms:
        L.append(f"**{a['name']}**\n\n|gold＼pred|" + "|".join(ROUTES) + "|"); L.append("|--|" + "--|" * len(ROUTES))
        for g in ROUTES: L.append(f"|{g}|" + "|".join(str(a["confusion"][g][p]) for p in ROUTES) + "|")
        L.append("")
    L.append("## 漏判的安全句與誤報（各列前 12 句）\n")
    for a in arms:
        L.append(f"- **{a['name']}** 漏判 {len(a['_safety_missed'])}：" + "；".join(a["_safety_missed"][:12]))
        L.append(f"  誤報 {len(a['_false_alarms'])}：" + "；".join(a["_false_alarms"][:12]))
    pp = [a for a in arms if a.get("paths")]
    if pp:
        L.append("\n## Production 路徑分布\n")
        for a in pp: L.append(f"- {a['name']}：" + "、".join(f"{k} {v}" for k, v in sorted(a["paths"].items(), key=lambda kv: -kv[1])))
    L.append("\n判讀規則見 SCORING.md §6：差距在 CI 內或 McNemar p ≥ 0.05 視為沒有差異；結果出來後不改任何凍結的設定。\n")
    return "\n".join(L) + "\n"


if __name__ == "__main__":                       # python3 holdout.py → FROZEN ✓ / NOT FROZEN ✗ (exit 1)
    import sys
    ok, bad = check_freeze(); print("FROZEN ✓" if ok else f"NOT FROZEN ✗ {bad}"); sys.exit(0 if ok else 1)
