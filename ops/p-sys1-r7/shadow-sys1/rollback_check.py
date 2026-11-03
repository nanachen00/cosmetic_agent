#!/usr/bin/env python3
"""rollback_check — 可執行的回滾條件（T8）。讀 overlay trace（與選用的 backend HTTP 狀態紀錄），任一條件成立 → 回滾（arm=off）。

條件（預設值＝handoff T8；可用參數改，但改了要寫進切換紀錄）：
  R1  Sys1 錯誤率（最近 --window-s 秒、used∈{sys1, llm_after_error} 的列）> --max-err（2%），且列數 ≥ --min-n
  R2  Sys1 延遲 p95（含 Parser，sys1_total_ms）> --p95-ms（400 ms），且列數 ≥ --min-n
  R3  5xx 增加：--http-log（jsonl：{"ts","status"}）最近 window 的 5xx 率 − 前一段 --baseline-s 的 5xx 率 > --max-5xx-increase（1 個百分點）
  （列數不足 → 不判定，exit 2；不會因為資料少就回滾，也不會因此判定安全）

  python3 rollback_check.py --control control/sys1_dispatch.json [--trace …] [--http-log …] [--apply] [--watch --interval 15]
exit：0 健康 · 10 觸發回滾（--apply 時已把 arm 設成 off 並記錄）· 2 資料不足 · 3 設定錯誤
"""
from __future__ import annotations
import argparse, json, os, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def pct(xs, q):
    xs = sorted(xs)
    if not xs: return None
    h = (len(xs) - 1) * q; lo = int(h); hi = min(lo + 1, len(xs) - 1); return xs[lo] + (h - lo) * (xs[hi] - xs[lo])


def read_jsonl_tail(p: Path, since: float, max_bytes=64 << 20):
    if not p or not p.exists(): return []
    out = []
    with open(p, "rb") as f:
        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - max_bytes)); data = f.read().decode("utf-8", "replace").splitlines()
    for l in data:
        try: r = json.loads(l)
        except Exception: continue
        if r.get("ts", 0) >= since: out.append(r)
    return out


def evaluate(a, now=None):
    now = now or time.time(); ctl = json.loads(Path(a.control).read_text(encoding="utf-8"))
    trace = Path(a.trace or ctl.get("trace") or "")
    if not str(trace): return 3, {"error": "no trace configured"}, ctl
    rows = [r for r in read_jsonl_tail(trace, now - a.window_s) if r.get("used") in ("sys1", "llm_after_error")]
    n = len(rows); errs = sum(1 for r in rows if r.get("sys1_error")); lat = [r["sys1_total_ms"] for r in rows if r.get("sys1_total_ms") is not None and not r.get("sys1_error")]
    res = {"window_s": a.window_s, "n": n, "errors": errs, "err_rate": errs / n if n else None, "p95_ms": pct(lat, .95), "triggered": []}
    if n >= a.min_n:
        if errs / n > a.max_err: res["triggered"].append(f"R1 Sys1 錯誤率 {errs}/{n}={errs / n:.3f} > {a.max_err}")
        if res["p95_ms"] is not None and res["p95_ms"] > a.p95_ms: res["triggered"].append(f"R2 p95 {res['p95_ms']:.0f} ms > {a.p95_ms} ms")
    if a.http_log:
        h = read_jsonl_tail(Path(a.http_log), now - a.window_s - a.baseline_s)
        cur = [r for r in h if r["ts"] >= now - a.window_s]; base = [r for r in h if r["ts"] < now - a.window_s]
        rate = lambda R: sum(1 for r in R if int(r.get("status", 0)) >= 500) / len(R) if R else None
        res["http"] = {"n": len(cur), "rate_5xx": rate(cur), "baseline_n": len(base), "baseline_rate_5xx": rate(base)}
        if len(cur) >= a.min_n and base and rate(cur) - rate(base) > a.max_5xx_increase:
            res["triggered"].append(f"R3 5xx {rate(cur):.3f} vs baseline {rate(base):.3f} (+{rate(cur) - rate(base):.3f} > {a.max_5xx_increase})")
    if res["triggered"]: return 10, res, ctl
    return (0 if n >= a.min_n else 2), res, ctl


def rollback(a, res):
    import sys1ctl
    ctl = Path(a.control); cur = json.loads(ctl.read_text(encoding="utf-8"))
    if cur.get("arm") == "off": return False
    sys1ctl.atomic_json(ctl, dict(cur, arm="off", canary_pct=0))
    sys1ctl.log(ctl, {"action": "auto_rollback", "from": {"arm": cur.get("arm"), "canary_pct": cur.get("canary_pct")}, "reason": "; ".join(res["triggered"]), "metrics": res})
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--control", default=str(HERE / "control" / "sys1_dispatch.json")); ap.add_argument("--trace"); ap.add_argument("--http-log")
    ap.add_argument("--window-s", type=float, default=300); ap.add_argument("--baseline-s", type=float, default=3600); ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--max-err", type=float, default=0.02); ap.add_argument("--p95-ms", type=float, default=400); ap.add_argument("--max-5xx-increase", type=float, default=0.01)
    ap.add_argument("--apply", action="store_true"); ap.add_argument("--watch", action="store_true"); ap.add_argument("--interval", type=float, default=15)
    a = ap.parse_args()
    while True:
        code, res, ctl = evaluate(a)
        print(json.dumps({"at": time.strftime("%F %T"), "arm": ctl.get("arm"), "canary_pct": ctl.get("canary_pct"), "code": code, **res}, ensure_ascii=False), flush=True)
        if code == 10 and a.apply and rollback(a, res): print("AUTO-ROLLBACK applied: arm=off", flush=True)
        if not a.watch: return code
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
