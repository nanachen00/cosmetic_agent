#!/usr/bin/env python3
"""負責人簽核紀錄（SIGNOFF.json）的檢查。

SIGNOFF.json 記錄 S0 決策，以及定案時 gates.json / train/budget.json 的 SHA-256。定案後這兩個檔案不得再改
（handoff §4-2、§5）；run_vm.sh 在 train / s2 之前呼叫 `verify`，內容不符就拒絕。

用法：python3 sys1/signoff.py verify      exit 0=一致 3=不一致或缺檔
      python3 sys1/signoff.py show
"""
import hashlib, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.environ.get("SYS1_SIGNOFF") or os.path.join(HERE, "SIGNOFF.json")


def sha(rel):
    with open(os.path.join(HERE, rel), "rb") as f: return hashlib.sha256(f.read()).hexdigest()


def verify():
    try: s = json.load(open(PATH, encoding="utf-8"))
    except FileNotFoundError: return 3, f"{PATH} 不存在：S0 尚未簽核"
    bad = [f"{rel}（簽核時 {want[:12]}…，現在 {sha(rel)[:12]}…）" for rel, want in s["frozen_files"].items() if sha(rel) != want]
    if bad: return 3, "簽核後被修改：" + "；".join(bad) + "。要改就重新簽核並在報告記錄。"
    return 0, f"SIGNOFF 一致（{s['signed_by']}，{s['signed_on']}）：" + "、".join(s["frozen_files"])


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "verify":
        code, msg = verify(); print(msg); sys.exit(code)
    if len(sys.argv) == 2 and sys.argv[1] == "show":
        print(json.dumps(json.load(open(PATH, encoding="utf-8")), ensure_ascii=False, indent=1)); sys.exit(0)
    print(__doc__); sys.exit(2)
