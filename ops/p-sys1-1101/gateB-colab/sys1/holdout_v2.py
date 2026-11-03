#!/usr/bin/env python3
"""router_gold_v2：新的、事前登錄的 hold-out（候選挑選與確認用）。
為什麼要有：router_gold（300 句）已經被 Kev/Gemma 看過結果，而且只有 50 句安全句；你要挑的是「替換 production 的模型」，不能拿看過答案的題目當最後一關。

  python3 sys1/holdout_v2.py check  sys1/holdout_v2/router_gold_v2.json     # 格式/配額/重複檢查
  python3 sys1/holdout_v2.py freeze sys1/holdout_v2/router_gold_v2.json     # 檢查通過才寫 sys1/holdout_v2/FREEZE.json（SHA-256），寫一次
  python3 sys1/holdout_v2.py verify                                          # 之後每次收集前確認沒被改過（exit 1=改過）

檔案格式與 router_gold.json 相同：{"cases":[{"id","query","route","boundary","source","note"}]}
配額（可在 QUOTA 調，但要在凍結前）：每個路由 ≥ 60 句、安全 ≥ 120 句（含 ≥ 40 句『回報使用後不適』）、boundary ≥ 25%、
  與 router_gold 與 gate_data 的句子（正規化後）不得重複、至少 30% 來自真實使用者問法（source=real，需去識別化、取得同意）。
安全句越多結論越窄：0 漏判時，Wilson 95% 上界 ≈ 3/n → 120 句約 2.5%；要宣稱 <1% 需要約 300 句。
"""
import hashlib, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "vendor_fk"))
ROUTES = ["product", "knowledge", "skin_tracking", "conversation", "safety", "clarify"]
QUOTA = {"per_route_min": 60, "safety_min": 120, "safety_reaction_min": 40, "boundary_frac_min": 0.25, "real_frac_min": 0.30}
FZ = os.path.join(HERE, "holdout_v2", "FREEZE.json")


def norm(t): return re.sub(r"[\s\W_]+", "", t).lower()


def known_texts():
    s = set()
    try:
        import holdout as HO; s |= {norm(c["text"]) for c in HO.cases()}
    except Exception as e: print("[warn] 讀不到 router_gold：", e)
    try:
        import gate_data as D; s |= {norm(c["text"]) for c in D.build(7, 24)}
        import extra_safety as XS; s |= {norm(c["text"]) for c in XS.cases()}
    except Exception as e: print("[warn] 讀不到 gate_data/extra_safety：", e)
    return s


def check(path):
    j = json.load(open(path, encoding="utf-8")); cs = j["cases"]; errs = []; ids = set(); seen = {}; known = known_texts()
    for c in cs:
        for k in ("id", "query", "route", "boundary", "source"):
            if k not in c: errs.append(f"{c.get('id')}: 缺欄位 {k}")
        if c.get("route") not in ROUTES: errs.append(f"{c.get('id')}: route 不合法 {c.get('route')}")
        if c.get("id") in ids: errs.append(f"{c['id']}: id 重複")
        ids.add(c.get("id")); n = norm(c.get("query", ""))
        if n in known: errs.append(f"{c['id']}: 與 router_gold/gate_data 的句子重複")
        if n in seen: errs.append(f"{c['id']}: 與 {seen[n]} 重複")
        seen[n] = c.get("id")
    cnt = {r: sum(1 for c in cs if c.get("route") == r) for r in ROUTES}
    for r, n in cnt.items():
        if n < QUOTA["per_route_min"]: errs.append(f"路由 {r} 只有 {n} 句（< {QUOTA['per_route_min']}）")
    if cnt["safety"] < QUOTA["safety_min"]: errs.append(f"安全句 {cnt['safety']} < {QUOTA['safety_min']}")
    react = sum(1 for c in cs if c.get("route") == "safety" and "reaction" in (c.get("note", "") + c.get("style", "")))
    if react < QUOTA["safety_reaction_min"]: errs.append(f"『回報使用後不適』安全句 {react} < {QUOTA['safety_reaction_min']}（note 或 style 要含 reaction）")
    b = sum(1 for c in cs if c.get("boundary")) / max(1, len(cs)); real = sum(1 for c in cs if c.get("source") == "real") / max(1, len(cs))
    if b < QUOTA["boundary_frac_min"]: errs.append(f"boundary 比例 {b:.2f} < {QUOTA['boundary_frac_min']}")
    if real < QUOTA["real_frac_min"]: errs.append(f"真實問法比例 {real:.2f} < {QUOTA['real_frac_min']}")
    print(f"n={len(cs)} 各路由={cnt} boundary={b:.2f} real={real:.2f}"); [print("  ✗", e) for e in errs[:40]]
    if len(errs) > 40: print(f"  … 另外 {len(errs) - 40} 項")
    return errs


def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] in ("check", "freeze"):
        errs = check(sys.argv[2])
        if sys.argv[1] == "check": sys.exit(1 if errs else 0)
        if errs: print("不凍結：先修正以上問題"); sys.exit(1)
        if os.path.exists(FZ): print("已經凍結過，拒絕覆蓋（要換題請另開 v3 資料夾）"); sys.exit(1)
        os.makedirs(os.path.dirname(FZ), exist_ok=True)
        json.dump({"file": os.path.abspath(sys.argv[2]), "sha256": sha(sys.argv[2]), "quota": QUOTA, "self_sha256": sha(__file__)}, open(FZ, "w"), indent=1); print("FROZEN ✓", FZ); sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "verify":
        if not os.path.exists(FZ): print("尚未凍結"); sys.exit(1)
        f = json.load(open(FZ)); ok = os.path.exists(f["file"]) and sha(f["file"]) == f["sha256"]; print("FROZEN ✓" if ok else "NOT FROZEN ✗（hold-out 檔案被改過）"); sys.exit(0 if ok else 1)
    print(__doc__); sys.exit(2)
