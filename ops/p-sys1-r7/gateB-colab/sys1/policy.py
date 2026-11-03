#!/usr/bin/env python3
"""基座來源規則（fail-closed）。

規則（使用者指定）：除了 Kev（只當對照）以外，任何 System1 候選的『基座模型』都不能是中國模型。
- lineage 裡任何一個名字命中 DENY_FAMILIES → blocked
- base_origin 含 "CN"（純中國）→ blocked；"CN-finetune-on-non-CN-base" → grey（預設拒絕）
- provenance_verified 不是 true → 拒絕（沒查證就不收集；查證要寫進 registry.json 並留下 verified_by / verified_on）
- role == reference（Kev）只能當對照，不能被選為替換候選

用法：python3 sys1/policy.py check <id> [--purpose collect|promote]   exit 0=允許 3=拒絕 2=找不到
      python3 sys1/policy.py list
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DENY_FAMILIES = ["qwen", "deepseek", "yi-", "chatglm", "glm", "internlm", "baichuan", "minicpm", "ernie", "hunyuan", "kimi",
                 "moonshot", "minimax", "doubao", "skywork", "telechat"]


def load():
    return {c["id"]: c for c in json.load(open(os.environ.get("SYS1_REGISTRY") or os.path.join(HERE, "registry.json"), encoding="utf-8"))["candidates"]}


def verdict(c, purpose="collect"):
    lin = " ".join(c.get("lineage", [])).lower()
    hit = [d for d in DENY_FAMILIES if d in lin]
    if c.get("role") == "reference":
        return (purpose == "collect"), ("reference：只能當對照；不得 promote" if purpose != "collect" else "reference（對照用，允許收集）")
    if hit:
        return False, f"blocked：lineage 命中中國模型家族 {hit}"
    if c.get("base_origin", "").upper() == "CN":
        return False, "blocked：base_origin=CN"
    if c.get("role") == "grey" or "finetune" in c.get("base_origin", ""):
        if not c.get("written_approval"):
            return False, "grey：需要書面同意（registry 加 written_approval:{by,on,note}）才可收集"
    if c.get("provenance_verified") is not True:
        return False, "unverified：先完成 verify 清單，再在 registry 設 provenance_verified=true 並填 verified_by/verified_on"
    if not (c.get("verified_by") and c.get("verified_on")):
        return False, "provenance_verified=true 但缺 verified_by / verified_on"
    return True, "allowed"


def check(cid, purpose="collect"):
    reg = load()
    if cid not in reg:
        return 2, f"{cid}: 不在 registry.json"
    ok, why = verdict(reg[cid], purpose)
    return (0 if ok else 3), f"{cid}: {why}"


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        for cid, c in load().items():
            print(f"{cid:16s} {c['role']:10s} {verdict(c)[1]}")
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "check":
        purpose = sys.argv[sys.argv.index("--purpose") + 1] if "--purpose" in sys.argv else "collect"
        code, msg = check(sys.argv[2], purpose); print(msg); sys.exit(code)
    print(__doc__); sys.exit(2)
