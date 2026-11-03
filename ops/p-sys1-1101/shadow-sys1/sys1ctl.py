#!/usr/bin/env python3
"""sys1ctl — P+Sys1 切換與回滾（T8）。只改 overlay 控制檔（control/sys1_dispatch.json 或 --control），不碰 backend、vLLM、.env。

  python3 sys1ctl.py status
  python3 sys1ctl.py shadow                                            # S3：只記錄（回應仍走舊 LLM）
  python3 sys1ctl.py canary 5   --approved-by "負責人姓名" [--note …]   # 5% → 25% → 100%，不能跳級（從 shadow/off 只能到 5）
  python3 sys1ctl.py rollback   [--reason …]                           # 一鍵回滾：arm=off、canary_pct=0（任何時候都允許，不需同意）
  python3 sys1ctl.py check      [rollback_check 參數…]                 # 跑一次回滾條件檢查（見 rollback_check.py）

規則：
- canary（arm=sys1）需要 --approved-by（handoff T8：實際調整任何 production 的 arm 都要負責人明確同意）；紀錄寫進 logs/switch_log.jsonl。
- 切到 sys1 前檢查控制檔已設定 model / frozen（存在且 sha256 正確）/ sys1_url（127.0.0.1:8230-8239）。
- 所有寫入都是原子替換（overlay 依 mtime 重讀，不需重啟）。
"""
from __future__ import annotations
import argparse, getpass, hashlib, json, os, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = [5, 25, 100]


def atomic_json(p: Path, obj):
    t = p.with_name(p.name + f".tmp{os.getpid()}"); t.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(t, "rb+") as f: os.fsync(f.fileno())          # rb+: Windows refuses fsync on a read-only handle
    os.replace(t, p)


def log(ctl_path: Path, rec: dict):
    lp = Path(os.environ.get("SYS1CTL_LOG", str(HERE / "logs" / "switch_log.jsonl"))); lp.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(rec, ts=time.time(), at=time.strftime("%F %T"), control=str(ctl_path), user=getpass.getuser())
    with open(lp, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def preflight(c: dict) -> list[str]:
    bad = []
    if not c.get("model"): bad.append("model 未設定（registry 的 model_field）")
    if not re.fullmatch(r"http://127\.0\.0\.1:823\d/?", str(c.get("sys1_url"))): bad.append("sys1_url 必須是 http://127.0.0.1:8230-8239")
    try:
        b = json.loads(Path(c["frozen"]).read_text(encoding="utf-8"))
        if hashlib.sha256(json.dumps(b["thresholds"], sort_keys=True).encode()).hexdigest() != b.get("sha256"): bad.append("frozen 門檻 sha256 不符")
    except Exception as e: bad.append(f"frozen 門檻讀不到：{type(e).__name__}: {e}")
    if not c.get("trace"): bad.append("trace 未設定（回滾檢查需要 trace）")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--control", default=os.environ.get("SYS1_DISPATCH_CONTROL", str(HERE / "control" / "sys1_dispatch.json")))
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status"); sp.add_parser("shadow")
    c = sp.add_parser("canary"); c.add_argument("pct", type=int, choices=STEPS); c.add_argument("--approved-by", required=True); c.add_argument("--note", default="")
    r = sp.add_parser("rollback"); r.add_argument("--reason", default="manual")
    sp.add_parser("check", add_help=False)
    a, rest = ap.parse_known_args(); ctl = Path(a.control); cur = json.loads(ctl.read_text(encoding="utf-8"))
    if a.cmd == "status":
        print(json.dumps({k: cur.get(k) for k in ("arm", "canary_pct", "model", "frozen", "sys1_url", "trace", "error_fallback")}, ensure_ascii=False, indent=1)); return 0
    if a.cmd == "rollback":
        new = dict(cur, arm="off", canary_pct=0); atomic_json(ctl, new); log(ctl, {"action": "rollback", "from": {"arm": cur.get("arm"), "canary_pct": cur.get("canary_pct")}, "reason": a.reason})
        print(f"ROLLED BACK: arm {cur.get('arm')}→off (canary {cur.get('canary_pct')}→0). reason={a.reason}"); return 0
    if a.cmd == "shadow":
        bad = preflight(cur)
        if bad: print("REFUSED:\n  - " + "\n  - ".join(bad)); return 3
        new = dict(cur, arm="shadow", canary_pct=0); atomic_json(ctl, new); log(ctl, {"action": "shadow", "from": cur.get("arm")}); print("arm=shadow（回應不變，只記錄）"); return 0
    if a.cmd == "canary":
        bad = preflight(cur)
        if bad: print("REFUSED:\n  - " + "\n  - ".join(bad)); return 3
        now = int(cur.get("canary_pct") or 0) if cur.get("arm") == "sys1" else 0
        allowed = STEPS[0] if now == 0 else (STEPS[STEPS.index(now) + 1] if now in STEPS and now != STEPS[-1] else now)
        if a.pct > allowed: print(f"REFUSED: 目前 {now}%，下一級只能到 {allowed}%（5→25→100，不能跳級）"); return 3
        new = dict(cur, arm="sys1", canary_pct=a.pct); atomic_json(ctl, new)
        log(ctl, {"action": "canary", "pct": a.pct, "from": {"arm": cur.get("arm"), "canary_pct": cur.get("canary_pct")}, "approved_by": a.approved_by, "note": a.note})
        print(f"arm=sys1 canary={a.pct}%（approved by {a.approved_by}）。回滾：python3 {Path(__file__).name} rollback"); return 0
    if a.cmd == "check":
        import rollback_check; sys.argv = ["rollback_check.py", "--control", str(ctl)] + rest; return rollback_check.main()


if __name__ == "__main__":
    sys.exit(main())
